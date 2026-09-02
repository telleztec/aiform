# specs/resource_tagging.md — marker-tag contract for `aiform/driver.py` (+ `drivers/digitalocean/compute.py` as the first adopter)

**Naming note**: this filename deliberately doesn't follow
`specs/README.md`'s strict per-module mirroring rule
(`drivers/digitalocean/compute.py` → `specs/digitalocean_compute.md`) —
that rule assumes one spec maps to one implementation module, and this
spec instead adds a small, shared capability to `aiform/driver.py`
(already specced in `specs/driver.md`) with one concrete integration
against `drivers/digitalocean/compute.py` (already specced in
`specs/digitalocean_compute.md`). Named for the feature it adds rather
than either module, with both of those specs cross-referencing it (see
below) so it's discoverable from either direction.

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
that chooses to use it gets a fixed, aiform-owned marker tag
(`aiform-managed`) attached, transparently, regardless of what the
user's file specifies. This has value beyond testing — most directly,
answering "what has aiform ever created in this account" from the CSP
side, independent of a `state.json` that could itself be lost,
corrupted, or simply never written (e.g. a crash between `create()`
succeeding and the state write in `orchestrator.py`'s `apply_plan()`).

**Relationship to `PLAN.md` §10's "Resource tagging convention" entry —
corrected after this spec initially missed it entirely.** That entry
already commits to a long-term target format,
`aiform:<short-uuid>:<state-incarnation-no>:intended-state:<owner-id>`
— a structured tag encoding which aiform state/formation owns a
resource, a generation counter, a fixed marker segment, and an owner
identifier. This spec does **not** implement that format. It ships only
the fixed marker-segment piece (`aiform-managed`, standing in for that
entry's `intended-state` literal), because the other three components
each depend on state/design work that doesn't exist yet: `state.json`
has no formation-UUID or incarnation-counter concept to source
`<short-uuid>`/`<state-incarnation-no>` from, and `<owner-id>`'s own
identifier scheme is explicitly still undetermined in `PLAN.md` §10
itself. `PLAN.md` §10 has been updated to say so explicitly, so the two
documents no longer silently disagree. The multi-project/provenance
scoping this spec's own Out of scope section defers is, concretely,
the `<short-uuid>`/`<owner-id>` portion of that fuller format — tracked
there, not solved here.

**Why no opt-in flag.** An earlier draft of this spec added a
`SUPPORTS_TAGGING: bool` class attribute to `ResourceDriver`, mirroring
`LIKELY_REPLACE_FIELDS`'s pattern, so the two helper methods below could
no-op instead of act. Dropped: unlike `LIKELY_REPLACE_FIELDS` (which the
orchestrator reads externally, for UX warnings), nothing outside a
driver's own `create()`/`read()` would ever have read `SUPPORTS_TAGGING`
— a driver "opts in" simply by choosing to call the two helpers from its
own methods, or not. A boolean flag whose only job is gating a codepath
that a *single existing driver* (`digitalocean`/`compute`, the only one
that exists per `PLAN.md` §10) always exercises is exactly the kind of
config knob `CLAUDE.md`'s "don't add abstractions ... for scenarios that
can't happen yet" rule warns against — the `False` branch existed only
for a hypothetical future CSP without a tagging primitive. If and when a
second driver actually needs to signal "I don't support this," that's
the point to add a flag, informed by a real second case instead of a
guessed one.

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

    def _tags_for_create(self, requested_tags: list[str]) -> list[str]:
        """Call from create() (and, on the replace path, the create()
        that follows a delete()) when building the CSP request body:
        appends AIFORM_MANAGED_TAG to whatever tags were requested.
        Raises ValueError if AIFORM_MANAGED_TAG is already present in
        requested_tags -- that string is reserved for aiform's own
        use; a user's own aiform.md must not set it (see Edge cases)."""

    def _tags_for_attributes(self, live_tags: list[str]) -> list[str]:
        """Call from every point a driver builds the `tags` value it
        returns to the orchestrator (create()'s return, read()'s
        return, update()'s return): strips AIFORM_MANAGED_TAG back
        out."""
```

Both are concrete (not abstract) methods on the base class. A driver
opts in purely by calling them from its own `create()`/`read()`/
`update()`; a driver that never calls them is entirely unaffected — no
flag, no conditional, nothing to configure.

`drivers/digitalocean/compute.py` is the first (and, per `PLAN.md` §10,
currently only) adopter, via the two integration points named in
Behavior below.

`specs/driver.md` (the spec for this already-implemented module) and
`PLAN.md` §4 (its "exact contract") both need a small addendum adding
`AIFORM_MANAGED_TAG` and these two methods, since `CLAUDE.md` treats
§4's contract as authoritative and requires it be followed exactly —
not done as part of this spec (see Out of scope), but a hard
prerequisite before implementation, not an afterthought.

## Behavior

- **The marker never enters the diff engine — the core invariant this
  whole design exists to protect.** `orchestrator.py`'s
  `build_create_plan()` diffs `resource_spec.params` (verbatim from the
  user's `.aiform.md`, via `planner.diff_attributes()`) against
  `current_attributes` (verbatim from `driver.read()`'s return). Neither
  side of that comparison is touched by this feature: the marker is
  added only inside a driver's own CSP request body, and stripped back
  out of anything the driver hands back to the orchestrator. From
  `orchestrator.py`'s point of view, a resource created by a driver
  using these helpers looks exactly as if the feature didn't exist.
  This is why nothing in `orchestrator.py`/`planner.py` needs to change
  at all.
- **`_tags_for_create(requested_tags)`**: raises `ValueError` if
  `AIFORM_MANAGED_TAG` is already present in `requested_tags` — that
  string is reserved for aiform's own use, and a user's own
  `.aiform.md` setting it explicitly is precisely the collision case
  Edge cases names below, surfaced loudly at `create()`/`apply` time
  rather than allowed to silently corrupt future diffs. Otherwise
  returns `[*requested_tags, AIFORM_MANAGED_TAG]` — marker always
  appended last, not inserted anywhere else, so behavior stays
  predictable to read (order doesn't matter to the CSP itself).
- **`_tags_for_attributes(live_tags)`**: returns
  `[t for t in live_tags if t != AIFORM_MANAGED_TAG]`, unconditionally.
- **`drivers/digitalocean/compute.py` integration** (concrete worked
  example, mirroring how `specs/digitalocean_compute.md` treats
  `PLAN.md`'s worked example as authoritative rather than illustrative):
  - `create()`'s existing body-building loop
    (`for key in ("ssh_keys", "backups", "monitoring", "tags"): if key
    in params: body[key] = params[key]`) changes its `tags` case to
    `body["tags"] = self._tags_for_create(params.get("tags", []))`,
    called unconditionally (not only `if "tags" in params:`) so the
    marker is attached even when the user's `.aiform.md` never mentions
    `tags` at all.
  - `_flatten()` — the one helper `create()`, `read()`, **and**
    `update()`'s in-place resize path all funnel through to build the
    attributes dict — changes its `"tags": droplet.get("tags", [])`
    line to `"tags": self._tags_for_attributes(droplet.get("tags", []))`.
    Because all three already share this one method, this is a
    one-line change that covers all three call sites at once — no
    separate edit needed in `read()` or `update()` themselves. See the
    next bullet for why `update()` specifically needs no *additional*
    change beyond this shared fix.
  - `update()`'s in-place resize path needs **no separate change
    beyond the `_flatten()` fix above** — correcting an earlier draft
    of this spec, which wrongly assumed `tags` was one of the fields
    `update()` echoes from `desired` the way it does for
    `ssh_keys`/`backups`/`monitoring`. It isn't: per
    `specs/digitalocean_compute.md`'s Behavior section and the real
    `update()` (`drivers/digitalocean/compute.py`), only
    `ssh_keys`/`backups`/`monitoring` are echoed from `desired`/
    `current` after the resize; `tags` comes entirely from
    `attrs = self._flatten(final_droplet)` — the live post-resize
    droplet response, already covered by the previous bullet. **Do
    not** add a fourth echo line for `tags` alongside the
    `ssh_keys`/`backups`/`monitoring` ones: doing so would overwrite
    the freshly-observed live tags with a value derived from
    `desired`/`current` instead, discarding real CSP state in favor of
    stale input — exactly the "state is a cache of live reality, not a
    source of truth" bug `CLAUDE.md`'s State handling section warns
    against, for `tags` specifically.
- **Zero extra API calls.** The marker rides in the same `POST
  /v2/droplets` request `create()` already makes — there is no separate
  "tag this resource" round trip, no orchestrator-level call site added,
  and nothing here touches an LLM in any way. `CLAUDE.md`'s zero-Anthropic-
  call-on-repeat-run guarantee is unaffected because this feature adds
  no new call anywhere in the plan/apply path; the one real DO call this
  touches (`create()`) was already being made regardless.
- **Migration safety for resources that already exist.** Because the
  marker is never part of `desired_params` (aiform.md's own params) and
  never visible in the `attributes` a driver returns to the
  orchestrator, a resource created *before* this feature exists (or
  before a given driver starts calling these helpers) produces an empty
  diff and is never touched, retried, or replaced by this feature
  landing. This is the specific hazard that ruled out the alternative of
  merging the marker into `resource_spec.params["tags"]` at the
  orchestrator level: that approach would make an already-live
  resource's real (unmarked) tags disagree with the newly-marker-
  including desired params on the very next `plan`. **The consequence
  named here has since changed, and the conclusion has not.** When this
  was written, `drivers/digitalocean/compute.py`'s `update()` accepted
  only a `size`-alone diff in place, so a `tags`-only diff raised
  `DriverUpdateNotSupported` and would have destroyed and recreated
  every existing tagged resource the first time this shipped. That bug
  is fixed (issue #77): a `tags` diff is now applied in place, so the
  same mistake would today cause perpetual tag churn — every `plan`
  proposing to re-add a marker the driver strips straight back out —
  rather than mass destruction. Less catastrophic, still wrong, and
  still a permanently non-converging plan. Keeping the marker entirely
  inside the driver, invisible to the diff, avoids that failure mode
  structurally rather than by convention.

  A second consequence of the same fix, in this design's favour — stated
  for when these helpers land, since none of them exist yet:
  `update()` computes its tag removals from `current`, and once
  `_tags_for_attributes` strips the marker out of that value, the marker
  can never appear in the `remove` set. So an ordinary tags edit will
  not be able to un-assign it, and the in-place path will need no
  special case of its own. **This also means the feature is
  intentionally not a backfill mechanism** — see Out of scope.
- **Review checklist follow-up required, not optional.** A driver whose
  `create()` calls `_tags_for_create` but whose `read()`/`update()`
  don't correspondingly call `_tags_for_attributes` everywhere they
  return `tags` (or vice versa) is exactly the kind of inconsistency
  gate #1 (`code-review-model`) needs to catch, not something any
  existing static check covers — `prompts/review_driver.md` needs a new
  numbered checklist item alongside its existing "urllib.request only"
  item (item 9) and idempotent-delete item (item 3): a driver that
  attaches the marker on create but leaks it back out anywhere (or
  strips a tag it never attached) is a `blocking_issues` entry, not a
  `concerns` one. Not written in this spec — a small, separate edit to
  that prompt file, done alongside whichever PR first wires these
  helpers into a real driver.

## Edge cases / errors

- **A user's own `.aiform.md` explicitly requesting the literal tag
  `aiform-managed`.** `PARAM_SCHEMA["tags"]` is an unconstrained string
  array (`specs/digitalocean_compute.md`) — nothing stops a user from
  writing `tags: ["aiform-managed"]` themselves. Left unhandled, this is
  a real, silent footgun: `_tags_for_create` would treat the marker as
  already present and no-op, `_tags_for_attributes` would then
  unconditionally strip it out of every subsequent `create()`/`read()`
  return, permanently zeroing that entry out of `attributes["tags"]`
  while `desired_params["tags"]` (never touched, per this spec's core
  "never enters the diff engine" invariant) still has it — a permanent
  `tags` mismatch on every future `plan`, and a permanent re-tagging
  loop on every `apply` — before issue #77 this was a permanent
  destroy+recreate loop instead, since `update()` then accepted only a
  `size`-alone diff in place. This is why `_tags_for_create` raises
  `ValueError` on this input instead (see Behavior above): the failure
  surfaces immediately, before any CSP call is made, naming the reserved
  tag, instead of manifesting later as an unexplained replace loop.
  Choosing a more obscure marker string wouldn't fix the general
  problem — any fixed string a user could plausibly type is a possible
  collision, so raising on collision is the actual fix, not the
  specific string chosen.
- **A CSP/resource kind with no tagging primitive at all, or a driver
  that simply chooses not to use this.** The driver's `create()`/
  `read()`/`update()` just never call `_tags_for_create`/
  `_tags_for_attributes` — not an error, not a degraded mode, the
  guarantee just doesn't extend to that `(provider, resource)` pair.
  Any sweep/audit tooling built against this mechanism has no signal
  for such a driver's resources and would need a different
  identification strategy (e.g. a name prefix convention) — a real
  limitation, named here, not solved by this spec.
- **`update()`'s replace path calls `create()` a second time**
  (`orchestrator.py`'s `apply_plan()`, the `DriverUpdateNotSupported`
  branch) with the same `desired_params` used for the original diff.
  Per the migration-safety point above, `desired_params["tags"]` never
  legitimately contains the marker, so `_tags_for_create`'s
  reserved-tag check has nothing to trigger on here — the same
  non-issue as the general migration-safety argument, not a new risk.
- **Two independent tag concepts coexist for the system test
  specifically**, and that's fine: this spec's `aiform-managed` marker
  (global, applied by any driver that uses these helpers, invisible to
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

- **Updating `specs/driver.md`/`PLAN.md` §4 with `AIFORM_MANAGED_TAG`
  and the two new methods.** Named as a hard prerequisite above, not
  done in this spec — a small, mechanical addendum to both, since
  `aiform/driver.py` is already implemented and `CLAUDE.md` requires
  its contract be followed exactly.
- **Retroactively tagging resources that already exist.** The marker is
  only ever attached at a resource's `create()` time (see Behavior's
  migration-safety point) — there is no mechanism here that walks
  existing `state.json` entries and tags what's already live. A future
  one-time migration tool (iterate tracked resources, call each
  adopting driver's tagging capability directly against them, outside
  the normal diff/plan path entirely) is real, separate future work,
  not designed here.
- **Multi-project or provenance-scoped tagging** (e.g. encoding which
  local `state.json`/project a resource belongs to, not just "aiform
  made this somewhere") — concretely, the `<short-uuid>`/`<owner-id>`
  components of `PLAN.md` §10's full target format (see Purpose's
  "Relationship to `PLAN.md` §10" note). Deliberately deferred in favor
  of shipping the smallest useful version first: one fixed, global
  `aiform-managed` string, with no formation-identity or ownership
  encoding.
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
  designs the integration for; whether these two helper methods are a
  good fit for a CSP with a meaningfully different tagging model — or
  none at all — is untested until a second driver exists, the same
  caveat `PLAN.md` §10 already states for the driver interface
  generally. If that turns out to need a discoverable per-driver
  capability flag after all, that's the point to add one, informed by a
  real case (see "Why no opt-in flag" above).
