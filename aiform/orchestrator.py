# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

import dataclasses
import hashlib
import importlib.util
import json
import logging
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anthropic

from aiform import config, llm, log, parser, planner, state
from aiform.driver import DriverUpdateNotSupported, ResourceDriver
from aiform.exceptions import DriverExecutionError, PlanBlockedError, ResourceNotFoundError
from aiform.models import (
    DriverInfo,
    LLMConfig,
    PlanAction,
    PlanEntry,
    PlanReview,
    PlanReviewFlag,
    PlanReviewSeverity,
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


def ensure_driver_trusted(
    provider: str,
    resource_type: str,
    state: State,
    *,
    client: anthropic.Anthropic | None = None,
    llm_config: LLMConfig | None = None,
) -> DriverInfo:
    path = driver_path(provider, resource_type)
    on_disk_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()

    for entry in state.resources.values():
        if (
            entry.provider == provider
            and entry.resource_type == resource_type
            and entry.driver.sha256 == on_disk_sha256
        ):
            logger.info(
                "",
                extra={"provider": provider, "resource_type": resource_type, "reused": True},
            )
            return entry.driver

    source_text = path.read_text(encoding="utf-8")
    review = llm.review_driver(source_text, client=client, llm_config=llm_config)
    logger.info(
        "",
        extra={
            "provider": provider,
            "resource_type": resource_type,
            "reused": False,
            "approved": review.approved,
        },
    )
    if not review.approved:
        raise PlanBlockedError(
            f"driver {path} failed code-review-model review: "
            f"{review.blocking_issues or review.concerns}"
        )

    return DriverInfo(
        path=f"drivers/{provider}/{resource_type}.py",
        sha256=on_disk_sha256,
        generated_at=datetime.now(UTC),
        code_review=review,
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

    driver_cache: dict[tuple[str, str], tuple[ResourceDriver, DriverInfo]] = {}
    credentials_cache: dict[str, dict[str, str]] = {}

    planned: list[PlannedResource] = []
    covered_keys: set[str] = set()

    for path in files:
        if is_delete_marked(path):
            content = path.read_text(encoding="utf-8-sig")
            resource_spec = parser.parse_frontmatter(content)
            key = resource_key(resource_spec.provider, resource_spec.resource, resource_spec.name)
            covered_keys.add(key)
            state_entry = st.resources.get(key)
            entry = planner.destroy_entry(key, rationale=f"marked for deletion via {path.name}")
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
                )
            )
            continue

        content = path.read_text(encoding="utf-8-sig")
        resource_spec = parser.parse_frontmatter(content)
        key = resource_key(resource_spec.provider, resource_spec.resource, resource_spec.name)
        covered_keys.add(key)
        state_entry = st.resources.get(key)
        previous_hash = state_entry.aiform_md_sha256 if state_entry else None

        parsed = parser.parse_file(
            path, previous_aiform_md_sha256=previous_hash, client=client, llm_config=llm_config
        )

        driver_key = (resource_spec.provider, resource_spec.resource)
        if driver_key not in driver_cache:
            driver = load_driver(resource_spec.provider, resource_spec.resource)
            driver_info = ensure_driver_trusted(
                resource_spec.provider,
                resource_spec.resource,
                st,
                client=client,
                llm_config=llm_config,
            )
            driver_cache[driver_key] = (driver, driver_info)
        driver, driver_info = driver_cache[driver_key]

        if resource_spec.provider not in credentials_cache:
            try:
                credentials_cache[resource_spec.provider] = config.resolve_credentials(
                    resource_spec.provider
                )
            except RuntimeError as exc:
                raise PlanBlockedError(str(exc)) from exc
        credentials = credentials_cache[resource_spec.provider]

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
                entry = planner.create_entry(
                    key, rationale="tracked resource no longer exists on the provider side"
                )
            else:
                entry = planner.plan_resource(
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

        planned.append(
            PlannedResource(
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
            )
        )

    state.save(st, state_path)

    warnings: list[str] = []
    if not paths:
        for key in st.resources:
            if key not in covered_keys:
                warnings.append(
                    f"resource {key!r} is tracked in state but has no corresponding "
                    ".aiform.md file this run; left unchanged"
                )

    return planned, warnings


def build_destroy_plan(
    paths: list[Path] | None = None,
    *,
    state_path: Path = state.DEFAULT_STATE_PATH,
) -> list[PlannedResource]:
    st = state.load(state_path)
    planned: list[PlannedResource] = []

    if paths:
        for path in paths:
            content = path.read_text(encoding="utf-8-sig")
            resource_spec = parser.parse_frontmatter(content)
            key = resource_key(resource_spec.provider, resource_spec.resource, resource_spec.name)
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
                )
            )
    else:
        for key, state_entry in st.resources.items():
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
                )
            )

    return planned


ConfirmFn = Callable[[str], bool]


@dataclass
class ApplyResult:
    executed: list[PlanEntry]
    review_flags: list[PlanReviewFlag]
    aborted: bool


def default_confirm(prompt: str) -> bool:
    return input(f"{prompt} [y/N]: ").strip().lower() == "y"


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
    client: anthropic.Anthropic | None = None,
    llm_config: LLMConfig | None = None,
) -> ApplyResult:
    st = state.load(state_path)
    confirm_fn = confirm or default_confirm
    review_flags: list[PlanReviewFlag] = []

    needs_review = any(
        pr.entry.action == PlanAction.DESTROY
        or (pr.entry.action == PlanAction.UPDATE and pr.entry.likely_replace)
        for pr in planned
    )
    if needs_review:
        review = llm.review_plan(build_plan_summary(planned), client=client, llm_config=llm_config)
        blocked = not review.safe_to_proceed or any(
            flag.severity == PlanReviewSeverity.BLOCK for flag in review.flags
        )
        (logger.warning if blocked else logger.info)(
            "",
            extra={"safe_to_proceed": review.safe_to_proceed, "flags_count": len(review.flags)},
        )
        _raise_if_review_blocked(review)
        review_flags.extend(
            flag for flag in review.flags if flag.severity != PlanReviewSeverity.BLOCK
        )

    if not yes and not confirm_fn("Apply this plan?"):
        return ApplyResult(executed=[], review_flags=review_flags, aborted=True)

    executed: list[PlanEntry] = []

    for pr in planned:
        if pr.entry.action == PlanAction.NO_OP:
            continue

        if pr.entry.action == PlanAction.CREATE:
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
            executed.append(pr.entry)

        elif pr.entry.action == PlanAction.UPDATE:
            replaced = False
            update_start = time.monotonic()
            try:
                raw = pr.driver.update(
                    pr.state_entry.id, pr.state_entry.attributes, pr.desired_params, pr.credentials
                )
            except DriverUpdateNotSupported:
                replaced = True
                if not pr.entry.likely_replace:
                    modified_entry = pr.entry.model_copy(update={"likely_replace": True})
                    single_summary = build_plan_summary(
                        [dataclasses.replace(pr, entry=modified_entry)]
                    )
                    single_review = llm.review_plan(
                        single_summary, client=client, llm_config=llm_config
                    )
                    _raise_if_review_blocked(single_review)
                    review_flags.extend(
                        flag
                        for flag in single_review.flags
                        if flag.severity != PlanReviewSeverity.BLOCK
                    )
                    if not confirm_fn(f"Replace {pr.entry.resource_key}?"):
                        return ApplyResult(
                            executed=executed, review_flags=review_flags, aborted=True
                        )
                _call_driver(
                    pr.driver.delete,
                    pr.provider,
                    pr.resource_type,
                    "delete",
                    pr.state_entry.id,
                    pr.credentials,
                )
                # The old resource is now verifiably gone on the CSP side --
                # drop it from state and save immediately, before attempting
                # create(). If create() then fails, state.json correctly
                # reflects "not tracked" rather than stale id/attributes for
                # a resource that no longer exists (the same drifted_missing
                # self-healing this checkpoint pre-empts would otherwise be
                # needed to detect it on the next refresh).
                _require_tracked(st, pr.entry.resource_key)
                del st.resources[pr.entry.resource_key]
                state.save(st, state_path)
                raw = _call_driver(
                    pr.driver.create,
                    pr.provider,
                    pr.resource_type,
                    "create",
                    pr.name,
                    pr.desired_params,
                    pr.credentials,
                )
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
            # entry.likely_replace reflects the plan-time prediction; report
            # what actually happened instead, in both directions -- a
            # predicted replace that update() handled in place must not be
            # reported as a replace just because the prediction said so, the
            # same way an unpredicted replace (the `replaced` branch above)
            # must not be under-reported.
            executed.append(pr.entry.model_copy(update={"likely_replace": replaced}))

        elif pr.entry.action == PlanAction.DESTROY:
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
            executed.append(pr.entry)
            continue

        state.save(st, state_path)

    return ApplyResult(executed=executed, review_flags=review_flags, aborted=False)


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
