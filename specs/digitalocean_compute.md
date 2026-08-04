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
driver must catch this explicitly wherever a non-2xx status is a
expected, handled case (404 on `read()`/`delete()`), not let it
propagate as an unhandled exception.

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
- Returns `{"id": str(droplet["id"]), **droplet}` (or an equivalent
  flattening) — at minimum `id` plus whatever attributes DO returned.

### `read(id, credentials)`

- `GET /v2/droplets/{id}`. **Exactly one API call.**
- `404` → `aiform.exceptions.ResourceNotFoundError` (not yet built —
  see Out of scope; a plain exception is acceptable for now, same
  established stance as `config.py`/`state.py`/`driver_gen.py`).
- Returns the same attribute shape as `create()`.

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
  (`POST /v2/droplets/{id}/actions` with
  `{"type": "resize", "disk": true, "size": desired["size"]}`) —
  **low-medium confidence, verify against DO's docs if this fails**:
  requires the droplet to be powered off first. The expected sequence:
  1. `POST .../actions {"type": "power_off"}` (skip if `current`
     already shows a non-active/powered-off status).
  2. Poll `GET /v2/droplets/{id}` until `status == "off"`, bounded
     attempts (a fixed small number, e.g. via a `time.sleep()` between
     checks — must be mockable: `time.sleep`, not a hardcoded blocking
     wait with no seam).
  3. `POST .../actions {"type": "resize", "disk": true, "size": ...}`.
  4. Poll until the resize action (or the droplet's `size_slug`) shows
     the new size.
  5. `POST .../actions {"type": "power_on"}`, poll until `status ==
     "active"`.
  6. Return the final attributes (equivalent to a `read()`).
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
  (`PLAN.md` §1); a plain exception is acceptable here for now, same
  stance already established for `DriverGenerationFailed` in
  `driver_gen.py`.
- **Verifying the DigitalOcean API details against current docs** — per
  explicit instruction, build against training-data knowledge first;
  only check DO's actual reference docs if something fails on first
  pass (most likely candidate: the resize power-off requirement).
- **Actually calling `generate_driver()` and running it against a real
  `DIGITALOCEAN_TOKEN`** — this spec is the acceptance criteria for that
  generation step and for the hand-written test suite that checks its
  output; neither the generation call nor a live integration test
  against DO's real API happens as part of writing this spec.
