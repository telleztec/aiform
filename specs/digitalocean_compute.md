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
LIKELY_REPLACE_FIELDS = ["image", "region", "ssh_keys", "monitoring"]
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
  `update()` uses, but with its own wider budget — `max_attempts=60`,
  `delay_seconds=3` (180s), vs. `update()`'s default `max_attempts=30`,
  `delay_seconds=2` (60s) — because full provisioning from scratch
  commonly takes longer than reconciling an already-existing droplet;
  either way, exhaustion raises `TimeoutError` naming the droplet `id`)
  until `status == "active"`, discarding the transient POST body in
  favor of the converged GET response. **Corrected here**: this
  paragraph previously claimed `create()` was "bounded the same way" as
  `update()`'s default budget — stale relative to the code, which has
  always given `create()` its own wider override; found while touching
  this area for an unrelated reason, not a new change. This was originally
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
  other field.

  **Per-field in-place capability.** Verified against DigitalOcean's
  official OpenAPI specification (the `digitalocean/openapi`
  repository), not recalled from training data:

  | Field | In place? | Mechanism |
  |---|---|---|
  | `size` | yes | `resize` droplet action (see below) |
  | `tags` | yes | `POST`/`DELETE /v2/tags/{name}/resources` |
  | `backups` | yes | `enable_backups`/`disable_backups` droplet actions |
  | `monitoring` | no | no monitoring action exists in `droplet_actions.yml`'s `type` enum; the do-agent runs inside the guest |
  | `region` | no | a region move requires snapshot+recreate |
  | `image` | no | an image change requires a destructive rebuild |
  | `ssh_keys` | no | not cloud-side state at all — see Edge cases |

  `update()` partitions `diff_fields` into the in-place-capable set
  (`size`, `tags`, `backups`) and everything else. **If any
  replace-forcing field is present, raise `DriverUpdateNotSupported`
  before mutating anything**, with **only** the genuinely
  replace-forcing fields in `unsupported_fields` — not the whole diff.
  A `size`+`region` diff names `region` alone: the `size` half is
  irrelevant once a replace is required.

  This is still all-or-nothing per diff — `aiform/driver.py`'s contract
  and `prompts/generate_driver.md` both require it, and nothing here
  introduces partial application. Only the definition of
  "replace-forcing" is corrected. The previous rule was
  `diff_fields != ["size"]`, which destroyed and recreated a droplet on
  a tags-only edit (issue #77) — the exact Terraform `ForceNew`
  pathology `README.md` names as the reason aiform exists — and which
  also rejected a `size`+`tags` combination even though both halves are
  individually supported.

  **Ordering: `size` first, then `tags`, then `backups`.** The resize is
  the only step that can raise `DriverUpdateNotSupported` mid-flight (a
  `400`/`422` rejection, step 4 below), and that exception makes
  `orchestrator.py`'s `apply_plan()` run a single-resource gate #2
  review followed by a `confirm_fn("Replace ...?")` the user may
  decline. Tags mutated before that point would leave the droplet
  altered while state says otherwise. Hence the invariant, which this
  driver's test suite asserts mechanically:

  > `update()` never raises `DriverUpdateNotSupported` after mutating
  > anything other than a power state it restores.

  The reverse failure needs no such guarantee because it is
  self-healing: if the resize succeeds and a later `tags` call fails
  transiently, a real error propagates, state is never written, and the
  next `plan` refreshes and converges on the remaining diff.

  All seven fields are diffable (see Behavior's `read()` note, plus the
  carry-forward above for `ssh_keys` specifically), so the four that
  remain replace-forcing are a real CSP constraint rather than a masked
  reliability gap. **One caveat, now closed in the planner and still
  open in this driver**: `tags` was compared with `!=` on a list, both
  here and in `planner.py`'s `diff_attributes()`, so it registered as
  "changed" whenever the two lists differed as sequences rather than as
  sets. `planner.py`'s half is fixed — this driver declares
  `UNORDERED_FIELDS = ["tags"]` and the planner now compares it as a
  multiset (`specs/unordered_fields.md`, closing #110). `update()`'s own
  local `diff_fields` comparison is deliberately unchanged; see the
  addendum at the end of this spec for why its cost is zero API calls.

  There were two ways in, and they now have **different** answers:

  - **DO returning the same tags in a different order.** Closed. The
    planner compares `tags` as a multiset, so element order can no
    longer produce a diff. Whether DO ever actually reorders is still
    unverified and now does not matter — `specs/unordered_fields.md`
    explains why no live probe was written to find out (briefly: a
    hash-set-backed store can be order-stable at two tags and unstable
    at nine, so a passing probe would prove nothing while reading as
    evidence that ordered comparison is safe).
  - **A duplicate entry in the user's own `tags:`.** Still open, and
    deliberately so. This needs no DO misbehavior at all — DO stores a
    set, so `["web", "web"]` can never match what comes back. The
    multiset semantics chosen in #110 keep reporting that as a diff
    **on purpose**: collapsing it would mean `set()` comparison, which
    would also hide a genuinely duplicated record elsewhere. The cost is
    a perpetual no-progress `update` — an `intent-orchestration-model`
    call per `plan` and one `GET` per `apply`, converging never. (Before
    issue #77 it was worse: such a mismatch destroyed and recreated the
    droplet on every `apply`.) The real fix is rejecting a duplicated
    tag as a malformed value in `_reject_malformed_values()`, alongside
    the checks already there — not weakening the comparison. Not done
    here; it is this driver's own follow-up.

  The live lifecycle test asserts convergence to `no-op` immediately
  after its tags edit (`specs/system_test.md` case 6b), which now guards
  the second path specifically.
- **If `size` is in the diff** (this step runs first, per the
  ordering above): DigitalOcean resize
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
  4. The resize action can fail with an `HTTPError`, and **not every
     `HTTPError` here means "DO rejected this specific resize."** Only
     `exc.code in _RESIZE_REJECTED_STATUSES` (a module-level constant,
     `(400, 422)`) is treated as a genuine rejection (DO's conventional
     status for "your request is well-formed but this particular resize
     is invalid" — e.g. the target size requires a disk-size change DO
     can't perform live, moving between families with different bundled
     disk sizes). Named as a constant rather than an inline tuple
     specifically so there's one place in the code to update it, not a
     literal repeated at each call site — flagged during `/code-review`
     on this same PR. Any other status — `429` (rate-limited), a `5xx`,
     `401`/`403` (auth) — is a transient or unrelated CSP failure,
     **not** evidence this resize is unsupported, and must **not** be
     converted into `DriverUpdateNotSupported`: doing so would
     misclassify a retriable/transient failure as a permanent one and
     trigger a destructive destroy+recreate for a resize that might
     have succeeded on retry. Caught by `/code-review` (gate #1) on the
     original version of this driver, which caught *every* `HTTPError`
     unconditionally here. **Deliberately not expanded to `403`/`409`/
     `423`** despite a later `/code-review` suggestion on this same PR:
     `403` reads as a permission/credential problem, not "this resize is
     invalid" — misclassifying *that* into a destroy+recreate is exactly
     the dangerous pattern this fix exists to prevent, since a
     permission problem doesn't go away by destroying the droplet.
     `409`/`423` read as "droplet locked/busy with another action,"
     which is transient (retry once the lock clears), not a permanent
     rejection either. Left as `(400, 422)` unless a concrete, observed
     DO status code demonstrates otherwise — not expanded speculatively.
     - In **both** cases (genuine rejection or not), first **power the
       droplet back on** if this call itself powered it off
       (`POST .../actions {"type": "power_on"}`, poll until `status ==
       "active"`) — regardless of *why* the resize failed, a droplet
       this driver itself powered off shouldn't stay off as a side
       effect of the failure. **This restore call is itself wrapped in
       its own `try`/`except (urllib.error.URLError, TimeoutError,
       http.client.HTTPException, OSError, json.JSONDecodeError)`** —
       matching exactly the tuple `tests/system/conftest.py`'s
       `wait_until_droplet_gone()` already uses for the identical
       `urlopen()` → `response.read()` → `json.loads()` call shape
       against the same DigitalOcean droplet-polling API (established
       and tested in an earlier commit, `fc2dd1d`: "urllib only wraps
       `OSError` from the request itself into `URLError`;
       `RemoteDisconnected`/`IncompleteRead` from `getresponse()`/
       `read()`, and a truncated body, propagate unwrapped"). An
       earlier version of this PR narrowed the catch to just
       `(urllib.error.HTTPError, TimeoutError)`, on the mistaken belief
       those were "the only exceptions `_do_action_and_wait`/
       `_poll_until` can actually raise" — a second `/code-review` pass
       caught that this directly contradicts `fc2dd1d`'s own tested
       finding against the identical pattern, and that an uncaught
       `URLError`/`OSError`/`JSONDecodeError` here would silently lose
       the original resize failure's context exactly the way the bare
       `except Exception` this replaced originally could — just via a
       different exception type. `urllib.error.HTTPError` is a subclass
       of `URLError`, so it's still covered without listing it
       separately. A *genuinely* unexpected type (never observed
       against this API, unlike the five above) still propagates
       immediately rather than being folded into a generic "restore
       also failed" message that would make an unrelated bug harder to
       distinguish from a real DO-API restore failure. If the restore
       *does* raise one of the five, the original resize `exc` must
       not be silently replaced by the restore failure (a second
       `/code-review` finding on this same PR — a bare, unguarded
       restore call meant a failed restore would mask the actual resize
       error and skip the classification below entirely). On a
       compounding failure, raise a plain `RuntimeError` naming the
       droplet `id`, the original resize failure, and the restore
       failure, chained `from` the original resize `exc` (not the
       restore failure) — both are visible in the message and the
       traceback, and this case deliberately isn't classified into
       `DriverUpdateNotSupported` either way (a compounding failure like
       this is unusual enough to warrant a loud, undisguised error
       rather than a guess).
     - **Only then**, branch: if the status was a genuine rejection
       (`400`/`422`), raise `DriverUpdateNotSupported` naming `size` in
       `unsupported_fields`, falling back to the normal destroy+recreate
       path. Restoring power state *before* raising matters here: `size`
       is deliberately not in `LIKELY_REPLACE_FIELDS`, so this specific
       `DriverUpdateNotSupported` triggers the orchestrator's
       single-resource `review-orchestration-model` safety-gate pause
       (`aiform/driver.py`'s own docstring) before any destroy+recreate
       proceeds — the entire point of that gate is a human/
       `review-orchestration-model` veto *before* anything destructive
       happens, which is defeated if the droplet is already sitting
       powered off while the gate is being evaluated. Don't retry the
       resize itself with `disk: true` — only restore power state, then
       raise.
     - Otherwise (any other status), **re-raise the original
       `HTTPError`** after the power-state restore — it propagates as a
       genuine failure (through `aiform/orchestrator.py`'s `apply_plan()`
       update branch's own `except Exception`, wrapped into
       `DriverExecutionError`), not a silent trigger for replace.
     - DO's JSON error body (when present) is extracted defensively (a
       malformed or already-consumed body must never raise a *new*
       exception mid-error-handling) on **both** branches, not just the
       rejection one — a third `/code-review` finding: the original
       version only extracted it for the `DriverUpdateNotSupported`
       message, silently dropping DO's own diagnostic text (e.g. "rate
       limit exceeded, retry after 30s") for exactly the transient
       failures a human would most want it for. On the re-raise branch
       *and* the compounding-failure branch above, the message is
       folded into the relevant `HTTPError` object's own `.msg`
       (mutating it in place, e.g. `"Too Many Requests: rate limit
       exceeded, retry after 30s"`) via one shared private helper,
       `_fold_do_error_into_exc()`, rather than each call site
       re-extracting and re-mutating separately — a fourth `/code-review`
       finding: the extract-and-mutate pattern was originally copy-pasted
       for the resize `exc` and the restore `restore_exc` instead of
       factored out. Mutating in place (rather than wrapping in a new
       exception type) keeps `exc.code`/`isinstance(exc, HTTPError)`
       intact for anything further up the stack that might care, while
       still enriching what `str(exc)` shows. `_fold_do_error_into_exc()`
       returns the extracted DO message itself (`str | None`), not a
       bare bool — a caller needs the message's own text too (e.g. this
       driver's structured-logging `do_message` extra field, added
       merging this driver's logging work into the resize-classification
       fix; re-deriving it via a second `_do_error_message()` call
       instead would read the already-consumed body again and silently
       get `None`) as well as the true/false signal, and the string's
       own truthiness already serves as that signal. On the rejection
       branch, `DriverUpdateNotSupported`'s `reason` string uses that
       return value's truthiness to decide whether to append `exc.msg`
       at all —
       still built from the now-enriched `exc.msg` when there's
       something to add, rather than re-appending the same DO message a
       second time through an independent extraction (a fifth
       `/code-review` finding: the same diagnostic text was threaded
       through two unsynchronized `if do_message:` blocks, one place to
       drift out of sync with the other on a future change) — but *not*
       unconditionally: a sixth `/code-review` finding, on the fix for
       the fifth, caught that reusing `exc.msg` unconditionally changed
       behavior for a 400/422 with no parseable DO body, which
       previously omitted the `: ...` suffix entirely and would
       otherwise silently start including urllib's bare HTTP reason
       phrase (e.g. `": Unprocessable Entity"`) instead — untested,
       and not what the fifth finding's fix was meant to change. Message
       extraction plays no role in the classification decision either
       way, which is status-code-only: every resize-action `HTTPError`
       at this point in the code is unambiguously about *this* resize
       request (no concurrent request shares this call site), so
       there's no genuine ambiguity for a body-content heuristic to
       resolve.
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
- **If `tags` is in the diff** (runs after the resize step, if any).
  Compute the two sets against `current.get("tags", [])`:
  `add` is every tag in `desired["tags"]` not currently present,
  `remove` every currently-present tag absent from `desired["tags"]`.
  Then:
  1. For each added tag, **ensure the tag object exists first**:
     `GET /v2/tags/{name}`, and on `404`, `POST /v2/tags {"name": ...}`.
     Creating a droplet with `tags` auto-creates them, but
     `POST /v2/tags/{name}/resources` does **not** — DigitalOcean's
     OpenAPI documents a `404` response on that endpoint for exactly
     this case. Deliberately *not* "assign, and create on a `404`":
     that would require deciding whether the `404` meant the tag or the
     droplet, and this driver has an explicit rule against inferring
     CSP status-code semantics from an ambiguous response (see
     `_RESIZE_REJECTED_STATUSES` above). Costs two requests for an
     existing tag and three for a new one, only when `tags` is actually
     in the diff.
  2. `POST /v2/tags/{name}/resources` per added tag and `DELETE
     /v2/tags/{name}/resources` per removed one, both with body
     `{"resources": [{"resource_id": <id>, "resource_type": "droplet"}]}`
     — `resource_id` is a **string** per DO's `tags_resource.yml`, even
     though a droplet id is numeric. `204 No Content` on success.
  3. The tag name is interpolated into the request path, so it is
     URL-quoted (`urllib.parse.quote(tag, safe=":")`) so a malformed
     name cannot inject an extra path segment. `:` is deliberately
     left unescaped: DO's own pattern is `^[a-zA-Z0-9_\-\:]+$` with a
     255-character cap, and DOKS really does use colon-bearing tags
     (`k8s:<cluster-id>`), so percent-encoding it would risk a `404` on
     the existence check for a name `create()` accepts happily. With
     `:` exempted, quoting is a no-op for every name valid under that
     pattern — which is the property the code comment claims, and it
     was false while `safe=""` encoded the colon.
  - **Tag names are case-stable.** DigitalOcean canonicalizes the
    capitalization at first creation and the URL must use that
    canonical form. Asking for `PROD` when `prod` already exists is a
    real, if unlikely, failure — it surfaces as DO's own error via the
    rule below, never as a silent no-op.
  - **This path never raises `DriverUpdateNotSupported`.** Any
    `HTTPError` propagates as a genuine driver error. A tag DO rejects
    as invalid would be rejected at `create()` too, so a
    destroy+recreate is no remedy — converting it would be exactly the
    misclassification `aiform/driver.py`'s `update()` docstring and
    `prompts/review_driver.md` item 4 forbid.
  - **Knowledge-confidence**: the tag-must-exist-first behavior is read
    off DigitalOcean's published OpenAPI responses, not observed live.
    Confirm it in the system-test run — if
    `POST /v2/tags/{name}/resources` turns out to auto-create after
    all, the existence check is redundant but harmless.
- **If `backups` is in the diff** (runs last).
  `POST /v2/droplets/{id}/actions` with `{"type": "enable_backups"}` or
  `{"type": "disable_backups"}`; both are members of
  `droplet_actions.yml`'s `type` enum. Poll until
  `("backups" in droplet["features"]) == desired["backups"]`, through
  the same `_poll_until` seam every other action here uses, so a
  DO-side delay surfaces as the usual `TimeoutError` naming the step
  rather than as a wrong return value.
  - **No `backup_policy` is sent.** `enable_backups` accepts an
    optional one and defaults to daily when it is omitted;
    `PARAM_SCHEMA` models `backups` as a bare boolean, so there is
    nothing for this driver to express. A weekly policy is therefore
    inexpressible today — a real limitation, named rather than hidden,
    and a `PARAM_SCHEMA` change if it is ever wanted.
  - Same never-`DriverUpdateNotSupported` rule as `tags` above.
- **Malformed `tags`/`backups` values are rejected before any mutation.**
  Nothing upstream validates `params` against `PARAM_SCHEMA` — the
  `create()` docstring in `aiform/driver.py` claims the orchestrator
  does, and `specs/orchestrator.md`'s judgment call 2 records that it
  does not — so these values reach `update()` exactly as YAML parsed
  them. That was harmless while every `tags`/`backups` diff went
  straight to a replace; it is not harmless now that both are applied
  locally, before any API call could reject them:
  - `tags: web` (a scalar, not a one-item list) is the string `"web"`.
    Iterating it yields `added = ["w", "e", "b"]` and
    `removed = <every current tag>` — junk tags created on the account
    and the real ones detached from a live droplet. `tags:` with no
    value is `None`, which raises `TypeError` mid-iteration.
  - `backups: "false"` is a non-empty string, so a `bool()` coercion
    reads it as `True` and switches **billed** backups on for a user
    who asked for them off.
  So `update()` checks that `tags` is a list of **non-empty** strings
  and `backups` a real `bool`, raising `ValueError` that names the field
  and the value it got. Non-empty matters on its own: an empty name
  makes the existence check `GET /v2/tags/`, which is DO's *list*
  endpoint and answers `200`, so the tag would be reported as already
  present and the assignment would then fail at DO.

  **`ValueError`, not `DriverUpdateNotSupported`**: a malformed value is
  not a diff the CSP declined, and the destroy+recreate that exception
  triggers would only hand the same value to `create()`. It propagates
  through `orchestrator.py`'s `apply_plan()` as a
  `DriverExecutionError` like any other driver failure, reaching the
  user as one `Error:` line and exit code 2.

  **The check runs before the replace-forcing partition, not after it**
  — the one placement detail that carries weight. An edit that changes
  `region` *and* mistypes `tags` would otherwise raise
  `DriverUpdateNotSupported(["region"])` first; the user approves the
  replace, `delete()` succeeds, the state entry is dropped, and then
  `create()` puts the malformed value in the POST body, where DO
  rejects it. Droplet destroyed, nothing rebuilt. Validating first
  turns that whole sequence into a `ValueError` the user fixes in
  YAML. This is a driver-local guard on the two
  values it acts on directly, not a general fix — the general gap is
  tracked separately.
  An integer `0`/`1` is *not* rejected, and needs no special case:
  Python has `0 == False`, so an int matching the live value produces
  no diff at all and never reaches the guard, while one that does not
  match is caught by it.
- **The returned attributes come from one final `GET`**, issued after
  *all* mutation steps rather than reusing the resize step's own last
  poll response — otherwise a `size`+`tags` update would return the
  pre-tag droplet. `ssh_keys`/`backups`/`monitoring` are still echoed
  from `desired`/`current` exactly as before, which stays correct in
  both directions: when the field is in the diff, `desired` holds the
  value just applied; when it isn't, `current`'s value is preserved.
  This costs one extra `GET` on a size-only update, on a path that
  already makes six or more requests.
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
- **Logging** (`specs/log.md`). This is the first driver to log at all
  — every other driver-visible log line comes from `orchestrator.py`'s
  `_call_driver()` wrapper, which only sees this driver's *outer*
  boundary (`operation=update duration_ms=<total> outcome=success`,
  covering the whole `power-off → resize → poll → power-on` sequence
  as one opaque span). Found insufficient after a real diagnostic
  session: a slow or stuck resize gives no way to tell *which* of the
  four steps it's stuck in from that one line alone.
  - `logger = logging.getLogger("aiform.driver.digitalocean.compute")`
    — a hardcoded literal, **not** `logging.getLogger(__name__)`.
    `orchestrator.py`'s `load_driver()` execs this file via
    `importlib.util.spec_from_file_location(f"aiform_driver_{provider}_{resource_type}",
    ...)`, so `__name__` inside this module at runtime is
    `"aiform_driver_digitalocean_compute"` — not a dotted descendant of
    the `"aiform"` logger `aiform/log.py`'s `configure()` attaches
    handlers to. Confirmed empirically before writing this (not
    assumed): a logger built from that synthetic name never reaches
    either handler, silently — no exception, just missing output.
  - `_poll_until()` logs once per call (not per poll attempt — the
    existing busy-wait exclusion in `specs/log.md` still applies to
    the individual `GET`s inside its loop): INFO with
    `id`/`step`/`attempts_used`/`duration_ms`/`outcome=success` on
    success, ERROR with the same shape plus `outcome=timeout`
    immediately before raising `TimeoutError`. `duration_ms` is
    `aiform.log.elapsed_ms(start)` — the same helper `llm.py`/
    `orchestrator.py` use, not a hand-rolled `round((time.monotonic() -
    start) * 1000)` — this driver imports `aiform.log` for it (caught
    by `/code-review`: the original version reimplemented the formula
    inline at both call sites instead of reusing the helper this same
    PR introduced specifically to eliminate that duplication). Since
    `create()` also calls `_poll_until()` (`step="create"`), it gets this too, for
    free — an added, not redundant, precision beyond
    `_call_driver()`'s own `operation=create` line, since
    `attempts_used` tells you how many `GET`s DO's convergence
    actually took.
  - `update()`'s resize path logs once, INFO, on entering the sequence
    (`id`/`status`/`current_size`/`target_size`) — the context every
    subsequent step-level line needs but doesn't itself carry.
  - `update()`'s `tags` step logs once, INFO, before issuing any tag
    request (`id`/`tags_added`/`tags_removed`); the individual
    `GET`/`POST`/`DELETE` calls are not logged separately. This is the
    only multi-request step here with no polling of its own, so that
    one line is the sole record of what it set out to do if a later
    call in the loop fails partway through.
  - `update()`'s `backups` step logs once, INFO, on entry (`id`/`step`,
    the latter being `enable_backups` or `disable_backups`). Its
    convergence poll is already covered by `_poll_until`'s own line,
    under `step` `enable-backups`/`disable-backups`.
  - The `except urllib.error.HTTPError` block around the resize action
    logs WARNING **once**, but the message and meaning depend on which
    of the two classification branches (`specs/digitalocean_compute.md`'s
    "Behavior" section above, step 4) is actually taken — this split
    exists because the classification itself (genuine rejection vs.
    transient/unrelated failure) didn't exist yet when this logging was
    first written; merging the two together kept only one, now-wrong
    unconditional message ("falling back to destroy+recreate") that no
    longer held once the re-raise branch existed too:
    - **Genuine rejection** (`exc.code in _RESIZE_REJECTED_STATUSES`):
      `"DigitalOcean rejected the in-place resize; falling back to
      destroy+recreate"` — accurate here, since this branch really does
      lead to `DriverUpdateNotSupported` and the orchestrator's
      destroy+recreate fallback.
    - **Everything else** (transient/unrelated, re-raised as a real
      error): `"DigitalOcean's resize action failed with an unrecognized
      status; propagating as a genuine driver error, no replace
      triggered"` — deliberately avoids reusing the phrase "falling
      back to destroy+recreate" even in negated form (a first version
      wrote "...rather than falling back to destroy+recreate", which
      still contains that exact substring — caught by the regression
      test asserting the phrase's absence, which failed against that
      wording). This is a real driver-execution failure with no further
      handling above it, and the log file is the only durable record
      once it propagates.
    Both share the same `extra` shape — `id`, `target_size`,
    `http_status` (`exc.code`), and best-effort `do_message`: DO's own
    `{"message": "..."}` JSON body, extracted via `_do_error_message()`
    and threaded through `_fold_do_error_into_exc()` (which returns the
    extracted message itself, not just a bool, specifically so a caller
    has the bare text for a structured field like this one — reusing
    the now-prefixed `exc.msg` instead would have logged `"Unprocessable
    Entity: disk size cannot be decreased"` rather than the bare `"disk
    size cannot be decreased"` a consumer of this field expects; caught
    merging this driver's structured-logging work into the
    resize-classification fix). `do_message` is the single
    highest-value diagnostic field either warning carries — DO's own
    stated reason (e.g. a disk-size-class mismatch, or a rate-limit
    message) is exactly the detail an operator, or an LLM reviewing the
    log afterward, needs to tell "this needs a destroy+recreate because
    X" (or "this was transient, safe to retry") apart from "this driver
    has a bug." Never raises on a malformed/absent body — the field is
    just omitted (per `_KeyValueFormatter`'s existing `None` handling)
    rather than crashing error handling itself.

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
- **Why `ssh_keys` is replace-forcing is a stronger claim than "DO has
  no API for it".** The `update()` section above gives the API reason;
  the underlying one is that `ssh_keys` is not cloud-side state in the
  first place. DigitalOcean's own create schema (`droplet_create.yml`)
  describes the field as *"the IDs or fingerprints of the SSH keys that
  you wish to embed in the Droplet's root account upon creation"* —
  cloud-init writes the public keys into the guest's
  `/root/.ssh/authorized_keys` at first boot, and DO keeps no record of
  them afterwards. That, not a missing endpoint, is the root cause of
  `read()`'s inability to recover the field, and therefore of
  `NON_DIFFABLE_FIELDS` and `refresh_resource()`'s carry-forward
  existing at all. Worth stating plainly, because it makes the current
  behavior questionable rather than merely inconvenient: a
  destroy+recreate converges against a *remembered* value aiform can
  never observe, so a droplet whose `authorized_keys` someone already
  fixed by hand is still destroyed on the next `plan`. `rebuild` is not
  an escape hatch either — it preserves the id and IP but wipes the
  disk and re-injects the droplet's *original* keys, not new ones.
  Whether the honest behavior is a hard refusal naming the field rather
  than a silent replace is tracked as its own issue; deliberately not
  changed here, since the orchestrator reads
  `DriverUpdateNotSupported` as "replace it" and a refusal would need a
  distinct error path and a `PLAN.md` §4 contract change.

## Out of scope

- **In-place update support for `ssh_keys`/`monitoring`** — neither has
  any DigitalOcean API surface at all (see the capability table under
  `update()` above), so a diff touching either raises
  `DriverUpdateNotSupported` and falls back to destroy+recreate.
  `backups` and `tags` were listed here too until issue #77; both are
  now applied in place, since DO does expose narrower endpoints for
  them and the destroy+recreate this deferral implied was destroying
  live droplets on trivial edits.
- **A live integration test against DO's real API with a real
  `DIGITALOCEAN_TOKEN`** — this spec and the hand-written test suite that
  checks an implementation against it are both built and validated
  against mocked `urllib.request.urlopen`, not a live DO account.
- **Running this driver's implementation through `generate_driver()`** —
  not done, and not pending either: `generate_driver()` has no caller,
  and the deliberate `aiform driver create` flow that would call it
  isn't being built (see `PLAN.md`'s "Driver curation" section). This
  spec accordingly serves as the hand-implementation guide, not only
  generated-output acceptance criteria.

## Addendum: `UNORDERED_FIELDS = ["tags"]` (`specs/unordered_fields.md`)

This driver declares `tags` as order-insensitive, so a DigitalOcean response
listing the same tags in a different order than the user's `.aiform.md` no
longer produces a permanent diff. See `specs/unordered_fields.md` for the
mechanism and for why no live probe of DO's actual ordering behavior was
written.

`update()`'s own local diff (`diff_fields`) still uses ordered comparison and
is deliberately unchanged -- out of scope per #110. The practical cost is
small: `update()` only runs once the planner has already produced a non-empty
diff, and a reordered-but-equal `tags` value reaching `_apply_tag_changes()`
computes empty add/remove sets, so it issues **zero** API calls. `tags` is in
`_IN_PLACE_UPDATABLE_FIELDS`, so it can never force a replace either.
