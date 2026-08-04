# specs/digitalocean_compute.md — `drivers/digitalocean/compute.py`

## Purpose

**This spec is different in kind from every prior one.** It does not
describe code to be hand-written — it describes the *acceptance
criteria* a driver produced by `aiform.driver_gen.generate_driver()`
must satisfy. The source itself is LLM-drafted (implementation role) and
Opus-reviewed (gate #1) inside `generate_driver()`; this document is
what the hand-written test suite (`tests/drivers/test_digitalocean_compute.py`)
checks the generated output against, as an independent second opinion on
top of Opus's review — and what `prompts/generate_driver.md` is aimed at
producing.

`PLAN.md` §4 already gives this driver's `PARAM_SCHEMA` and
`LIKELY_REPLACE_FIELDS` verbatim as its worked example — treated here as
authoritative, not just illustrative, since it's explicitly labeled
`# drivers/digitalocean/compute.py`.

**Knowledge-confidence note**: the DigitalOcean API details below come
from training-data knowledge, not a freshly-fetched doc check. Per
explicit instruction, the plan is to build against this and check DO's
actual API reference only if something fails on first pass — the one
place confidence is genuinely low is `update()`'s power-off requirement
for resizing (flagged inline below).

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
```

`credentials` is always `{"DIGITALOCEAN_TOKEN": "<token>"}` (per
`config.py`'s `PROVIDER_TOKEN_ENV_VARS`), sent as
`Authorization: Bearer <token>` on every request.

**HTTP client convention** (added here, not in `PLAN.md`, for
testability): the driver must use `urllib.request` — a single
`urllib.request.Request(url, data=..., headers=..., method=...)` /
`urllib.request.urlopen(request)` call per API request, no third-party
HTTP library. This is a deliberate, minimal addition to
`prompts/generate_driver.md`'s existing generic HTTP guidance, made
specifically so tests can mock one well-known stdlib seam
(`urllib.request.urlopen`) rather than guessing which third-party
library a generation run happened to reach for. DigitalOcean's API
returns non-2xx responses as `urllib.error.HTTPError` (a subclass of
`OSError` with `.code` and `.read()`), not a normal return value — the
driver must catch this explicitly wherever a non-2xx status is an
expected, handled case (404 on `read()`/`delete()`), not let it
propagate as an unhandled exception.

Stating this as a "hard requirement" in the prompt doesn't mechanically
enforce it on its own — `driver_gen.py`'s `validate_driver_source()` only
checks class/method structure and anthropic-related patterns, nothing
HTTP-library-specific, so a violation could otherwise pass gate #1
undetected and only surface when this driver's own test suite fails.
Closed by adding an explicit checklist item to
`prompts/review_driver.md` (Opus gate #1 itself now checks for it) rather
than extending `validate_driver_source()` — that function is generic
across all future providers/drivers, and this urllib convention, while
also generic, doesn't yet warrant a second enforcement point beyond
Opus's review; revisit if a future driver's generation run actually
slips a third-party HTTP library past review.

## Behavior

All request bodies are JSON; base URL `https://api.digitalocean.com/v2`.

### `create(params, credentials)`

- `POST /v2/droplets` with a JSON body built from `params`
  (`name` comes from the resource's `name` in `aiform.md`, not from
  `params` itself — `ResourceSpec.name` is a separate field the
  orchestrator must pass through some other means; see Edge cases).
- **Exactly one API call** — per `PLAN.md` §8 step 3 ("Executes
  `driver.create(params, credentials)` — one real DO API call"), this
  method does **not** poll until the droplet reaches `status: "active"`.
  DigitalOcean's create response is `202 Accepted` with the droplet
  object already in the body, typically `status: "new"` and
  `networks.v4: []` (no IP yet) — that's an acceptable return value.
  Convergence to `"active"` (and a real IP) is picked up later by a
  subsequent `read()` call during the next `plan`/`refresh`, per this
  project's "state is a cache of live reality, refreshed via `read()`"
  design — `create()` is not responsible for waiting out DO's own
  asynchronous provisioning.
- Returns a **flattened** dict whose keys mirror `PARAM_SCHEMA`'s flat
  shape (plus `id`/`status`/`ip_address`), **not** DO's raw nested
  response verbatim — this matters because `update()` diffs this return
  value (as `current`) against `desired` (the flat `params` dict), and
  DO's raw droplet object nests `region`/`image` as objects
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
      "ip_address": <first networks.v4 entry where type == "public", or None>,
  }
  ```
  `ssh_keys`/`backups`/`monitoring` are **not** included in this
  flattening — see Edge cases below for why, and what that means for
  `update()`.

### `read(id, credentials)`

- `GET /v2/droplets/{id}`. **Exactly one API call.**
- `404` → a plain `LookupError` naming the `id` (not
  `aiform.exceptions.ResourceNotFoundError` — that module doesn't exist
  yet; see Out of scope).
- Returns the same flattened attribute shape as `create()`.

### `delete(id, credentials)`

- `DELETE /v2/droplets/{id}`. **Exactly one API call.**
- `204` → success, returns `None`.
- `404` → **also** success, returns `None` — idempotent delete is a
  hard requirement (`aiform/driver.py`'s own docstring: "a 404 from the
  CSP ... is treated as success, not an error"). This is the single
  most important behavior this spec's test suite checks.

### `update(id, current, desired, credentials)`

- Compare `current` vs `desired`. If the diff touches anything other
  than `size` alone (i.e. `region`, `image`, `ssh_keys`, `backups`,
  `monitoring`, or `tags` changed) → raise `DriverUpdateNotSupported`
  naming the changed field(s) in `unsupported_fields`. `region` and
  `image` genuinely cannot be changed in place on DigitalOcean (a region
  move requires a snapshot+recreate; an image change requires a
  destructive rebuild) — this matches `LIKELY_REPLACE_FIELDS` exactly.
  `ssh_keys`/`backups`/`monitoring`/`tags` are deliberately *not*
  attempted in place either, even though DO likely exposes narrower
  endpoints for some of them — out of scope for this MVP driver, see
  below.
- If the diff is `size` alone: DigitalOcean resize
  (`POST /v2/droplets/{id}/actions`) —
  **low-medium confidence, verify against DO's docs if this fails**:
  requires the droplet to be powered off first. The expected sequence:
  1. `POST .../actions {"type": "power_off"}` (skip if `current`
     already shows a non-active/powered-off status).
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
     with different bundled disk sizes), catch that specific failure
     and raise `DriverUpdateNotSupported` naming `size` in
     `unsupported_fields`, falling back to the normal destroy+recreate
     path — don't retry with `disk: true` and risk an irreversible or
     still-rejected disk operation. (The droplet was already powered
     off in step 1; leave it powered off — `create()`/`delete()` handle
     the replace from there, they don't require the old droplet to be
     running.)
  5. On success, poll until the resize action (or the droplet's
     `size_slug`) shows the new size.
  6. `POST .../actions {"type": "power_on"}`, poll until `status ==
     "active"`.
  7. Return the final attributes (equivalent to a `read()`).
  This is the one path in this driver allowed more than one API call —
  unlike `create`/`read`/`delete`, an in-place resize is a genuinely
  multi-step DO operation, not a single request.
- `PLAN.md` §8 step 2 itself anticipates a first-generation `update()`
  might be cruder than this (e.g. "resizes on *any* diff instead of
  scoping to `size`/`region`") and treats that as an Opus-flagged
  *non-blocking concern*, not a rejection. This spec describes the
  target/ideal behavior; the hand-written test suite should check the
  behaviors that actually matter (size changes work in place; genuinely
  non-updatable fields raise `DriverUpdateNotSupported`, never attempt a
  destructive call) without being so strict about the *exact* internal
  call sequence that a reasonable first-draft variation fails outright.

## Edge cases / errors

- **Where does the droplet's `name` come from?** `params` (validated
  against `PARAM_SCHEMA`) has no `name` field — the resource's `name`
  lives on `ResourceSpec`, one level up from what a driver method
  receives. Until `orchestrator.py` exists to resolve this, this spec
  assumes `create()` accepts `name` as a required key inside `params`
  itself in practice (i.e. the orchestrator, once built, merges
  `spec.name` into the `params` dict before calling `create()`) — flagged
  as a real gap `orchestrator.py`'s own spec will need to resolve
  explicitly, not silently assumed away here.
- A `read()`/`delete()` call against an `id` that was never a valid
  droplet ID (malformed, not just missing) is not specially handled —
  DO's API itself returns `404` for a nonexistent ID same as a
  deleted one, so this collapses into the same case.
- `power_off`/`resize`/`power_on` each being independently-async DO
  *actions* (not instant) is why `update()`'s resize path needs polling
  at all, unlike the other three methods — this is a genuine DO API
  constraint, not a design choice made for its own sake.
- **`ssh_keys`/`backups`/`monitoring` can't be round-tripped through
  `read()`.** DO's droplet GET response doesn't return the SSH keys used
  at creation at all (they're write-only), and `backups`/`monitoring`
  don't map onto a clean boolean field the way `PARAM_SCHEMA` expects
  (low confidence, same "verify docs on failure" stance). Since these
  three keys are absent from `create()`/`read()`'s flattened return but
  may be present in `desired` (they're optional in `PARAM_SCHEMA`, but
  commonly set), `update()`'s plain key-comparison sees them as
  "changed" on every `plan` after the first, correctly raises
  `DriverUpdateNotSupported` per the "anything other than size alone"
  rule (not silently ignored), but that means these three params are
  effectively **write-once**: settable at `create()` time, any
  subsequent value forces a destroy+recreate even when nothing
  incompatible with an in-place update is actually happening. A real
  MVP limitation surfaced while fixing the region/image/size shape
  mismatch above, not a new design choice — documented here rather than
  solved, since solving it needs DO API knowledge (exact `features`
  array shape, whether there's a per-droplet SSH-key-list endpoint)
  this spec doesn't have high confidence in.

## Out of scope

- **Polling `create()` to `"active"`** — deliberately not done, per
  `PLAN.md` §8's "one real DO API call" framing (see Behavior above).
- **In-place update support for `ssh_keys`/`backups`/`monitoring`/`tags`**
  — DO likely exposes narrower endpoints for some of these (e.g.
  `enable_backups`/`disable_backups` actions), but supporting them is
  real future work, not required for this MVP driver; any diff touching
  them raises `DriverUpdateNotSupported` and falls back to
  destroy+recreate, same as a genuinely non-updatable field.
- **`aiform.exceptions.ResourceNotFoundError`** — doesn't exist yet
  (`PLAN.md` §1). `prompts/generate_driver.md`'s own generic guidance
  used to name this exact class before this pass — fixed (see below) to
  instruct a plain `LookupError` instead, since an import of a
  nonexistent module would break every method at load time, not just
  the 404-on-read path. Same stance already established for
  `DriverGenerationFailed` in `driver_gen.py`: don't invent
  `exceptions.py`'s types ahead of it existing.
- **Verifying the DigitalOcean API details against current docs** — per
  explicit instruction, build against training-data knowledge first;
  only check DO's actual reference docs if something fails on first
  pass (most likely candidate: the resize power-off requirement).
- **Actually calling `generate_driver()` and running it against a real
  `DIGITALOCEAN_TOKEN`** — this spec is the acceptance criteria for that
  generation step and for the hand-written test suite that checks its
  output; neither the generation call nor a live integration test
  against DO's real API happens as part of writing this spec.
