# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

import dataclasses
import hashlib
import importlib.util
import json
import logging
import os
import shutil
import sys
import termios
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anthropic

from aiform import config, graph, llm, log, parser, planner, state
from aiform.driver import DriverUpdateNotSupported, ResourceDriver
from aiform.exceptions import DriverExecutionError, PlanBlockedError, ResourceNotFoundError
from aiform.models import (
    DriverInfo,
    LLMConfig,
    ParsedResource,
    PlanAction,
    PlanEntry,
    PlanReview,
    PlanReviewFlag,
    PlanReviewSeverity,
    ResourceSpec,
    StateEntry,
)
from aiform.state import State

logger = logging.getLogger(__name__)

DRIVERS_DIR = Path(__file__).resolve().parent.parent / "drivers"
TRASH_DIR = Path(".aiform/trash")


def resource_key(provider: str, resource_type: str, name: str) -> str:
    return f"{provider}.{resource_type}.{name}"


def _log_driver_outcome(
    provider: str, resource_type: str, operation: str, duration_ms: int, *, outcome: str
) -> None:
    # Shared by _call_driver() and apply_plan()'s UPDATE branch, which
    # can't route through _call_driver() itself -- its blanket
    # DriverExecutionError wrapping would swallow DriverUpdateNotSupported,
    # a signal that branch needs unwrapped to decide on a delete+create
    # fallback. Only the logging *shape* is shared; each site keeps its
    # own exception handling. Factored out after /code-review: the two
    # copies had already drifted once (the update branch's first pass
    # missed the outcome=error case entirely -- see specs/orchestrator.md).
    level = logging.INFO if outcome == "success" else logging.ERROR
    logger.log(
        level,
        "",
        extra={
            "provider": provider,
            "resource_type": resource_type,
            "operation": operation,
            "duration_ms": duration_ms,
            "outcome": outcome,
        },
    )


def _call_driver(fn: Callable[..., Any], provider: str, resource_type: str, operation: str, *args):
    start = time.monotonic()
    try:
        result = fn(*args)
    except Exception as exc:
        _log_driver_outcome(
            provider, resource_type, operation, log.elapsed_ms(start), outcome="error"
        )
        raise DriverExecutionError(provider, resource_type, operation, exc) from exc
    _log_driver_outcome(
        provider, resource_type, operation, log.elapsed_ms(start), outcome="success"
    )
    return result


def _pop_id(
    raw: dict[str, Any], provider: str, resource_type: str, operation: str
) -> tuple[str, dict[str, Any]]:
    attrs = dict(raw)
    try:
        new_id = attrs.pop("id")
    except KeyError as exc:
        raise DriverExecutionError(provider, resource_type, operation, exc) from exc
    return new_id, attrs


def _require_tracked(st: State, key: str) -> StateEntry:
    try:
        return st.resources[key]
    except KeyError:
        raise PlanBlockedError(
            f"{key}: expected to be tracked in state, but state.json no longer has it -- "
            "state may have changed since this plan was built; re-run plan"
        ) from None


def _new_state_entry(
    pr: "PlannedResource", new_id: str, attrs: dict[str, Any], now: datetime
) -> StateEntry:
    return StateEntry(
        provider=pr.provider,
        resource_type=pr.resource_type,
        name=pr.name,
        id=new_id,
        attributes=attrs,
        driver=pr.driver_info,
        last_applied_at=now,
        last_refreshed_at=now,
        aiform_md_path=str(pr.aiform_md_path),
        aiform_md_sha256=pr.current_aiform_md_sha256,
        depends_on=pr.depends_on,
    )


def discover_files(paths: list[Path] | None, *, cwd: Path = Path(".")) -> list[Path]:
    if paths:
        return list(paths)
    return sorted(cwd.glob("*.aiform.md"))


def is_delete_marked(path: Path) -> bool:
    return path.name.startswith("AIFORM-DELETE-")


def driver_path(provider: str, resource_type: str) -> Path:
    return DRIVERS_DIR / provider / f"{resource_type}.py"


def load_driver(provider: str, resource_type: str) -> ResourceDriver:
    path = driver_path(provider, resource_type)
    try:
        spec = importlib.util.spec_from_file_location(
            f"aiform_driver_{provider}_{resource_type}", path
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except FileNotFoundError:
        raise PlanBlockedError(
            f"no driver found for (provider={provider!r}, resource_type={resource_type!r}) "
            f"-- expected {path}"
        ) from None
    return module.Driver()


def driver_info_for(
    provider: str,
    resource_type: str,
    state: State,
) -> DriverInfo:
    path = driver_path(provider, resource_type)
    on_disk_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()

    reused = next(
        (
            entry.driver
            for entry in state.resources.values()
            if entry.provider == provider
            and entry.resource_type == resource_type
            and entry.driver.sha256 == on_disk_sha256
        ),
        None,
    )
    logger.info(
        "",
        extra={"provider": provider, "resource_type": resource_type, "reused": reused is not None},
    )
    if reused is not None:
        return reused

    return DriverInfo(
        path=f"drivers/{provider}/{resource_type}.py",
        sha256=on_disk_sha256,
        generated_at=datetime.now(UTC),
    )


def refresh_resource(
    driver: ResourceDriver, state_entry: StateEntry, credentials: dict[str, str]
) -> tuple[dict[str, Any], bool]:
    start = time.monotonic()
    try:
        raw = driver.read(state_entry.id, credentials)
    except ResourceNotFoundError:
        logger.warning(
            "",
            extra={
                "provider": state_entry.provider,
                "resource_type": state_entry.resource_type,
                "id": state_entry.id,
                "drifted_missing": True,
            },
        )
        return state_entry.attributes, True
    except Exception as exc:
        _log_driver_outcome(
            state_entry.provider,
            state_entry.resource_type,
            "read",
            log.elapsed_ms(start),
            outcome="error",
        )
        raise DriverExecutionError(
            state_entry.provider, state_entry.resource_type, "read", exc
        ) from exc
    _new_id, attrs = _pop_id(raw, state_entry.provider, state_entry.resource_type, "read")
    # A driver's read() structurally cannot verify a NON_DIFFABLE_FIELDS
    # key (e.g. ssh_keys -- write-only on the CSP side); carry the prior
    # state's value forward instead of letting it get blanked out here,
    # so planner.py's diff still correctly sees "unchanged" when nothing
    # changed, and correctly sees a real diff when the desired value
    # genuinely differs from what was last known -- never silently drops
    # an intended change (see specs/digitalocean_compute.md).
    for field in driver.NON_DIFFABLE_FIELDS:
        if field not in attrs and field in state_entry.attributes:
            attrs[field] = state_entry.attributes[field]
    return attrs, False


def refresh_state(*, state_path: Path = state.DEFAULT_STATE_PATH) -> State:
    st = state.load(state_path)
    driver_cache: dict[tuple[str, str], ResourceDriver] = {}
    credentials_cache: dict[str, dict[str, str]] = {}

    for entry in st.resources.values():
        driver_key = (entry.provider, entry.resource_type)
        if driver_key not in driver_cache:
            driver_cache[driver_key] = load_driver(entry.provider, entry.resource_type)
        driver = driver_cache[driver_key]

        if entry.provider not in credentials_cache:
            try:
                credentials_cache[entry.provider] = config.resolve_credentials(entry.provider)
            except RuntimeError as exc:
                raise PlanBlockedError(str(exc)) from exc
        credentials = credentials_cache[entry.provider]

        attrs, _drifted_missing = refresh_resource(driver, entry, credentials)
        entry.attributes = attrs
        entry.last_refreshed_at = datetime.now(UTC)
    state.save(st, state_path)
    return st


@dataclass
class PlannedResource:
    entry: PlanEntry
    provider: str
    resource_type: str
    name: str
    desired_params: dict[str, Any]
    aiform_md_path: Path
    current_aiform_md_sha256: str | None
    driver: ResourceDriver | None
    driver_info: DriverInfo | None
    credentials: dict[str, str] | None
    state_entry: StateEntry | None
    depends_on: list[str] = dataclasses.field(default_factory=list)


@dataclass
class _DiscoveredFile:
    path: Path
    key: str
    spec: ResourceSpec
    delete_marked: bool


def _discover_one(path: Path) -> _DiscoveredFile:
    content = path.read_text(encoding="utf-8-sig")
    resource_spec = parser.parse_frontmatter(content)
    key = resource_key(resource_spec.provider, resource_spec.resource, resource_spec.name)
    return _DiscoveredFile(
        path=path, key=key, spec=resource_spec, delete_marked=is_delete_marked(path)
    )


def _dedupe_normalized_paths(paths: list[Path]) -> list[Path]:
    # The same file handed twice under different spellings (`app.aiform.md`
    # and `./app.aiform.md`) is not a genuine duplicate declaration -- it is
    # one file read twice. Collapsing by normalized path here, before
    # _check_duplicate_keys() ever sees it, keeps that error for its real
    # case: two *different* files declaring the same resource key.
    #
    # os.path.abspath(), not Path.resolve(): abspath only makes the path
    # absolute and folds `.`/`..` via normpath, so it still collapses
    # `./app.aiform.md` into `app.aiform.md`. resolve() additionally
    # follows symlinks, which would collapse a symlink and its target into
    # one entry and silently pick whichever spelling came first -- exactly
    # the ambiguity _check_duplicate_keys() exists to refuse, since trashing
    # the symlink's spelling on destroy would leave the target file behind
    # to resurrect the resource on the next `plan create`.
    seen: set[str] = set()
    deduped: list[Path] = []
    for path in paths:
        normalized = os.path.abspath(path)
        if normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(path)
    return deduped


def _check_duplicate_keys(discovered: list[_DiscoveredFile]) -> None:
    seen: dict[str, Path] = {}
    for entry in discovered:
        if entry.key in seen:
            raise PlanBlockedError(
                f"{entry.key}: declared by both {seen[entry.key]} and {entry.path} in this run"
            )
        seen[entry.key] = entry.path


# Per target, not per resource: one resource can legally have a mix of
# targets resolved different ways (specs/resource_dependencies.md's
# "Which targets contribute an edge" table). A live declaring resource and
# a delete-marked declaring resource resolve the same target differently
# on purpose -- a destroy runs after every live action by construction
# (see the ordering built by _order_files below), so a delete-marked
# resource depending on a live one needs no edge to stay correct, while a
# live resource depending on something being destroyed in the same run is
# a genuine conflict.
def _resolve_dependency_edges(discovered: list[_DiscoveredFile], st: State) -> dict[str, set[str]]:
    delete_marked_keys = {entry.key for entry in discovered if entry.delete_marked}
    live_keys = {entry.key for entry in discovered if not entry.delete_marked}

    edges: dict[str, set[str]] = {entry.key: set() for entry in discovered}
    for entry in discovered:
        for target in entry.spec.depends_on:
            if entry.delete_marked:
                if target in delete_marked_keys:
                    edges[entry.key].add(target)
                continue
            if target in delete_marked_keys:
                raise PlanBlockedError(
                    f"{entry.key}: depends on {target!r}, which is marked for deletion in this run"
                )
            if target in live_keys:
                edges[entry.key].add(target)
            elif target in st.resources:
                continue
            else:
                raise PlanBlockedError(
                    f"{entry.key}: depends on {target!r}, which is neither a file in this "
                    "run nor a resource tracked in state"
                )
    return edges


def _topological(keys: set[str], edges: dict[str, set[str]]) -> list[str]:
    try:
        return graph.topological_order(keys, edges)
    except graph.CycleError as exc:
        raise PlanBlockedError("dependency cycle: " + " -> ".join(exc.path)) from exc


def _reverse_topological(keys: set[str], edges: dict[str, set[str]]) -> list[str]:
    return list(reversed(_topological(keys, edges)))


def _order_files(files: list[Path], st: State) -> list[Path]:
    files = _dedupe_normalized_paths(files)
    discovered = [_discover_one(path) for path in files]
    _check_duplicate_keys(discovered)
    edges = _resolve_dependency_edges(discovered, st)

    delete_marked_keys = {entry.key for entry in discovered if entry.delete_marked}
    live_keys = {entry.key for entry in discovered if not entry.delete_marked}
    live_order = _topological(live_keys, {k: edges[k] for k in live_keys})
    destroy_order = _reverse_topological(
        delete_marked_keys, {k: edges[k] for k in delete_marked_keys}
    )

    by_key = {entry.key: entry.path for entry in discovered}
    return [by_key[key] for key in live_order] + [by_key[key] for key in destroy_order]


def build_create_plan(
    paths: list[Path] | None = None,
    *,
    cwd: Path = Path("."),
    state_path: Path = state.DEFAULT_STATE_PATH,
    client: anthropic.Anthropic | None = None,
    llm_config: LLMConfig | None = None,
) -> tuple[list[PlannedResource], list[str]]:
    st = state.load(state_path)
    files = discover_files(paths, cwd=cwd)
    ordered_files = _order_files(files, st)

    driver_cache: dict[tuple[str, str], tuple[ResourceDriver, DriverInfo]] = {}
    credentials_cache: dict[str, dict[str, str]] = {}

    planned: list[PlannedResource] = []
    covered_keys: set[str] = set()

    for path in ordered_files:
        if is_delete_marked(path):
            pr = _plan_delete_marked(path, st)
        else:
            pr = _plan_one(
                path,
                st,
                driver_cache,
                credentials_cache,
                client=client,
                llm_config=llm_config,
            )
        covered_keys.add(pr.entry.resource_key)
        planned.append(pr)

    state.save(st, state_path)

    return planned, _warnings_for_uncovered(st, covered_keys, paths)


def _plan_delete_marked(path: Path, st: State) -> PlannedResource:
    content = path.read_text(encoding="utf-8-sig")
    resource_spec = parser.parse_frontmatter(content)
    key = resource_key(resource_spec.provider, resource_spec.resource, resource_spec.name)
    state_entry = st.resources.get(key)
    entry = planner.destroy_entry(key, rationale=f"marked for deletion via {path.name}")
    return PlannedResource(
        entry=entry,
        provider=resource_spec.provider,
        resource_type=resource_spec.resource,
        name=resource_spec.name,
        desired_params={},
        aiform_md_path=path,
        current_aiform_md_sha256=None,
        driver=None,
        driver_info=None,
        credentials=None,
        state_entry=state_entry,
        depends_on=resource_spec.depends_on,
    )


def _plan_one(
    path: Path,
    st: State,
    driver_cache: dict[tuple[str, str], tuple[ResourceDriver, DriverInfo]],
    credentials_cache: dict[str, dict[str, str]],
    *,
    client: anthropic.Anthropic | None,
    llm_config: LLMConfig | None,
) -> PlannedResource:
    content = path.read_text(encoding="utf-8-sig")
    resource_spec = parser.parse_frontmatter(content)
    key = resource_key(resource_spec.provider, resource_spec.resource, resource_spec.name)
    state_entry = st.resources.get(key)
    previous_hash = state_entry.aiform_md_sha256 if state_entry else None

    parsed = _parsed_resource(
        path,
        content,
        resource_spec,
        state_entry,
        previous_hash=previous_hash,
        client=client,
        llm_config=llm_config,
    )
    driver, driver_info = _driver_for(
        resource_spec.provider, resource_spec.resource, st, driver_cache
    )
    credentials = _credentials_for(resource_spec.provider, credentials_cache)

    entry, params_agree = _decide_action(
        key,
        resource_spec,
        parsed,
        state_entry,
        driver=driver,
        credentials=credentials,
        previous_hash=previous_hash,
        client=client,
        llm_config=llm_config,
    )

    # depends_on is ordering metadata, not resource config, so it is kept
    # in sync with the file on every plan run regardless of the action
    # decided below -- unlike aiform_md_sha256 just below, its correctness
    # never depends on params_agree. This matters specifically for NO_OP:
    # apply_plan() skips NO_OP before any state write, so a NO_OP is the
    # only action that never reaches _record_update()/_new_state_entry()'s
    # own depends_on writes. Retrofitting depends_on: onto an
    # already-tracked resource with unchanged params would otherwise never
    # reach state.json, leaving `plan destroy` ordering by stale (empty)
    # edges forever -- no apply can repair it, since there is nothing
    # non-NO_OP to apply.
    if state_entry is not None:
        state_entry.depends_on = resource_spec.depends_on

    # The toll for a text-only edit is spent by `plan`, so `plan` is
    # what clears it. apply_plan() skips NO_OP before any state write,
    # so without this a reworded Intent section left state's hash stale
    # forever and every later plan re-paid for a categorization it had
    # already run -- issue #195.
    #
    # `params_agree` is load-bearing, not belt-and-braces: a NO_OP whose
    # diff is NON-empty is legal (prompts/diff_plan.md lets the model
    # call a cosmetic difference semantically identical), and recording
    # the hash there is actively harmful. The diff would stay non-empty,
    # so the next run still fails plan_resource()'s `not diff` conjunct
    # and still calls the model -- but parse_file() would now see a
    # matching hash and skip intent extraction, feeding that call
    # `intent_notes=[]` forever. The user's Intent guidance would be
    # silently dropped from every subsequent categorization, and the
    # answer can flip from no-op to update/likely_replace.
    if entry.action == PlanAction.NO_OP and state_entry is not None and params_agree:
        state_entry.aiform_md_sha256 = parsed.aiform_md_sha256

    return PlannedResource(
        entry=entry,
        provider=resource_spec.provider,
        resource_type=resource_spec.resource,
        name=resource_spec.name,
        desired_params=resource_spec.params,
        aiform_md_path=path,
        current_aiform_md_sha256=parsed.aiform_md_sha256,
        driver=driver,
        driver_info=driver_info,
        credentials=credentials,
        state_entry=state_entry,
        depends_on=resource_spec.depends_on,
    )


def _parsed_resource(
    path: Path,
    content: str,
    resource_spec: ResourceSpec,
    state_entry: StateEntry | None,
    *,
    previous_hash: str | None,
    client: anthropic.Anthropic | None,
    llm_config: LLMConfig | None,
) -> ParsedResource:
    # parse_file() only when a state entry exists. It does three things:
    # read the file, parse the frontmatter, and -- if the hash moved --
    # spend one intent_orchestration_call on extract_intent_notes(). The
    # first two are already done by the caller, and the third feeds
    # `intent_notes`, which is consumed only by plan_resource() in
    # _decide_action()'s tracked branch. On an untracked resource that call
    # was bought and thrown away, which is what made PLAN.md §9's "a first
    # plan create makes zero Anthropic calls" false (#125): the parse cost
    # one every time, and a second read of the same file with it.
    if state_entry is None:
        return ParsedResource(
            spec=resource_spec,
            intent_notes=[],
            aiform_md_sha256=parser.compute_sha256(content),
        )
    return parser.parse_file(
        path, previous_aiform_md_sha256=previous_hash, client=client, llm_config=llm_config
    )


def _driver_for(
    provider: str,
    resource_type: str,
    st: State,
    cache: dict[tuple[str, str], tuple[ResourceDriver, DriverInfo]],
) -> tuple[ResourceDriver, DriverInfo]:
    driver_key = (provider, resource_type)
    if driver_key not in cache:
        driver = load_driver(provider, resource_type)
        driver_info = driver_info_for(provider, resource_type, st)
        cache[driver_key] = (driver, driver_info)
    return cache[driver_key]


def _credentials_for(provider: str, cache: dict[str, dict[str, str]]) -> dict[str, str]:
    if provider not in cache:
        try:
            cache[provider] = config.resolve_credentials(provider)
        except RuntimeError as exc:
            raise PlanBlockedError(str(exc)) from exc
    return cache[provider]


# Refreshes the tracked entry's `attributes`/`last_refreshed_at` in place as
# a side effect, which is what build_create_plan()'s single trailing
# state.save() then persists. The structural cross-checks live here rather
# than at the call site so `drifted_missing` -- which only the checks read --
# does not have to escape as a third return value.
def _decide_action(
    key: str,
    resource_spec: ResourceSpec,
    parsed: ParsedResource,
    state_entry: StateEntry | None,
    *,
    driver: ResourceDriver,
    credentials: dict[str, str],
    previous_hash: str | None,
    client: anthropic.Anthropic | None,
    llm_config: LLMConfig | None,
) -> tuple[PlanEntry, bool]:
    # The model is asked to categorize only when the answer is
    # genuinely open. Both `create` cases below are settled by this
    # module's own records, so asking about them means asking a
    # question whose answer is already held -- issue #117, where the
    # model answered 'update' for a brand-new resource and the
    # cross-check below then blocked a user's first `plan apply` on
    # an internal invariant they could not act on.
    if state_entry is None:
        # Nothing to refresh and no diff to build: create_entry()
        # takes only the key and a rationale. drifted_missing is
        # still bound because the cross-check below names it -- the
        # `and` short-circuits before reading it on this path, but
        # leaving it unbound would be a trap for the next edit.
        drifted_missing = False
        params_agree = False
        entry = planner.create_entry(
            key, rationale="no state entry is tracked for this resource yet"
        )
    else:
        current_attributes, drifted_missing = refresh_resource(driver, state_entry, credentials)
        state_entry.attributes = current_attributes
        state_entry.last_refreshed_at = datetime.now(UTC)

        if drifted_missing:
            # Tracked, but gone from the CSP: it must be recreated,
            # whatever the diff says. prompts/diff_plan.md already
            # told the model this answer was forced ("always means the
            # resource needs to be created again, regardless of what
            # `diff` contains") -- a forced answer is not a question.
            # Left asked, this was the sharper half of #117: neither
            # cross-check below fires for `update` on a drifted
            # resource, so a wrong answer reached apply_plan() and
            # called driver.update() against an id that no longer
            # exists, failing mid-apply rather than at plan time.
            params_agree = False
            entry = planner.create_entry(
                key, rationale="tracked resource no longer exists on the provider side"
            )
        else:
            entry, params_agree = planner.plan_resource(
                key,
                current_attributes,
                resource_spec.params,
                intent_notes=parsed.intent_notes,
                param_schema=driver.PARAM_SCHEMA,
                likely_replace_fields=driver.LIKELY_REPLACE_FIELDS,
                unordered_fields=driver.UNORDERED_FIELDS,
                state_aiform_md_sha256=previous_hash,
                current_aiform_md_sha256=parsed.aiform_md_sha256,
                drifted_missing=drifted_missing,
                client=client,
                llm_config=llm_config,
            )

    # The first check is unreachable by construction since the branch
    # above -- an untracked resource is never categorized, so there is
    # no model answer to disagree with. Kept rather than deleted
    # because it costs nothing and states the invariant plainly; note
    # it is NOT what would catch a regression here, since unreachable
    # code cannot fail a test. The call-count assertions in
    # tests/test_orchestrator.py are what actually guard the branch.
    # The second check is still live and still reachable: the model
    # can answer 'create' for a resource that IS tracked and present.
    if entry.action == PlanAction.UPDATE and state_entry is None:
        raise PlanBlockedError(
            f"{key}: categorization returned 'update' but no state entry is tracked for it"
        )
    if entry.action == PlanAction.CREATE and state_entry is not None and not drifted_missing:
        raise PlanBlockedError(
            f"{key}: categorization returned 'create' but a state entry is already tracked "
            "for it and it has not drifted missing"
        )

    return entry, params_agree


def _warnings_for_uncovered(
    st: State, covered_keys: set[str], paths: list[Path] | None
) -> list[str]:
    warnings: list[str] = []
    if not paths:
        for key in st.resources:
            if key not in covered_keys:
                warnings.append(
                    f"resource {key!r} is tracked in state but has no corresponding "
                    ".aiform.md file this run; left unchanged"
                )
    return warnings


def build_destroy_plan(
    paths: list[Path] | None = None,
    *,
    state_path: Path = state.DEFAULT_STATE_PATH,
) -> list[PlannedResource]:
    st = state.load(state_path)
    if paths:
        return _build_destroy_plan_from_paths(paths, st)
    return _build_destroy_plan_from_state(st)


def _build_destroy_plan_from_paths(paths: list[Path], st: State) -> list[PlannedResource]:
    paths = _dedupe_normalized_paths(paths)
    discovered = [_discover_one(path) for path in paths]
    _check_duplicate_keys(discovered)

    edges = {entry.key: set(entry.spec.depends_on) for entry in discovered}
    order = _reverse_topological({entry.key for entry in discovered}, edges)
    by_key = {entry.key: (entry.path, entry.spec) for entry in discovered}

    planned: list[PlannedResource] = []
    for key in order:
        path, resource_spec = by_key[key]
        state_entry = st.resources.get(key)
        entry = planner.destroy_entry(key, rationale=f"explicit destroy requested via {path}")
        planned.append(
            PlannedResource(
                entry=entry,
                provider=resource_spec.provider,
                resource_type=resource_spec.resource,
                name=resource_spec.name,
                desired_params={},
                aiform_md_path=path,
                current_aiform_md_sha256=None,
                driver=None,
                driver_info=None,
                credentials=None,
                state_entry=state_entry,
                depends_on=resource_spec.depends_on,
            )
        )
    return planned


def _build_destroy_plan_from_state(st: State) -> list[PlannedResource]:
    edges = {key: set(entry.depends_on) for key, entry in st.resources.items()}
    order = _reverse_topological(set(st.resources), edges)

    planned: list[PlannedResource] = []
    for key in order:
        state_entry = st.resources[key]
        entry = planner.destroy_entry(
            key,
            rationale="explicit destroy requested: no files given, destroying all tracked "
            "resources",
        )
        planned.append(
            PlannedResource(
                entry=entry,
                provider=state_entry.provider,
                resource_type=state_entry.resource_type,
                name=state_entry.name,
                desired_params={},
                aiform_md_path=Path(state_entry.aiform_md_path),
                current_aiform_md_sha256=None,
                driver=None,
                driver_info=None,
                credentials=None,
                state_entry=state_entry,
                depends_on=state_entry.depends_on,
            )
        )
    return planned


ConfirmFn = Callable[[str], bool]
OnReviewFn = Callable[[list[PlanReviewFlag]], None]


@dataclass
class ApplyResult:
    executed: list[PlanEntry]
    review_flags: list[PlanReviewFlag]
    aborted: bool


def default_confirm(prompt: str) -> bool:
    # Gate #2's review call takes tens of seconds with nothing on screen, so a
    # keystroke typed during it is already queued in the terminal when the
    # prompt finally appears, and input() would take it as the answer to a
    # prompt the user never saw -- a stray "y" would approve a destroy nobody
    # agreed to. Discard the queue before every input() call, not just the
    # first: an unrecognized answer re-prompts, and anything typed while that
    # unrecognized answer was being read is queued for the *next* prompt the
    # user hasn't seen yet either (#182) -- the same #163 shape, one prompt
    # later. Best-effort by design: a non-tty stdin (piped input, tests) has
    # no terminal queue and nothing to discard, and a failure to flush must
    # never be what stops a confirmation from being asked.
    while True:
        try:
            termios.tcflush(sys.stdin, termios.TCIFLUSH)
        except Exception:
            pass
        answer = input(f"{prompt} (y/n): ").strip().lower()
        if answer == "y":
            return True
        if answer == "n":
            return False


def build_plan_summary(planned: list[PlannedResource]) -> str:
    return json.dumps(
        [
            {
                "resource_key": pr.entry.resource_key,
                "action": pr.entry.action.value,
                "rationale": pr.entry.rationale,
                "likely_replace": pr.entry.likely_replace,
            }
            for pr in planned
        ]
    )


def _raise_if_review_blocked(review: PlanReview) -> None:
    blocking = [flag for flag in review.flags if flag.severity == PlanReviewSeverity.BLOCK]
    if blocking:
        raise PlanBlockedError(
            "plan review blocked: "
            + "; ".join(f"{flag.resource_key}: {flag.concern}" for flag in blocking)
        )
    if not review.safe_to_proceed:
        # A schema-compliant response could set safe_to_proceed=false without
        # attaching a block-severity flag naming why -- prompts/review_plan.md
        # instructs against this, but nothing in PLAN_REVIEW_SCHEMA structurally
        # forbids it. Same defensive stance driver_gen.py already takes on
        # DriverReview.approved: never trust "no block flag" alone as a pass.
        raise PlanBlockedError(
            "plan review declined to approve (safe_to_proceed=false) without naming a "
            "block-severity flag"
        )


def apply_plan(
    planned: list[PlannedResource],
    *,
    state_path: Path = state.DEFAULT_STATE_PATH,
    yes: bool = False,
    confirm: ConfirmFn | None = None,
    on_review: OnReviewFn | None = None,
    client: anthropic.Anthropic | None = None,
    llm_config: LLMConfig | None = None,
) -> ApplyResult:
    st = state.load(state_path)
    confirm_fn = confirm or default_confirm
    on_review_fn = on_review or (lambda flags: None)
    review_flags: list[PlanReviewFlag] = []

    _batch_plan_review(planned, review_flags, on_review_fn, client=client, llm_config=llm_config)

    if not yes and not confirm_fn("Apply this plan?"):
        return ApplyResult(executed=[], review_flags=review_flags, aborted=True)

    executed: list[PlanEntry] = []

    for pr in planned:
        if pr.entry.action == PlanAction.NO_OP:
            continue

        if pr.entry.action == PlanAction.CREATE:
            _apply_create(pr, st)
            executed.append(pr.entry)

        elif pr.entry.action == PlanAction.UPDATE:
            # The two handlers below are siblings on purpose. Python never
            # re-enters a sibling handler, so the delete()/create() calls
            # _replace_resource() makes from inside the first one are NOT
            # covered by `except Exception` -- they surface as "delete"/
            # "create" rather than being relabelled "update". Flattening
            # these, or moving those calls under a broader try, changes
            # which exceptions get wrapped; see the two
            # test_replace_*_failure_reports_* tests.
            replaced = False
            update_start = time.monotonic()
            try:
                raw = pr.driver.update(
                    pr.state_entry.id, pr.state_entry.attributes, pr.desired_params, pr.credentials
                )
            except DriverUpdateNotSupported:
                replaced = True
                if not pr.entry.likely_replace:
                    _replace_review(
                        pr, review_flags, on_review_fn, client=client, llm_config=llm_config
                    )
                    if not confirm_fn(f"Replace {pr.entry.resource_key}?"):
                        return ApplyResult(
                            executed=executed, review_flags=review_flags, aborted=True
                        )
                raw = _replace_resource(pr, st, state_path=state_path)
            except Exception as exc:
                _log_driver_outcome(
                    pr.provider,
                    pr.resource_type,
                    "update",
                    log.elapsed_ms(update_start),
                    outcome="error",
                )
                raise DriverExecutionError(pr.provider, pr.resource_type, "update", exc) from exc

            if not replaced:
                _log_driver_outcome(
                    pr.provider,
                    pr.resource_type,
                    "update",
                    log.elapsed_ms(update_start),
                    outcome="success",
                )

            executed.append(_record_update(pr, st, raw, replaced=replaced))

        elif pr.entry.action == PlanAction.DESTROY:
            _apply_destroy(pr, st, state_path=state_path)
            executed.append(pr.entry)
            continue

        state.save(st, state_path)

    return ApplyResult(executed=executed, review_flags=review_flags, aborted=False)


def _batch_plan_review(
    planned: list[PlannedResource],
    review_flags: list[PlanReviewFlag],
    on_review_fn: OnReviewFn,
    *,
    client: anthropic.Anthropic | None,
    llm_config: LLMConfig | None,
) -> None:
    needs_review = any(
        pr.entry.action == PlanAction.DESTROY
        or (pr.entry.action == PlanAction.UPDATE and pr.entry.likely_replace)
        for pr in planned
    )
    if not needs_review:
        return

    review = llm.review_plan(build_plan_summary(planned), client=client, llm_config=llm_config)
    blocked = not review.safe_to_proceed or any(
        flag.severity == PlanReviewSeverity.BLOCK for flag in review.flags
    )
    (logger.warning if blocked else logger.info)(
        "",
        extra={"safe_to_proceed": review.safe_to_proceed, "flags_count": len(review.flags)},
    )
    _raise_if_review_blocked(review)
    _extend_and_notify(review, review_flags, on_review_fn)


# Notifies with only *this* review's non-BLOCK flags, never the accumulated
# `review_flags` -- a later single-resource review is a different review about
# one resource and must surface as its own thing before its own confirmation.
# The notification is unconditional for both callers, including under yes=True:
# --yes suppresses a *prompt*, never the record of what a gate #2 review said
# (#166), and the caller must see it before whatever confirmation follows
# rather than after apply_plan() has returned.
#
# Which prompt --yes actually suppresses differs by caller, so do not read a
# general rule out of this: it skips _batch_plan_review()'s "Apply this plan?",
# but the "Replace <key>?" confirmation after _replace_review() is deliberately
# NOT gated on yes (specs/orchestrator.md's judgment call on the stricter
# single-resource gate). Anyone "fixing" that asymmetry removes a safety
# prompt.
def _extend_and_notify(
    review: PlanReview, review_flags: list[PlanReviewFlag], on_review_fn: OnReviewFn
) -> None:
    new_flags = [flag for flag in review.flags if flag.severity != PlanReviewSeverity.BLOCK]
    review_flags.extend(new_flags)
    on_review_fn(new_flags)


def _apply_create(pr: PlannedResource, st: State) -> None:
    raw = _call_driver(
        pr.driver.create,
        pr.provider,
        pr.resource_type,
        "create",
        pr.name,
        pr.desired_params,
        pr.credentials,
    )
    new_id, attrs = _pop_id(raw, pr.provider, pr.resource_type, "create")
    now = datetime.now(UTC)
    st.resources[pr.entry.resource_key] = _new_state_entry(pr, new_id, attrs, now)


def _replace_review(
    pr: PlannedResource,
    review_flags: list[PlanReviewFlag],
    on_review_fn: OnReviewFn,
    *,
    client: anthropic.Anthropic | None,
    llm_config: LLMConfig | None,
) -> None:
    modified_entry = pr.entry.model_copy(update={"likely_replace": True})
    single_summary = build_plan_summary([dataclasses.replace(pr, entry=modified_entry)])
    single_review = llm.review_plan(single_summary, client=client, llm_config=llm_config)
    _raise_if_review_blocked(single_review)
    _extend_and_notify(single_review, review_flags, on_review_fn)


def _replace_resource(pr: PlannedResource, st: State, *, state_path: Path) -> dict[str, Any]:
    _call_driver(
        pr.driver.delete,
        pr.provider,
        pr.resource_type,
        "delete",
        pr.state_entry.id,
        pr.credentials,
    )
    # The old resource is now verifiably gone on the CSP side -- drop it from
    # state and save immediately, before attempting create(). If create() then
    # fails, state.json correctly reflects "not tracked" rather than stale
    # id/attributes for a resource that no longer exists (the same
    # drifted_missing self-healing this checkpoint pre-empts would otherwise be
    # needed to detect it on the next refresh).
    _require_tracked(st, pr.entry.resource_key)
    del st.resources[pr.entry.resource_key]
    state.save(st, state_path)
    return _call_driver(
        pr.driver.create,
        pr.provider,
        pr.resource_type,
        "create",
        pr.name,
        pr.desired_params,
        pr.credentials,
    )


def _record_update(
    pr: PlannedResource, st: State, raw: dict[str, Any], *, replaced: bool
) -> PlanEntry:
    operation = "create" if replaced else "update"
    new_id, attrs = _pop_id(raw, pr.provider, pr.resource_type, operation)
    now = datetime.now(UTC)
    if replaced:
        st.resources[pr.entry.resource_key] = _new_state_entry(pr, new_id, attrs, now)
    else:
        existing = _require_tracked(st, pr.entry.resource_key)
        existing.id = new_id
        existing.attributes = attrs
        existing.driver = pr.driver_info
        existing.last_applied_at = now
        existing.last_refreshed_at = now
        existing.aiform_md_sha256 = pr.current_aiform_md_sha256
        existing.depends_on = list(pr.depends_on)
    # entry.likely_replace reflects the plan-time prediction; report what
    # actually happened instead, in both directions -- a predicted replace that
    # update() handled in place must not be reported as a replace just because
    # the prediction said so, the same way an unpredicted replace must not be
    # under-reported.
    return pr.entry.model_copy(update={"likely_replace": replaced})


def _apply_destroy(pr: PlannedResource, st: State, *, state_path: Path) -> None:
    if pr.state_entry is not None:
        driver = load_driver(pr.provider, pr.resource_type)
        try:
            credentials = config.resolve_credentials(pr.provider)
        except RuntimeError as exc:
            raise PlanBlockedError(str(exc)) from exc
        _call_driver(
            driver.delete,
            pr.provider,
            pr.resource_type,
            "delete",
            pr.state_entry.id,
            credentials,
        )
        _require_tracked(st, pr.entry.resource_key)
        del st.resources[pr.entry.resource_key]
    state.save(st, state_path)
    move_to_trash(pr.aiform_md_path)


def _split_aiform_md_suffix(name: str) -> tuple[str, str]:
    if name.endswith(".aiform.md"):
        return name[: -len(".aiform.md")], ".aiform.md"
    return Path(name).stem, Path(name).suffix


def move_to_trash(path: Path, *, trash_dir: Path = TRASH_DIR) -> Path:
    trash_dir.mkdir(parents=True, exist_ok=True)
    timestamp = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    stem, suffix = _split_aiform_md_suffix(path.name)

    destination = trash_dir / f"{timestamp}-{stem}{suffix}"
    counter = 2
    while destination.exists():
        destination = trash_dir / f"{timestamp}-{stem}-{counter}{suffix}"
        counter += 1

    shutil.move(str(path), str(destination))
    return destination
