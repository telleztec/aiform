# specs/resource_tagging.md — marker-tag contract for `aiform/driver.py` (+ `drivers/digitalocean/compute.py` as the first adopter)

## Purpose

`specs/system_test.md`'s "Orphan cleanup (leaked resources)" section
names a real, separate gap while designing that suite's sweep backstop:
nothing in `aiform/driver.py`'s `ResourceDriver` contract or
`orchestrator.py` guarantees that *any* resource aiform creates carries
an identifying tag — `tags` today is just one more optional key a
user's own `.aiform.md` may or may not set. The system test's own sweep
works around this by having its fixture set an explicit
`tags: ["aiform-system-test"]`, but that only covers resources the
suite itself creates.

This spec is the general fix: every resource created through a driver
that opts in gets a fixed, aiform-owned marker tag
(`aiform-managed`) attached, transparently, regardless of what the
user's file specifies. This has value beyond testing — most directly,
answering "what has aiform ever created in this account" from the CSP
side, independent of a `state.json` that could itself be lost,
corrupted, or simply never written (e.g. a crash between `create()`
succeeding and the state write in `orchestrator.py`'s `apply_plan()`).

## Interface

All additions live in `aiform/driver.py`, on `ResourceDriver` itself —
no change to `orchestrator.py` and no change to any of the four
abstract methods' signatures. This is deliberate: see Behavior for why
keeping the marker entirely inside each driver's own implementation,
invisible to the orchestrator's diff engine, is what makes this safe to
add without risking a destructive replace on every pre-existing
resource the moment this ships.

```python
# aiform/driver.py

AIFORM_MANAGED_TAG = "aiform-managed"


class ResourceDriver(ABC):
    ...
    # Opt-in, mirrors LIKELY_REPLACE_FIELDS's pattern: defaults to
    # "not supported," a subclass whose CSP has a tagging primitive
    # reassigns it explicitly.
    SUPPORTS_TAGGING: bool = False

    def _tags_for_create(self, requested_tags: list[str]) -> list[str]:
        """Call from create() (and, on the replace path, the create()
        that follows a delete()) when building the CSP request body:
        appends AIFORM_MANAGED_TAG to whatever tags were requested, if
        not already present. No-op (returns requested_tags unchanged)
        when SUPPORTS_TAGGING is False."""

    def _tags_for_attributes(self, live_tags: list[str]) -> list[str]:
        """Call from every point a driver builds the `tags` value it
        returns to the orchestrator (create()'s return, read()'s
        return, update()'s return): strips AIFORM_MANAGED_TAG back out.
        No-op when SUPPORTS_TAGGING is False."""
```

Both are concrete (not abstract) methods on the base class, callable by
any driver whether or not it opts in — a driver that leaves
`SUPPORTS_TAGGING` at its default `False` can still call them
harmlessly (they're identity functions in that case), so opting in
later is a one-line flag flip plus wiring in the two call sites, not a
larger refactor.

`drivers/digitalocean/compute.py` is the first (and, per `PLAN.md` §10,
currently only) adopter: `SUPPORTS_TAGGING = True`, plus the two
integration points named in Behavior below.

## Behavior

- **The marker never enters the diff engine — the core invariant this
  whole design exists to protect.** `orchestrator.py`'s
  `build_create_plan()` diffs `resource_spec.params` (verbatim from the
  user's `.aiform.md`, via `planner.diff_attributes()`) against
  `current_attributes` (verbatim from `driver.read()`'s return). Neither
  side of that comparison is touched by this feature: the marker is
  added only inside a driver's own CSP request body, and stripped back
  out of anything the driver hands back to the orchestrator. From
  `orchestrator.py`'s point of view, an opted-in driver's resources look
  exactly as if the feature didn't exist. This is why nothing in
  `orchestrator.py`/`planner.py` needs to change at all.
- **`_tags_for_create(requested_tags)`**: returns
  `[*requested_tags, AIFORM_MANAGED_TAG]` unless the marker is already
  present (idempotent — see the replace-path note below), when
  `SUPPORTS_TAGGING` is `True`; returns `requested_tags` unchanged
  otherwise. Marker is always appended last, not inserted anywhere else
  — order doesn't matter for correctness (the CSP doesn't care), but a
  fixed position keeps behavior predictable to read.
- **`_tags_for_attributes(live_tags)`**: returns
  `[t for t in live_tags if t != AIFORM_MANAGED_TAG]` when
  `SUPPORTS_TAGGING` is `True`; returns `live_tags` unchanged otherwise.
- **`drivers/digitalocean/compute.py` integration** (concrete worked
  example, mirroring how `specs/digitalocean_compute.md` treats
  `PLAN.md`'s worked example as authoritative rather than illustrative):
  - `create()`'s existing body-building loop
    (`for key in ("ssh_keys", "backups", "monitoring", "tags"): if key
    in params: body[key] = params[key]`) changes its `tags` case to
    `body["tags"] = self._tags_for_create(params.get("tags", []))`,
    called unconditionally (not only `if "tags" in params"`) so the
    marker is attached even when the user's `.aiform.md` never mentions
    `tags` at all.
  - `_flatten()` — the single helper both `create()` and `read()`
    already funnel through to build the attributes dict — changes its
    `"tags": droplet.get("tags", [])` line to
    `"tags": self._tags_for_attributes(droplet.get("tags", []))`. Because
    both `create()` and `read()` already share this one method, this is
    a one-line change that covers both call sites at once — no separate
    edit needed in `read()` itself.
  - `update()`'s in-place resize path returns attributes "echoed from
    `desired`" per `specs/digitalocean_compute.md`'s Behavior section
    (`update()` has `desired` in scope, unlike `read()`); that echo
    should also route `tags` through `_tags_for_attributes` for the same
    invisibility guarantee, even though `desired["tags"]` never
    legitimately contains the marker today (see the migration-safety
    point below) — defense in depth, not defense against a case that's
    expected to occur.
- **Zero extra API calls.** The marker rides in the same `POST
  /v2/droplets` request `create()` already makes — there is no separate
  "tag this resource" round trip, no orchestrator-level call site added,
  and nothing here touches an LLM in any way. `CLAUDE.md`'s zero-Anthropic-
  call-on-repeat-run guarantee is unaffected because this feature adds
  no new call anywhere in the plan/apply path; the one real DO call this
  touches (`create()`) was already being made regardless.
- **Migration safety for resources that already exist.** Because the
  marker is never part of `desired_params` (aiform.md's own params) and
  never visible in the `attributes` a driver returns to the orchestrator,
  a resource created *before* a driver opts into `SUPPORTS_TAGGING` — or
  before this feature exists at all — produces an empty diff and is
  never touched, retried, or replaced by this feature landing. This is
  the specific hazard that ruled out the alternative of merging the
  marker into `resource_spec.params["tags"]` at the orchestrator level:
  that approach would make an already-live resource's real (unmarked)
  tags disagree with the newly-marker-including desired params on the
  very next `plan`, and — because
  `drivers/digitalocean/compute.py`'s `update()` only accepts a
  `size`-alone diff in place (`specs/digitalocean_compute.md`'s Behavior
  section) — a `tags`-only diff would raise `DriverUpdateNotSupported`
  and trigger a full destroy+recreate of every existing tagged resource
  the first time this shipped. Keeping the marker entirely inside the
  driver, invisible to the diff, avoids that failure mode structurally
  rather than by convention. **This also means the feature is
  intentionally not a backfill mechanism** — see Out of scope.
- **Review checklist follow-up required, not optional.** `SUPPORTS_TAGGING
  = True` declared without both `_tags_for_create`/`_tags_for_attributes`
  actually wired into `create()`/`read()`/`update()` is exactly the kind
  of thing gate #1 (`code-review-model`) needs to catch, not something
  any existing static check covers — `prompts/review_driver.md` needs a
  new numbered checklist item alongside its existing "urllib.request
  only" item (item 8) and idempotent-delete item (item 3), phrased the
  same way: a driver claiming the flag but not honoring it is a
  `blocking_issues` entry, not a `concerns` one. Not written in this
  spec — a small, separate edit to that prompt file, done alongside
  whichever PR first lands `SUPPORTS_TAGGING = True` on a real driver.

## Edge cases / errors

- **A CSP/resource kind with no tagging primitive at all.** The driver
  simply leaves `SUPPORTS_TAGGING` at its default `False` — not an
  error, not a degraded mode, the guarantee just doesn't extend to that
  `(provider, resource)` pair. Any sweep/audit tooling built against
  this mechanism has no signal for such a driver's resources and would
  need a different identification strategy (e.g. a name prefix
  convention) — a real limitation, named here, not solved by this spec.
- **`update()`'s replace path calls `create()` a second time**
  (`orchestrator.py`'s `apply_plan()`, the `DriverUpdateNotSupported`
  branch) with the same `desired_params` used for the original diff.
  Per the migration-safety point above, `desired_params["tags"]` never
  legitimately contains the marker, so `_tags_for_create`'s
  already-present check is defensive rather than load-bearing today —
  written that way anyway, since "this input can't actually contain the
  marker" is exactly the kind of assumption worth not hard-coding.
- **Two independent tag concepts coexist for the system test
  specifically**, and that's fine: this spec's `aiform-managed` marker
  (global, applied to every opted-in driver's resources, invisible to
  the diff engine) and `specs/system_test.md`'s own
  `aiform-system-test` tag (test-scoped, explicitly set by that suite's
  `.aiform.md` fixture through the ordinary, user-facing `tags` param,
  fully visible to the diff engine like any other user-requested tag).
  They don't conflict — `specs/system_test.md`'s sweep mechanism already
  works without this spec landing; once it does, that sweep could
  optionally also filter on `aiform-managed` as a second, broader
  signal, but nothing about it depends on that.
- **A CSP-side tag-count or tag-length cap** (not a concern for
  DigitalOcean specifically, but plausible for a future driver) is a
  per-driver detail to handle if and when it's relevant — not an
  aiform-level concern, not designed further here.

## Out of scope

- **Retroactively tagging resources that already exist.** The marker is
  only ever attached at a resource's `create()` time (see Behavior's
  migration-safety point) — there is no mechanism here that walks
  existing `state.json` entries and tags what's already live. A future
  one-time migration tool (iterate tracked resources, call each opted-in
  driver's tagging capability directly against them, outside the normal
  diff/plan path entirely) is real, separate future work, not designed
  here.
- **Multi-project or provenance-scoped tagging** (e.g. encoding which
  local `state.json`/project a resource belongs to, not just "aiform
  made this somewhere") — `specs/system_test.md` already named this as
  an open question when it proposed this spec. Deliberately deferred in
  favor of shipping the smallest useful version first: one fixed, global
  `aiform-managed` string.
- **A CLI surface over this mechanism** (e.g. `aiform resources list`
  querying every configured provider's tagging API for everything
  tagged `aiform-managed`) — a real, valuable future consumer, not built
  here. This spec only guarantees the tag exists; nothing reads it back
  yet outside `specs/system_test.md`'s own sweep script.
- **Any change to the user-facing `tags:` field's own semantics or
  `PARAM_SCHEMA`.** Untouched — a user's own requested tags continue to
  flow through exactly as `specs/digitalocean_compute.md` already
  documents. This mechanism is deliberately invisible to, and
  independent of, that path.
- **Validating that this pattern generalizes to a second driver/CSP.**
  `digitalocean`/`compute` is the only driver that exists (`PLAN.md` §10,
  "Only one resource kind is implemented") and the only one this spec
  designs the integration for; whether `SUPPORTS_TAGGING`/the two helper
  methods are a good fit for a CSP with a meaningfully different tagging
  model is untested until a second driver exists, the same caveat
  `PLAN.md` §10 already states for the driver interface generally.
