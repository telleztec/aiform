# specs/digitalocean_compute.md — `drivers/digitalocean/compute.py`

## Purpose

**Revised after `PLAN.md`'s "Driver curation" pivot.** This spec
originally described acceptance criteria for output from
`aiform.driver_gen.generate_driver()` — three real generation attempts
against it (documented in `PLAN.md`) showed that path isn't reliable
enough yet to trust unattended, so the MVP curates this driver instead:
hand-authored directly against this spec and the pre-existing test suite
(`tests/drivers/test_digitalocean_compute.py`), following `PROCESS.md`'s
normal spec-first/test-first/Opus-reviewed loop like every other module
in this codebase — the same red-tests-first discipline, just with a
human (or Claude Code under human supervision) writing the
implementation instead of an unattended `draft_driver()` call. The
acceptance criteria below are unchanged by this — they were already
written precisely enough to serve as a hand-implementation spec, not
just a check on generated output.

`PLAN.md` §4 already gives this driver's `PARAM_SCHEMA` and
`LIKELY_REPLACE_FIELDS` verbatim as its worked example — treated here as
authoritative, not just illustrative, since it's explicitly labeled
`# drivers/digitalocean/compute.py`.

## Interface

`PARAM_SCHEMA` and `LIKELY_REPLACE_FIELDS`, verbatim from `PLAN.md` §4:

```python
PARAM_SCHEMA = {
    "type": "object",
    "properties": {
        "region": {"type": "string"},
        "size": {"type": "string"},
        "image": {"type": "string"},
        "ssh_keys": {"type": "array", "items": {"type": "string"}},
        "backups": {"type": "boolean"},
        "monitoring": {"type": "boolean"},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["region", "size", "image"],
    "additionalProperties": True,
}
LIKELY_REPLACE_FIELDS = ["image", "region"]
NON_DIFFABLE_FIELDS = ["ssh_keys"]
```

`credentials` is always `{"DIGITALOCEAN_TOKEN": "<token>"}` (per
`config.py`'s `PROVIDER_TOKEN_ENV_VARS`), sent as
`Authorization: Bearer <token>` on every request.

**HTTP client convention** (added here, not in `PLAN.md`, for
testability): `urllib.request` only, no third-party HTTP library — see
`prompts/generate_driver.md` items 8–9 for the exact wording (single
source of truth; not restated in full here to avoid three-file drift
across this spec, the generation prompt, and the review prompt). Made
specifically so tests can mock one well-known stdlib seam
(`urllib.request.urlopen`) rather than guessing which third-party
library a generation run happened to reach for.

Stating this as a "hard requirement" in the prompt doesn't mechanically
enforce it on its own — `driver_gen.py`'s `validate_driver_source()` only
checks class/method structure and anthropic-related patterns, nothing
HTTP-library-specific, so a violation could otherwise pass gate #1
undetected and only surface when this driver's own test suite fails.
Closed *for now* by an explicit checklist item in
`prompts/review_driver.md` (gate #1, `code-review-model`, checks for it) rather than
extending `validate_driver_source()`. **Known gap, flagged as a named
follow-up, not silently accepted**: this is inconsistent with this
project's stated preference for deterministic checks over probabilistic
LLM review wherever cheap to do so (`CLAUDE.md`/`PLAN.md`'s "deterministic
dict-diff first" framing) — a static `ast`-based check here would be a
few lines, mirroring the existing `_imports_anthropic` pattern in
`driver_gen.py`. Not done in this same pass because `driver_gen.py` is a
separate, already-merged, already-reviewed module — extending it belongs
in its own follow-up PR, not stacked onto this one.

## Behavior

All request bodies are JSON; base URL `https://api.digitalocean.com/v2`.

### `create(name, params, credentials)`

- `POST /v2/droplets` with a JSON body built from `params` plus the
  separate `name` argument — resolved by `orchestrator.py`'s spec: `name`
  is `create()`'s own parameter (`aiform/driver.py`'s `ResourceDriver`
  contract), never a key inside `params`. See the former "Where does the
  droplet's `name` come from?" Edge case below, now resolved rather than
  left open.
- **One mutating call, plus polling GETs to convergence.** `PLAN.md` §9
  step 3's "one real DO API call" describes this step of the MVP
  walkthrough at the level of "this is a real CSP-side operation, not an
  LLM-only step" — not a literal one-HTTP-request budget on `create()`
  internally, the same way `update()`'s in-place resize already makes
  several (power-off, poll, resize, poll, power-on, poll) without
  violating that framing. DigitalOcean's create response is `202
  Accepted` with the droplet object already in the body, typically
  `status: "new"` and `networks.v4: []` (no IP yet) — `create()` takes
  only the new `id` from that response and then polls `GET
  /v2/droplets/{id}` (via the same `_get_droplet`/`_poll_until` helpers
  `update()` uses, bounded the same way: `max_attempts=20`,
  `delay_seconds=2`, raising `TimeoutError` naming the droplet `id` on
  exhaustion) until `status == "active"`, discarding the transient POST
  body in favor of the converged GET response. This was originally
  designed the other way (no polling, convergence picked up by a later
  `plan`/`refresh`) but that left `create()` returning a permanently
  wrong `status`/`ipv4_address` immediately after every `apply` and was
  inconsistent with `update()`'s own convergence-polling — revised after
  a live `code-review-model` run against the curated driver flagged the
  missing convergence handling as a real correctness gap, not a false
  positive.
- Returns a **flattened** dict whose keys mirror `PARAM_SCHEMA`'s flat
  shape (plus `id`/`status`/`ipv4_address` — note that key name, not
  `ip_address`: it must match the field name `PLAN.md`'s worked
  `state.json` example and `StateEntry`'s existing test fixture already
  use for this exact resource), **not** DO's raw nested response
  verbatim — this matters because `update()` diffs this return value
  (as `current`) against `desired` (the flat `params` dict), and DO's
  raw droplet object nests `region`/`image` as objects
  (`{"slug": ..., "name": ...}`) and reports size under a *different*
  key entirely (`size_slug`, not `size`). Comparing the raw shapes
  directly would see `region`/`image`/`size` as "changed" on literally
  every call — including when nothing changed — permanently disabling
  the in-place resize path this whole `update()` section describes. At
  minimum:
  ```python
  {
      "id": str(droplet["id"]),
      "region": droplet["region"]["slug"],
      "size": droplet["size_slug"],
      "image": droplet["image"]["slug"],
      "status": droplet["status"],
      "tags": droplet.get("tags", []),
      "ipv4_address": <first networks.v4 entry where type == "public", or None>,
  }
  ```
  **Pending update**: `specs/resource_tagging.md` (not yet implemented)
  wraps this `"tags"` line in `self._tags_for_attributes(...)` — this
  code fence will understate what `_flatten()` actually returns once
  that lands. See that spec for the corrected picture.
  `ssh_keys`/`backups`/`monitoring`: `create()` additionally echoes back
  whatever was in `params` for these three keys, **preserving each
  field's own type** — `"ssh_keys": params.get("ssh_keys", [])`,
  `"backups": params.get("backups", False)`, `"monitoring":
  params.get("monitoring", False)`. Don't default all three to `[]`;
  `backups`/`monitoring` are booleans per `PARAM_SCHEMA` and a `[]`
  default there is itself a type mismatch against `desired`'s `False`,
  defeating the no-op diff this echo-back exists to protect. DO's
  response doesn't confirm any of these three, but `create()` *knows*
  what it just requested, so echoing is accurate immediately after
  creation — see Edge cases below for `ssh_keys` specifically, the one
  of the three `read()` can never recover on a later refresh, and what
  that means for the diff.

### `read(id, credentials)`

- `GET /v2/droplets/{id}`. **Exactly one API call.**
- `404` → `aiform.exceptions.ResourceNotFoundError`, naming the `id` in
  the message.
- Returns the same flattened attribute shape as `create()`, with two
  partial exceptions: `monitoring` **and now `backups`** can both be
  recovered from DO's response — `"monitoring" in droplet.get("features", [])`,
  `"backups" in droplet.get("features", [])`, **always with `.get`,
  never `droplet["features"]` directly** (a response missing that key
  entirely must not raise an unhandled `KeyError` out of `read()`) —
  verified directly against DigitalOcean's official OpenAPI droplet
  schema (`digitalocean/openapi` on GitHub): `features` is documented
  as `["backups", "private_networking", "ipv6"]`-shaped, `"backups"` a
  plain member exactly like `"monitoring"`. **This corrects this spec's
  own earlier claim** that `backups` "doesn't map onto a clean boolean
  the way `monitoring` does" — that was an explicit low-confidence
  guess (see this file's git history), and a live `code-review-model`
  run caught it as a real correctness gap during system testing;
  checking the actual schema confirmed the guess was wrong, not the
  reviewer. `ssh_keys` is the one field that genuinely cannot be
  recovered from a `GET` — DigitalOcean's droplet object has no
  `ssh_keys` field at all, on any response, confirmed against the same
  official schema — see Edge cases for what that means for the diff.

### `delete(id, credentials)`

- `DELETE /v2/droplets/{id}`. **Exactly one API call.**
- `204` → success, returns `None`.
- `404` → **also** success, returns `None` — idempotent delete is a
  hard requirement (`aiform/driver.py`'s own docstring: "a 404 from the
  CSP ... is treated as success, not an error"). This is the single
  most important behavior this spec's test suite checks.

### `update(id, current, desired, credentials)`

- Compare `current` vs `desired` — **plain comparison, no field
  excluded** (an earlier version of this driver excluded `ssh_keys`
  here, mirroring an equally-wrong exclusion in `planner.py`'s
  `diff_attributes()`; both were reverted after `/code-review` caught
  that excluding a field from the diff silently drops a *genuine* edit
  to it, not just a spurious mismatch caused by `read()`'s own
  limitations — see `specs/planner.md`'s judgment call 1 and
  `specs/orchestrator.md`'s `refresh_resource()`). `ssh_keys`
  specifically is kept diffable *and* correct here because the caller
  (`orchestrator.py`'s `refresh_resource()`) already carries the prior
  state's `ssh_keys` value forward before this method is ever called —
  by the time `update()` sees `current`, an unchanged `ssh_keys` looks
  unchanged and a genuinely changed one looks changed, the same as any
  other field. If the diff touches anything other than `size` alone
  (i.e. `region`, `image`, `ssh_keys`, `backups`, `monitoring`, or
  `tags` changed) → raise `DriverUpdateNotSupported` naming the changed
  field(s) in `unsupported_fields`. `region` and `image` genuinely
  cannot be changed in place on DigitalOcean (a region move requires a
  snapshot+recreate; an image change requires a destructive rebuild) —
  this matches `LIKELY_REPLACE_FIELDS` exactly. `ssh_keys` also
  genuinely cannot be changed in place — DigitalOcean has no API for it
  at all, `ssh_keys` is accepted only at creation time — so a real
  `ssh_keys` edit correctly falls back to destroy+recreate via this same
  path, through the normal gate #2 review, rather than being silently
  dropped. `backups`/`monitoring`/`tags` are deliberately *not*
  attempted in place either, even though DO likely exposes narrower
  endpoints for some of them — out of scope for this MVP driver, see
  below. All six fields here are correctly diffable (see Behavior's
  `read()` note, plus the carry-forward above for `ssh_keys`
  specifically) — they only show as "changed" when they genuinely were,
  so leaving them un-updatable-in-place is a real scoping choice, not a
  masked reliability gap.
- If the diff is `size` alone: DigitalOcean resize
  (`POST /v2/droplets/{id}/actions`) —
  **low-medium confidence, verify against DO's docs if this fails**:
  requires the droplet to be powered off first. The expected sequence:
  1. If `current["status"] == "off"` already, skip to step 3. If it's
     `"active"`, `POST .../actions {"type": "power_off"}` and proceed to
     step 2. **Any other status** (e.g. `"new"` — still provisioning,
     or `"archive"`) is an unmodeled state for a resize attempt: raise
     `DriverUpdateNotSupported` naming `size`, rather than guessing
     whether power-off applies — DO would likely reject the resize from
     an unexpected state in a way step 4 below isn't built to catch,
     and guessing wrong risks a confusing raw error instead of a clean
     fallback.
  2. Poll `GET /v2/droplets/{id}` until `status == "off"`, bounded
     attempts (a fixed small number, e.g. via a `time.sleep()` between
     checks — must be mockable: `time.sleep`, not a hardcoded blocking
     wait with no seam).
  3. `POST .../actions {"type": "resize", "disk": false, "size": ...}`
     — **`disk: false`, not `true`.** A disk-inclusive resize
     (`disk: true`) can only *grow* a droplet's disk, never shrink it;
     hardcoding `true` risks DO rejecting a downsize outright with no
     recovery path. `disk: false` (compute-only resize) is the safe
     default — it works between sizes that share the same underlying
     disk allotment, which is the common case for a same-family
     up/down move.
  4. If DO rejects the `disk: false` resize (the target size requires a
     disk-size change DO can't perform live — moving between families
     with different bundled disk sizes): **power the droplet back on
     first** (`POST .../actions {"type": "power_on"}`, poll until
     `status == "active"`), *then* raise `DriverUpdateNotSupported`
     naming `size` in `unsupported_fields`, falling back to the normal
     destroy+recreate path. Restoring power state before raising matters:
     `size` is deliberately not in `LIKELY_REPLACE_FIELDS`, so this
     specific `DriverUpdateNotSupported` triggers the orchestrator's
     single-resource `review-orchestration-model` safety-gate pause
     (`aiform/driver.py`'s own docstring) before any destroy+recreate
     proceeds — the entire point of that gate is a human/
     `review-orchestration-model` veto *before* anything destructive
     happens, which is defeated if the droplet is already sitting
     powered off while the gate is being evaluated. Don't retry the
     resize itself with `disk: true` — only restore power state, then
     raise.
  5. On success, poll until the resize action (or the droplet's
     `size_slug`) shows the new size.
  6. `POST .../actions {"type": "power_on"}`, poll until `status ==
     "active"`.
  7. Return the final attributes (equivalent to a `read()`), **including
     `ssh_keys`/`backups`/`monitoring` echoed from `desired`** — per
     `aiform/driver.py`'s `update()` docstring, the return must be "same
     shape as `create()`," and `create()`'s shape includes those three
     keys (see Behavior above). `update()` has `desired` in scope, so it
     echoes from there the same way `create()` echoes from `params` —
     don't return a bare `read()`-shaped dict here, that would silently
     drop those three keys from state on every successful resize, a
     worse, undisclosed version of the refresh-only gap Edge cases
     describes below.
  Alongside `create`'s own convergence-polling (see Behavior above),
  this is one of the two paths in this driver that make more than one
  API call — `read`/`delete` remain genuinely single-request. An
  in-place resize is a multi-step DO operation, not a single request.
  **If any polling step (2, 5, or 6) exhausts its bounded attempt
  budget without reaching the target state**, raise a plain
  `TimeoutError` naming which step and the droplet `id` — don't retry
  indefinitely, don't silently return stale attributes, and don't leave
  the failure mode unspecified. This is a real, bounded operational
  failure (DO provisioning slower than expected), distinct from the
  step-4 disk-mismatch case: that one is a *known, expected* rejection
  with a defined recovery (fall back to destroy+recreate); a timeout is
  *unexpected* and should surface as a loud error for a human to
  investigate, not trigger an automatic destroy+recreate against a
  droplet that might still complete the resize a moment later.
- **`create()`'s convergence poll timing out orphans a real droplet.**
  If the `POST /v2/droplets` call already succeeded (the droplet exists
  and is billing) but the subsequent poll to `status == "active"`
  exhausts its bounded attempts (or hits a transient error), `create()`
  raises `TimeoutError` and `aiform/orchestrator.py`'s `apply_plan()`
  never gets a return value to write into `state.json` — the droplet
  is real but untracked. This isn't specific to `create()`: `update()`'s
  own poll timeout above has the same shape (a mutating action already
  succeeded before the poll that follows it times out). Neither is
  fixed here — `PLAN.md` §10's "Timeout/retry/failover orchestration
  for driver network calls" entry tracks the general gap (no retry
  layer, and no orchestrator-level recovery for a driver call that
  fails after partially succeeding). What *is* addressed here: the
  poll's bounded-attempt budget is sized generously enough
  (`max_attempts=60`, `delay_seconds=3` — 180 seconds) that a
  legitimately-provisioning droplet essentially never hits it in
  practice; this remains a real but rare failure mode, not a
  routinely-triggered one.
- `PLAN.md` §8 step 2 itself anticipates a first-generation `update()`
  might be cruder than this (e.g. "resizes on *any* diff instead of
  scoping to `size`/`region`") and treats that as a
  `code-review-model`-flagged *non-blocking concern*, not a rejection. This spec describes the
  target/ideal behavior; the hand-written test suite should check the
  behaviors that actually matter (size changes work in place; genuinely
  non-updatable fields raise `DriverUpdateNotSupported`, never attempt a
  destructive call) without being so strict about the *exact* internal
  call sequence that a reasonable first-draft variation fails outright.

## Edge cases / errors

- **Knowledge-confidence**: the DigitalOcean API details in this spec
  come from training-data knowledge, not a freshly-fetched doc check —
  build against this and check DO's actual API reference only if
  something fails on first pass. Confidence is genuinely low on two
  points, both flagged inline where they matter: `update()`'s power-off
  requirement for resizing, and whether checking `droplet["features"]`
  is really how DO reports monitoring status on a `GET`.
- **Where does the droplet's `name` come from? Resolved.** This spec
  originally assumed the orchestrator would merge `spec.name` into
  `params` before calling `create()` — that assumption turned out wrong:
  `orchestrator.py` (once built) simply passed `resource_spec.params`
  through unmerged, so `create()` reading `params["name"]` always raised
  `KeyError` in practice, undetected because this file's own test suite
  baked the same wrong assumption into its `BASE_PARAMS` fixture.
  Resolved instead by adding `name: str` as `create()`'s own parameter
  on the `ResourceDriver` contract (`aiform/driver.py`, `PLAN.md` §4) —
  `create()` now receives it directly, never nested inside `params`.
- A `read()`/`delete()` call against an `id` that was never a valid
  droplet ID (malformed, not just missing) is not specially handled —
  DO's API itself returns `404` for a nonexistent ID same as a
  deleted one, so this collapses into the same case.
- `power_off`/`resize`/`power_on` each being independently-async DO
  *actions* (not instant) is why `update()`'s resize path needs polling
  at all, unlike the other three methods — this is a genuine DO API
  constraint, not a design choice made for its own sake.
- **`ssh_keys`/`backups` used to both be unable to round-trip through
  `read()`, violating the non-negotiable zero-Anthropic-API-call
  guarantee for any resource that set them. Fixed, in two different
  ways, after a live `code-review-model` run caught the consequence
  directly** (gate #1 declined to trust this driver, citing exactly
  this: *"forcing a destroy/recreate of a live droplet on every
  run... for practically any droplet configured with SSH keys or
  backups"*):
  - **`backups`**: this spec previously claimed it "doesn't map onto a
    clean boolean the way `monitoring` does via `features`" — an
    explicit low-confidence guess, and checking DigitalOcean's actual
    OpenAPI schema showed the guess was wrong: `backups` is a plain
    member of `features`, exactly like `monitoring`. `read()` now
    derives it the same way (see Behavior above); the round-trip gap
    for `backups` is closed, not just mitigated.
  - **`ssh_keys`**: genuinely cannot be recovered — confirmed against
    the same official schema, the droplet object has no `ssh_keys`
    field at all, on any response, at any time after creation. No
    `read()`-level fix exists for this (the data isn't in DO's API), so
    the fix instead lives in `NON_DIFFABLE_FIELDS = ["ssh_keys"]`
    (`specs/driver.md`) — but **not** as a diff exclusion.
    `orchestrator.py`'s `refresh_resource()` (`specs/orchestrator.md`)
    carries the prior state's `ssh_keys` value forward across every
    `read()`-driven refresh, since `read()` itself can never supply it;
    `planner.py`'s `diff_attributes()` and this driver's own `update()`
    diff (see Behavior above) both do a completely ordinary comparison
    with no special-casing at all. The first version of this fix
    excluded `ssh_keys` from both diffs directly instead — reverted
    after `/code-review` caught that it silently dropped a *genuine*
    `ssh_keys` edit in `.aiform.md` (the diff never contained the key,
    so a real change produced the same empty diff, and the same
    `no-op` plan, as no change at all — worse than the original bug,
    which was at least a visible, if spurious, failure). Carrying the
    value forward instead means: unchanged `ssh_keys` → still matches
    desired → correctly stays a no-op (fixes the original bug); changed
    `ssh_keys` → correctly diffs against the last-known value → reaches
    `update()`, which correctly can't apply it in place and falls back
    to destroy+recreate through the normal gate #2 review (fixes the
    regression, and reflects DO's real constraint — SSH keys really can
    only be set at creation). This is exactly the "concrete, named
    follow-up for whoever specs `planner.py`" this spec previously
    called out as necessary but not yet designed; it's now designed and
    implemented, not still open.
  - `create()` still echoes `ssh_keys`/`backups`/`monitoring` from
    `params` unchanged (see Behavior above) — that part of the original
    design was already correct; only the *ongoing, post-refresh*
    comparison was the actual bug.

## Out of scope

- **In-place update support for `ssh_keys`/`backups`/`monitoring`/`tags`**
  — DO likely exposes narrower endpoints for some of these (e.g.
  `enable_backups`/`disable_backups` actions), but supporting them is
  real future work, not required for this MVP driver; any diff touching
  them raises `DriverUpdateNotSupported` and falls back to
  destroy+recreate, same as a genuinely non-updatable field.
- **A live integration test against DO's real API with a real
  `DIGITALOCEAN_TOKEN`** — this spec and the hand-written test suite that
  checks an implementation against it are both built and validated
  against mocked `urllib.request.urlopen`, not a live DO account.
- **Running this driver's implementation through `generate_driver()`** —
  not done yet, since mechanism 2's on-the-fly generation pipeline
  (see `PLAN.md`'s "Driver curation" section) isn't wired into
  `plan`/`apply` yet; this spec now also serves as the
  hand-implementation guide, not only generated-output acceptance
  criteria.
