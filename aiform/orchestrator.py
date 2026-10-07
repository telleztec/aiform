# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

import copy
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
import urllib.error
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anthropic

from aiform import config, graph, llm, log, parser, planner, references, state
from aiform.driver import (
    ID_TYPES,
    DriverUpdateNotSupported,
    ReferenceField,
    ResourceDriver,
    values_at,
)
from aiform.driver import reserved_tags as deployment_tags
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


# The referenceable namespace: a tracked resource's attributes plus its `id`.
# `id` is not in `attributes` -- _pop_id() above moves it to StateEntry.id --
# but it is the most useful cross-resource value, so it is merged back in here
# rather than every caller remembering to.
def referenceable(st: State) -> dict[str, dict[str, Any]]:
    return {key: {**entry.attributes, "id": entry.id} for key, entry in st.resources.items()}


# `volatile` holds the keys this run is about to give new attribute values --
# a drifted resource being recreated, or one whose update may turn into a
# delete+create. Their CURRENT attributes are still sitting in state, and
# offering those to a dependent is the stale-DNS bug this whole feature exists
# to fix: the dependent would resolve to the doomed address, diff clean, plan
# NO_OP, and be skipped by apply_plan() before the apply-time re-resolve could
# ever correct it. Withholding them instead leaves the reference unresolved,
# which routes the dependent through unresolved_entry() and gets it resolved
# for real during the apply, after the target has its new value.
def _resolve_params(
    key: str, params: dict[str, Any], st: State, volatile: set[str], replaced: set[str]
) -> tuple[dict[str, Any], list[str]]:
    try:
        return references.resolve(params, referenceable(st), volatile=volatile, replaced=replaced)
    except references.ReferenceResolutionError as exc:
        raise PlanBlockedError(f"{key}: {exc}") from exc


# Any CREATE or UPDATE, deliberately -- not only an UPDATE flagged
# likely_replace. "This target's attributes may differ after the apply" is the
# question, and an update is by definition an answer of yes; likely_replace only
# describes *how* the value changes, and it is the model's advisory guess rather
# than a fact, so gating on it missed two real cases: an update the model called
# in-place that driver.update() then refuses (delete + create, new address), and
# the middle of a chain, whose own action is the deterministic UPDATE this
# mechanism produces and which therefore has likely_replace=False by
# construction. Both left a dependent pointing at a dead host for a plan cycle.
#
# The cost of being broad is a dependent update that rewrites an identical
# value, at zero LLM cost, and it converges: once the target is applied its next
# plan is NO_OP, so nothing is volatile and the dependent is NO_OP too.
def _will_get_new_attributes(entry: PlanEntry) -> bool:
    return entry.action in (PlanAction.CREATE, PlanAction.UPDATE)


# The subset of the above whose CURRENT value says nothing about the value to
# come, because the resource itself is being made again. It matters for exactly
# one rule: a reference to an attribute that is currently unset. For a recreate
# that is fine and expected -- a drifted VM's ipv4_address is None
# precisely because the VM is gone -- while for an in-place update the
# value stays unset, so refusing at plan time beats failing mid-apply.
def _will_be_recreated(entry: PlanEntry) -> bool:
    return entry.action == PlanAction.CREATE


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
        depends_on=list(pr.depends_on),
        reference_edges=copy.deepcopy(pr.reference_edges),
    )


def discover_files(paths: list[Path] | None, *, cwd: Path = Path(".")) -> list[Path]:
    if paths:
        return list(paths)
    return sorted(cwd.glob("*.aiform.md"))


def is_delete_marked(path: Path) -> bool:
    return path.name.startswith("AIFORM-DELETE-")


def driver_path(provider: str, resource_type: str) -> Path:
    return DRIVERS_DIR / provider / f"{resource_type}.py"


def load_driver(
    provider: str, resource_type: str, reserved_tags: Sequence[str] = ()
) -> ResourceDriver:
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
    return module.Driver(reserved_tags=reserved_tags)


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


def refresh_state(*, state_path: Path = state.DEFAULT_STATE_PATH, deployment: str) -> State:
    st = state.load(state_path, deployment=deployment)
    driver_cache: dict[tuple[str, str], ResourceDriver] = {}
    credentials_cache: dict[str, dict[str, str]] = {}

    for entry in st.resources.values():
        driver_key = (entry.provider, entry.resource_type)
        if driver_key not in driver_cache:
            driver_cache[driver_key] = load_driver(
                entry.provider, entry.resource_type, reserved_tags=deployment_tags(st.deployment)
            )
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
    reference_edges: dict[str, list[str]] = dataclasses.field(default_factory=dict)
    # desired_params is resolved as far as plan time could manage and is what
    # the diff and the plan display read. raw_params keeps the references
    # intact, because apply re-resolves from scratch against state as it
    # stands at the moment of each driver call.
    raw_params: dict[str, Any] = dataclasses.field(default_factory=dict)
    unresolved_references: list[str] = dataclasses.field(default_factory=list)
    dropped_dependents: list[str] = dataclasses.field(default_factory=list)
    repairs: list[str] = dataclasses.field(default_factory=list)


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
# A reference implies the edge, so the two sources are unioned before the
# per-target classification below ever runs -- a reference adds targets to
# that table, it does not add rules to it. Declared order is preserved and
# reference-only targets are appended sorted, so which target a
# PlanBlockedError names first stays deterministic and unchanged from Phase 1.
def _dependency_targets(spec: ResourceSpec, key: str) -> list[str]:
    declared = list(spec.depends_on)
    try:
        referenced = references.reference_targets(spec.params)
    except references.ReferenceResolutionError as exc:
        raise PlanBlockedError(f"{key}: {exc}") from exc
    return declared + sorted(referenced - set(declared))


def _reference_edges(params: dict[str, Any]) -> dict[str, list[str]]:
    attributes: dict[str, set[str]] = {}
    for found in references.find_references(params).values():
        for reference in found:
            attributes.setdefault(reference.target_key, set()).add(reference.attribute)
    return {target: sorted(names) for target, names in sorted(attributes.items())}


def _resolve_dependency_edges(discovered: list[_DiscoveredFile], st: State) -> dict[str, set[str]]:
    delete_marked_keys = {entry.key for entry in discovered if entry.delete_marked}
    live_keys = {entry.key for entry in discovered if not entry.delete_marked}

    edges: dict[str, set[str]] = {entry.key: set() for entry in discovered}
    for entry in discovered:
        for target in _dependency_targets(entry.spec, entry.key):
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


def _reverse_topological_breaking_cycles(
    keys: set[str], edges: dict[str, set[str]]
) -> tuple[list[str], list[str]]:
    edges = {key: set(targets) for key, targets in edges.items()}
    warnings: list[str] = []
    while True:
        try:
            return list(reversed(graph.topological_order(keys, edges))), warnings
        except graph.CycleError as exc:
            dependent, target = exc.path[0], exc.path[1]
            edges[dependent].discard(target)
            warnings.append(
                f"dependency cycle in state: {' -> '.join(exc.path)}; dropping the edge from "
                f"{dependent} to {target}, so the destroy order no longer guarantees that "
                f"{dependent} is destroyed before {target}, which it depends on"
            )


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
    deployment: str,
    client: anthropic.Anthropic | None = None,
    llm_config: LLMConfig | None = None,
) -> tuple[list[PlannedResource], list[str]]:
    st = state.load(state_path, deployment=deployment)
    files = discover_files(paths, cwd=cwd)
    ordered_files = _order_files(files, st)
    repairs, repair_warnings = _plan_marker_repairs(ordered_files, st)

    driver_cache: dict[tuple[str, str], tuple[ResourceDriver, DriverInfo]] = {}
    credentials_cache: dict[str, dict[str, str]] = {}

    planned: list[PlannedResource] = []
    covered_keys: set[str] = set()
    # Accumulated in topological order, so by the time a dependent is planned
    # every target of its has already been classified.
    volatile: set[str] = set()
    replaced: set[str] = set()

    for path in ordered_files:
        if is_delete_marked(path):
            pr = _plan_delete_marked(path, st)
        else:
            pr = _plan_one(
                path,
                st,
                driver_cache,
                credentials_cache,
                volatile,
                replaced,
                client=client,
                llm_config=llm_config,
            )
        if _will_get_new_attributes(pr.entry):
            volatile.add(pr.entry.resource_key)
        if _will_be_recreated(pr.entry):
            replaced.add(pr.entry.resource_key)
        covered_keys.add(pr.entry.resource_key)
        planned.append(pr)

    state.save(st, state_path)

    return _repairs_before_destroys(planned, repairs), (
        _warnings_for_uncovered(st, covered_keys, paths) + repair_warnings
    )


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
        depends_on=_dependency_targets(resource_spec, key),
    )


def _plan_one(
    path: Path,
    st: State,
    driver_cache: dict[tuple[str, str], tuple[ResourceDriver, DriverInfo]],
    credentials_cache: dict[str, dict[str, str]],
    volatile: set[str],
    replaced: set[str],
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

    # Resolved before the diff, not after: plan_resource() compares
    # desired_params against live attributes, so an unresolved "${...}" literal
    # would never equal a real value and would defeat the no-op short-circuit
    # permanently, billing a categorization on every future plan. Because the
    # plan is walked in topological order, a target that is also in this run
    # has already been refreshed by the time its dependent resolves.
    desired_params, unresolved = _resolve_params(key, resource_spec.params, st, volatile, replaced)

    entry, params_agree = _decide_action(
        key,
        resource_spec,
        desired_params,
        unresolved,
        parsed,
        state_entry,
        driver=driver,
        credentials=credentials,
        previous_hash=previous_hash,
        client=client,
        llm_config=llm_config,
    )
    depends_on = _dependency_targets(resource_spec, key)
    reference_edges = _reference_edges(resource_spec.params)

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
    # The unioned list, not just the declared one: a reference-derived edge has
    # to reach state or `plan destroy` from state alone would tear a target
    # down before the resource pointing at it.
    if state_entry is not None:
        state_entry.depends_on = list(depends_on)
        state_entry.reference_edges = copy.deepcopy(reference_edges)

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
        desired_params=desired_params,
        aiform_md_path=path,
        current_aiform_md_sha256=parsed.aiform_md_sha256,
        driver=driver,
        driver_info=driver_info,
        credentials=credentials,
        state_entry=state_entry,
        depends_on=depends_on,
        reference_edges=reference_edges,
        raw_params=resource_spec.params,
        unresolved_references=unresolved,
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
        driver = load_driver(provider, resource_type, reserved_tags=deployment_tags(st.deployment))
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
    desired_params: dict[str, Any],
    unresolved: list[str],
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
        elif unresolved:
            # Tracked, present, but a reference target is being (re)created in
            # this same run, so the desired value is not knowable yet. There is
            # no honest diff to categorize -- handing the model the literal
            # "${...}" would invite it to categorize the placeholder -- so the
            # answer is produced deterministically instead, at zero cost.
            #
            # params_agree stays False so _plan_one() does not record the
            # .aiform.md hash on a run whose params were never fully known
            # (the #195 invariant).
            params_agree = False
            entry = planner.unresolved_entry(key, unresolved)
        else:
            entry, params_agree = planner.plan_resource(
                key,
                current_attributes,
                desired_params,
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


# Both destroy producers pass raw, unfiltered depends_on lists (unlike
# _order_files' _resolve_dependency_edges above, which restricts edges to
# the run's own keys before calling _topological). A target is either an
# edge (it's one of this producer's own nodes), silently resolvable (it
# exists elsewhere and nothing here needs to order against it), or
# dangling (it resolves nowhere at all). Collapsing "silently resolvable"
# and "dangling" into one case would either warn on the everyday case --
# `plan destroy one-file.aiform.md` naming a dependency tracked in state
# but not in this run -- or silently proceed on a genuinely broken
# reference; the distinction is the whole point.
def _classify_destroy_edges(
    raw_edges: dict[str, set[str]], node_keys: set[str], *, resolvable_elsewhere: set[str]
) -> tuple[dict[str, set[str]], list[tuple[str, str]]]:
    edges: dict[str, set[str]] = {}
    dangling: list[tuple[str, str]] = []
    for key, targets in raw_edges.items():
        kept = set()
        for target in targets:
            if target in node_keys:
                kept.add(target)
            elif target not in resolvable_elsewhere:
                dangling.append((key, target))
        edges[key] = kept
    return edges, dangling


def _dangling_targets_reason(dangling: list[tuple[str, str]]) -> str:
    pairs = "; ".join(f"{key} depends on {target!r}" for key, target in sorted(dangling))
    return (
        f"cannot destroy: {pairs} -- neither in this run nor tracked in state; "
        "pass --force to drop these edges and destroy anyway"
    )


def _resolve_dangling_targets(dangling: list[tuple[str, str]], *, force: bool) -> list[str]:
    if not dangling:
        return []
    if not force:
        raise PlanBlockedError(_dangling_targets_reason(dangling))
    return [
        f"{key}: depends on {target!r}, which is neither in this run nor tracked in "
        "state -- dropping the edge (--force)"
        for key, target in sorted(dangling)
    ]


# _build_destroy_plan_from_paths()'s own hazard, the mirror image of a
# dangling target: a resource NOT in this run (so absent from node_keys and
# never seen by _classify_destroy_edges above, which only looks at edges
# OUT of the run's own nodes) whose persisted depends_on points INTO this
# run. Destroying the target would silently orphan it. Read from the
# dependent's persisted StateEntry.depends_on, not its .aiform.md -- that
# file may not exist, may not be part of this run, and re-parsing every
# .aiform.md on disk to answer this would be a new filesystem scan on the
# destroy path.
def _reverse_dependents(node_keys: set[str], st: State) -> list[tuple[str, str]]:
    orphaned: list[tuple[str, str]] = []
    for key, entry in st.resources.items():
        if key in node_keys:
            continue
        for target in entry.depends_on:
            if target in node_keys:
                orphaned.append((key, target))
    return orphaned


_FORCE_HINT = "pass --force to drop these edges and destroy anyway"


def _orphaned_dependents_reason(orphaned: list[tuple[str, str]], *, hint: str = _FORCE_HINT) -> str:
    pairs = "; ".join(
        f"{dependent} depends on {target!r}" for dependent, target in sorted(orphaned)
    )
    return (
        f"cannot destroy: {pairs} -- each dependent is not in this run and would be "
        f"orphaned (read from recorded state; run `aiform plan` first if this edge is "
        f"stale); {hint}"
    )


# Dependents repaired, not refused, when their target is destroyed: the
# dependent's own driver declares (REFERENCE_FIELDS) a top-level list holding
# the target's native id, and its driver can drop that id from the live
# resource. A declared nested path that still names the target blocks the
# repair, because stripping only the top-level list would leave that id
# behind. Ids compare as strings because a live read need not return them in
# the declared type.
def _declared_fields(
    provider: str, resource_type: str, driver: ResourceDriver, target_type: tuple[str, str]
) -> list[ReferenceField]:
    for field in driver.REFERENCE_FIELDS:
        if field.id_type not in ID_TYPES:
            raise PlanBlockedError(
                f"cannot plan: the driver for (provider={provider!r}, "
                f"resource_type={resource_type!r}) declares id_type {field.id_type!r} for "
                f"REFERENCE_FIELDS path {field.path!r}, expected one of "
                f"{', '.join(map(repr, ID_TYPES))}; fix the declaration in that driver"
            )
    return [field for field in driver.REFERENCE_FIELDS if field.target == target_type]


def _declared_id(target_id: str, id_type: str) -> int | str | None:
    if id_type == "string":
        return target_id
    if target_id.isascii() and target_id.isdigit():
        return int(target_id)
    return None


def _nested_path_naming(
    attributes: dict[str, Any], fields: list[ReferenceField], target_id: str
) -> str | None:
    for field in fields:
        if field.top_level:
            continue
        form = str(_declared_id(target_id, field.id_type))
        if any(str(listed) == form for listed in values_at(attributes, field.path)):
            return field.path
    return None


def _is_repairable(dependent: StateEntry, target_key: str, st: State) -> bool:
    target = st.resources.get(target_key)
    if target is None:
        return False
    try:
        driver = load_driver(
            dependent.provider,
            dependent.resource_type,
            reserved_tags=deployment_tags(st.deployment),
        )
    except Exception:
        return False
    fields = _declared_fields(
        dependent.provider,
        dependent.resource_type,
        driver,
        (target.provider, target.resource_type),
    )
    if not any(field.top_level for field in fields):
        return False
    if any(_declared_id(target.id, field.id_type) is None for field in fields):
        return False
    return _nested_path_naming(dependent.attributes, fields, target.id) is None


def _split_reverse_dependents(
    orphaned: list[tuple[str, str]], st: State
) -> tuple[dict[str, list[str]], list[tuple[str, str]]]:
    repairs: dict[str, list[str]] = {}
    unrepairable: list[tuple[str, str]] = []
    for dependent, target in sorted(orphaned):
        if _is_repairable(st.resources[dependent], target, st):
            repairs.setdefault(dependent, []).append(target)
        else:
            unrepairable.append((dependent, target))
    return repairs, unrepairable


def _repair_planned(dependent_key: str, targets: list[str], st: State) -> PlannedResource:
    entry = st.resources[dependent_key]
    return PlannedResource(
        entry=planner.repair_entry(dependent_key, targets),
        provider=entry.provider,
        resource_type=entry.resource_type,
        name=entry.name,
        desired_params={},
        aiform_md_path=Path(entry.aiform_md_path),
        current_aiform_md_sha256=None,
        driver=None,
        driver_info=None,
        credentials=None,
        state_entry=entry,
        depends_on=list(entry.depends_on),
        repairs=targets,
    )


def _repair_notice(dependent_key: str, targets: list[str]) -> str:
    return (
        f"{dependent_key}: repaired before the destroy -- its .aiform.md is not edited and "
        f"still names {', '.join(targets)}, so the next `aiform plan` will flag it until "
        "you remove the reference"
    )


def _repairs_before_destroys(
    planned: list[PlannedResource], repairs: list[PlannedResource]
) -> list[PlannedResource]:
    first_destroy = next(
        (i for i, pr in enumerate(planned) if pr.entry.action == PlanAction.DESTROY), len(planned)
    )
    return planned[:first_destroy] + repairs + planned[first_destroy:]


def _plan_marker_repairs(
    ordered_files: list[Path], st: State
) -> tuple[list[PlannedResource], list[str]]:
    discovered = [_discover_one(path) for path in ordered_files]
    run_keys = {entry.key for entry in discovered}
    marked = {entry.key for entry in discovered if entry.delete_marked}
    orphaned = [
        (dependent, target)
        for dependent, target in _reverse_dependents(marked, st)
        if dependent not in run_keys
    ]
    repairs, unrepairable = _split_reverse_dependents(orphaned, st)
    if unrepairable:
        raise PlanBlockedError(
            _orphaned_dependents_reason(
                unrepairable,
                hint="destroy it with `aiform plan destroy <file> --force` to drop these edges",
            )
        )
    return (
        [_repair_planned(dependent, targets, st) for dependent, targets in repairs.items()],
        [_repair_notice(dependent, targets) for dependent, targets in repairs.items()],
    )


def _resolve_reverse_dependents(orphaned: list[tuple[str, str]], *, force: bool) -> list[str]:
    if not orphaned:
        return []
    if not force:
        raise PlanBlockedError(_orphaned_dependents_reason(orphaned))
    return [
        f"{dependent}: depends on {target!r}, which is being destroyed in this run -- "
        "dropping the edge (--force)"
        for dependent, target in sorted(orphaned)
    ]


# The operational direction the dependency model cannot express (#235): the
# configuration edge says a firewall depends on its VM, so a destroy
# deletes the firewall first, but it is the VM that depends on the
# firewall for filtering. Until that direction is modelled, the one known
# pairing is named here.
_PROTECTS = {("digitalocean", "firewall"): ({("digitalocean", "compute")}, "unfiltered")}


def _exposure_warnings(planned: list[PlannedResource]) -> list[str]:
    destroyed = {
        pr.entry.resource_key: pr for pr in planned if pr.entry.action == PlanAction.DESTROY
    }
    position = {key: index for index, key in enumerate(destroyed)}
    warnings: list[str] = []
    for key, pr in destroyed.items():
        protected_types, lapse = _PROTECTS.get((pr.provider, pr.resource_type), (set(), ""))
        for target_key in pr.depends_on:
            target = destroyed.get(target_key)
            # A cycle broken in state drops an edge from the order but not from
            # depends_on, so the protector can end up after its target.
            if (
                target is not None
                and (target.provider, target.resource_type) in protected_types
                and position[key] < position[target_key]
            ):
                warnings.append(
                    f"{key} protects {target_key}, and the plan destroys both: "
                    f"{key} goes first, so {target_key} runs {lapse} until its own "
                    "delete succeeds (indefinitely if that delete fails)"
                )
    return warnings


def build_destroy_plan(
    paths: list[Path] | None = None,
    *,
    state_path: Path = state.DEFAULT_STATE_PATH,
    deployment: str,
    force: bool = False,
) -> tuple[list[PlannedResource], list[str]]:
    st = state.load(state_path, deployment=deployment)
    if paths:
        planned, warnings = _build_destroy_plan_from_paths(paths, st, force=force)
    else:
        planned, warnings = _build_destroy_plan_from_state(st, force=force)
    return planned, [*warnings, *_exposure_warnings(planned)]


def _build_destroy_plan_from_paths(
    paths: list[Path], st: State, *, force: bool
) -> tuple[list[PlannedResource], list[str]]:
    paths = _dedupe_normalized_paths(paths)
    discovered = [_discover_one(path) for path in paths]
    _check_duplicate_keys(discovered)

    node_keys = {entry.key for entry in discovered}
    # _dependency_targets(), not spec.depends_on: a reference implies the edge on
    # this path too. The state-driven producer below gets this for free by reading
    # the persisted, already-unioned StateEntry.depends_on; this one derives its
    # edges from the files, so it has to union them itself.
    raw_edges = {entry.key: set(_dependency_targets(entry.spec, entry.key)) for entry in discovered}
    edges, dangling = _classify_destroy_edges(
        raw_edges, node_keys, resolvable_elsewhere=set(st.resources)
    )
    warnings = _resolve_dangling_targets(dangling, force=force)
    repairs, orphaned = _split_reverse_dependents(_reverse_dependents(node_keys, st), st)
    warnings += _resolve_reverse_dependents(orphaned, force=force)
    warnings += [_repair_notice(dependent, targets) for dependent, targets in repairs.items()]

    order = _reverse_topological(node_keys, edges)
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
                depends_on=_dependency_targets(resource_spec, key),
                dropped_dependents=sorted(
                    dependent for dependent, target in orphaned if target == key
                ),
            )
        )
    repair_prs = [_repair_planned(dependent, targets, st) for dependent, targets in repairs.items()]
    return _repairs_before_destroys(planned, repair_prs), warnings


def _build_destroy_plan_from_state(
    st: State, *, force: bool
) -> tuple[list[PlannedResource], list[str]]:
    node_keys = set(st.resources)
    raw_edges = {key: set(entry.depends_on) for key, entry in st.resources.items()}
    edges, dangling = _classify_destroy_edges(raw_edges, node_keys, resolvable_elsewhere=set())
    warnings = _resolve_dangling_targets(dangling, force=force)

    order, cycle_warnings = _reverse_topological_breaking_cycles(node_keys, edges)
    warnings.extend(cycle_warnings)

    planned: list[PlannedResource] = []
    for key in order:
        state_entry = st.resources[key]
        if not Path(state_entry.aiform_md_path).exists():
            warnings.append(
                f"{key}: tracked file {state_entry.aiform_md_path} is missing; destroying "
                "from state and skipping the trash move"
            )
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
    return planned, warnings


ConfirmFn = Callable[[str], bool]
OnReviewFn = Callable[[list[PlanReviewFlag]], None]


@dataclass
class ApplyResult:
    executed: list[PlanEntry]
    review_flags: list[PlanReviewFlag]
    aborted: bool


# Attached to an exception escaping apply_plan()'s execute loop as
# `apply_progress`, instead of wrapping it in a new type: every handler and
# test that catches DriverExecutionError/PlanBlockedError keeps working.
@dataclass(frozen=True)
class ApplyProgress:
    applied: list[PlanEntry]
    failed: PlanEntry
    not_run: list[PlanEntry]
    repairs: dict[str, list[str]] = dataclasses.field(default_factory=dict)


# Persistent enough to ride out a rate limit or a 5xx blip, bounded so a
# resource that will never delete does not hang a script. 65s of waiting.
DESTROY_RETRY_ATTEMPTS = 4
DESTROY_RETRY_DELAYS_SECONDS = (5, 15, 45)


def _read_input(prompt: str) -> str:
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
    try:
        termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except Exception:
        pass
    return input(prompt)


def read_answer(prompt: str) -> str:
    try:
        return _read_input(prompt)
    except EOFError:
        return ""


def default_confirm(prompt: str) -> bool:
    while True:
        try:
            answer = _read_input(f"{prompt} (y/n): ").strip().lower()
        except EOFError:
            return False
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
    deployment: str,
    yes: bool = False,
    confirm: ConfirmFn | None = None,
    on_review: OnReviewFn | None = None,
    client: anthropic.Anthropic | None = None,
    llm_config: LLMConfig | None = None,
    retry_destroy: bool = False,
) -> ApplyResult:
    st = state.load(state_path, deployment=deployment)
    confirm_fn = confirm or default_confirm
    on_review_fn = on_review or (lambda flags: None)
    review_flags: list[PlanReviewFlag] = []

    _batch_plan_review(planned, review_flags, on_review_fn, client=client, llm_config=llm_config)

    if not yes and not confirm_fn("Apply this plan?"):
        return ApplyResult(executed=[], review_flags=review_flags, aborted=True)

    if not yes:
        for pr in planned:
            if pr.repairs and not confirm_fn(_repair_prompt(pr, st)):
                return ApplyResult(executed=[], review_flags=review_flags, aborted=True)

    executed: list[PlanEntry] = []

    position = 0
    try:
        for position, pr in enumerate(planned):  # noqa: B007 -- read by the except below
            if pr.repairs:
                _apply_repair(pr, st, state_path=state_path)
                executed.append(pr.entry)
                continue

            match pr.entry.action:
                case PlanAction.NO_OP:
                    continue

                case PlanAction.CREATE:
                    _apply_create(pr, st)
                    executed.append(pr.entry)

                case PlanAction.UPDATE:
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
                    desired = _apply_params(pr, st)
                    try:
                        raw = pr.driver.update(
                            pr.state_entry.id, pr.state_entry.attributes, desired, pr.credentials
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
                        raw = _replace_resource(pr, st, state_path=state_path, desired=desired)
                    except Exception as exc:
                        _log_driver_outcome(
                            pr.provider,
                            pr.resource_type,
                            "update",
                            log.elapsed_ms(update_start),
                            outcome="error",
                        )
                        raise DriverExecutionError(
                            pr.provider, pr.resource_type, "update", exc
                        ) from exc

                    if not replaced:
                        _log_driver_outcome(
                            pr.provider,
                            pr.resource_type,
                            "update",
                            log.elapsed_ms(update_start),
                            outcome="success",
                        )

                    executed.append(_record_update(pr, st, raw, replaced=replaced))

                case PlanAction.DESTROY:
                    _apply_destroy(pr, st, state_path=state_path, retry=retry_destroy)
                    executed.append(pr.entry)
                    continue

            state.save(st, state_path)
    except Exception as exc:
        exc.apply_progress = _progress_at(planned, executed, position)
        raise

    return ApplyResult(executed=executed, review_flags=review_flags, aborted=False)


def _progress_at(
    planned: list[PlannedResource], executed: list[PlanEntry], position: int
) -> ApplyProgress:
    failed = planned[position].entry
    return ApplyProgress(
        # A resource whose action ran but whose state save then failed is in
        # `executed` already; it must be reported once, as failed.
        applied=[entry for entry in executed if entry.resource_key != failed.resource_key],
        failed=failed,
        not_run=[pr.entry for pr in planned[position + 1 :] if pr.entry.action != PlanAction.NO_OP],
        repairs={pr.entry.resource_key: pr.repairs for pr in planned if pr.repairs},
    )


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


# Re-resolved from the raw tree at the moment of the call, not reused from plan
# time, and deliberately for the whole tree rather than only the paths that were
# unknown then: one code path instead of two, and the value handed to a driver
# is the one live now -- which is the correct answer precisely when a target was
# replaced earlier in this same apply.
#
# Topological ordering means an unresolved path here cannot happen. The guard is
# still worth its two lines, because the alternative to raising is sending a
# literal "${...}" to the provider as a real resource value.
def _apply_params(pr: PlannedResource, st: State) -> dict[str, Any]:
    try:
        resolved, unresolved = references.resolve(pr.raw_params, referenceable(st))
    except references.ReferenceResolutionError as exc:
        # Wrapped, not left to escape: cli.py handles PlanBlockedError and does
        # not handle this, so a reference that only becomes checkable at apply
        # time -- a brand-new target's attribute name -- would otherwise reach
        # the user as a traceback with the apply half-done.
        raise PlanBlockedError(f"{pr.entry.resource_key}: {exc}") from exc
    if unresolved:
        raise PlanBlockedError(
            f"{pr.entry.resource_key}: references are still unresolved at apply time: "
            + ", ".join(unresolved)
        )
    return resolved


def _apply_create(pr: PlannedResource, st: State) -> None:
    raw = _call_driver(
        pr.driver.create,
        pr.provider,
        pr.resource_type,
        "create",
        pr.name,
        _apply_params(pr, st),
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


def _replace_resource(
    pr: PlannedResource, st: State, *, state_path: Path, desired: dict[str, Any]
) -> dict[str, Any]:
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
    # `desired` is the value computed before delete(), not recomputed here:
    # this resource has just been dropped from state, and re-resolving would
    # only differ if it referenced itself, which is a cycle and already refused.
    return _call_driver(
        pr.driver.create,
        pr.provider,
        pr.resource_type,
        "create",
        pr.name,
        desired,
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
        existing.reference_edges = copy.deepcopy(pr.reference_edges)
    # entry.likely_replace reflects the plan-time prediction; report what
    # actually happened instead, in both directions -- a predicted replace that
    # update() handled in place must not be reported as a replace just because
    # the prediction said so, the same way an unpredicted replace must not be
    # under-reported.
    return pr.entry.model_copy(update={"likely_replace": replaced})


# _resolve_reverse_dependents()'s --force warning promises "dropping the
# edge" for a resource outside the run that depends on the target being
# destroyed. Making that true is this function's job, not build_destroy_plan()'s:
# the edge only actually disappears once the target is really gone, which is
# here. Only the named dependents are touched: any other destroy route leaves
# a survivor's edge in place so the next plan refuses instead of forgetting the
# orphaning. A dependent's OWN .aiform.md may still declare the dead dependency
# -- left alone on purpose, since rewriting a file nobody asked to edit is worse
# than the next `plan` on it blocking with an actionable error.
def _prune_dependents_on(st: State, destroyed_key: str, dependents: list[str]) -> None:
    for dependent in dependents:
        entry = st.resources.get(dependent)
        if entry is None:
            continue
        if destroyed_key in entry.depends_on:
            entry.depends_on = [target for target in entry.depends_on if target != destroyed_key]
        entry.reference_edges.pop(destroyed_key, None)


def _repair_prompt(pr: PlannedResource, st: State) -> str:
    driver = load_driver(
        pr.provider, pr.resource_type, reserved_tags=deployment_tags(st.deployment)
    )
    paths: list[str] = []
    for target in pr.repairs:
        tracked = _require_tracked(st, target)
        for field in _declared_fields(
            pr.provider, pr.resource_type, driver, (tracked.provider, tracked.resource_type)
        ):
            if field.top_level and field.path not in paths:
                paths.append(field.path)
    removed = ", ".join(f"{target} (id {_require_tracked(st, target).id})" for target in pr.repairs)
    return (
        f"Repair {pr.entry.resource_key}: remove {removed} from its "
        f"{', '.join(paths)} before destroying?"
    )


def _coerce_listed_id(listed: Any, id_type: str) -> Any:
    # A live read may return an integer id as a digit string.
    if id_type == "integer" and isinstance(listed, str) and listed.isascii() and listed.isdigit():
        return int(listed)
    return listed


# The repair edits state and the provider only. It leaves the dependent's
# aiform_md_sha256 and driver alone, unlike _record_update(): its file is
# unchanged, and recording the hash would make the next plan treat that file as
# already applied.
def _apply_repair(pr: PlannedResource, st: State, *, state_path: Path) -> None:
    dependent = _require_tracked(st, pr.entry.resource_key)
    driver = load_driver(
        pr.provider, pr.resource_type, reserved_tags=deployment_tags(st.deployment)
    )
    declared: dict[str, list[ReferenceField]] = {}
    for target in pr.repairs:
        tracked = _require_tracked(st, target)
        declared[target] = _declared_fields(
            pr.provider, pr.resource_type, driver, (tracked.provider, tracked.resource_type)
        )
    removals: dict[str, set[str]] = {}
    id_types: dict[str, str] = {}
    for target, fields in declared.items():
        for field in fields:
            if field.top_level:
                forms = removals.setdefault(field.path, set())
                forms.add(str(_declared_id(_require_tracked(st, target).id, field.id_type)))
                id_types[field.path] = field.id_type
    credentials = _credentials_for(pr.provider, {})

    live, gone = refresh_resource(driver, dependent, credentials)
    attributes = live
    now = datetime.now(UTC)
    if not gone:
        for target, fields in declared.items():
            target_id = _require_tracked(st, target).id
            nested = _nested_path_naming(live, fields, target_id)
            if nested is not None:
                raise PlanBlockedError(
                    f"cannot destroy: {pr.entry.resource_key} names {target!r} "
                    f"(id {target_id}) at {nested}, which removing it from "
                    f"{', '.join(removals)} would leave behind (read live just now; the plan "
                    "was made from recorded state); destroy it with `aiform plan destroy "
                    "<file> --force` to drop the edge"
                )
    if not gone and any(
        str(listed) in forms for path, forms in removals.items() for listed in live.get(path) or []
    ):
        properties = driver.PARAM_SCHEMA.get("properties", {})
        desired = {key: value for key, value in live.items() if key in properties}
        for path, forms in removals.items():
            if path in live:
                desired[path] = [
                    _coerce_listed_id(listed, id_types[path])
                    for listed in live[path] or []
                    if str(listed) not in forms
                ]
        raw = _call_driver(
            driver.update,
            pr.provider,
            pr.resource_type,
            "update",
            dependent.id,
            live,
            desired,
            credentials,
        )
        dependent.id, attributes = _pop_id(raw, pr.provider, pr.resource_type, "update")
        dependent.last_applied_at = now
    if not gone:
        dependent.attributes = attributes
        dependent.last_refreshed_at = now
    dependent.depends_on = [target for target in dependent.depends_on if target not in pr.repairs]
    for target in pr.repairs:
        dependent.reference_edges.pop(target, None)
    state.save(st, state_path)


def _is_retryable(error: Exception) -> bool:
    if isinstance(error, urllib.error.HTTPError):
        return error.code == 429 or error.code >= 500
    return isinstance(error, urllib.error.URLError | TimeoutError | ConnectionError)


def _delete_with_retry(
    driver: ResourceDriver, pr: PlannedResource, credentials: dict[str, str], *, retry: bool
) -> None:
    attempts = DESTROY_RETRY_ATTEMPTS if retry else 1
    for attempt in range(1, attempts + 1):
        try:
            _call_driver(
                driver.delete,
                pr.provider,
                pr.resource_type,
                "delete",
                pr.state_entry.id,
                credentials,
            )
            return
        except DriverExecutionError as exc:
            if attempt == attempts or not _is_retryable(exc.original):
                raise
            delay = DESTROY_RETRY_DELAYS_SECONDS[attempt - 1]
            logger.warning(
                f"delete of {pr.entry.resource_key} failed ({exc.original}); "
                f"retrying delete of {pr.entry.resource_key} in {delay}s "
                f"(attempt {attempt} of {attempts})",
                extra={
                    "resource_key": pr.entry.resource_key,
                    "operation": "delete",
                    "retry": "destroy-all",
                    "attempt": attempt,
                    "delay_seconds": delay,
                    "error": str(exc.original),
                },
            )
            time.sleep(delay)


def _apply_destroy(
    pr: PlannedResource, st: State, *, state_path: Path, retry: bool = False
) -> None:
    if pr.state_entry is not None:
        driver = load_driver(
            pr.provider, pr.resource_type, reserved_tags=deployment_tags(st.deployment)
        )
        try:
            credentials = config.resolve_credentials(pr.provider)
        except RuntimeError as exc:
            raise PlanBlockedError(str(exc)) from exc
        _delete_with_retry(driver, pr, credentials, retry=retry)
        _require_tracked(st, pr.entry.resource_key)
        del st.resources[pr.entry.resource_key]
    _prune_dependents_on(st, pr.entry.resource_key, pr.dropped_dependents)
    state.save(st, state_path)
    try:
        if not pr.aiform_md_path.exists():
            raise FileNotFoundError(str(pr.aiform_md_path))
        move_to_trash(pr.aiform_md_path)
    except FileNotFoundError:
        logger.warning(
            "%s: tracked file %s is missing; skipping the trash move",
            pr.entry.resource_key,
            pr.aiform_md_path,
        )


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
