# specs/planner.md — `aiform/planner.py`

## Purpose

`PLAN.md` §5 steps 5–6: turn a resource's refreshed live `attributes` and
its desired `params` into one `PlanEntry`, doing the deterministic
dict-diff first and only spending an `intent-orchestration-model` call when there's something
that actually needs interpreting. This is what makes a repeat `plan` run
against unchanged input free of Anthropic API calls. It also provides
`destroy_entry()`, the deterministic constructor `orchestrator.py` calls
once it's identified a resource as explicitly marked for deletion
(`PLAN.md`'s "Resource deletion") — every `PlanEntry` in the system is
built by this module, whether it came from a diff, an `intent-orchestration-model` call, or
neither.

**Two judgment calls made explicit here** (not fully specified in
`PLAN.md`, resolved before writing this spec):

1. **The diff is scoped to `desired`'s keys, not a symmetric dict-diff.**
   `attributes` returned by `driver.read()`/`driver.create()` legitimately
   contains CSP-managed fields with no `params` counterpart at all (e.g.
   a droplet's `ipv4_address`, `status`) — these can never match anything
   in `desired` and would otherwise show up as a permanent, un-resolvable
   diff entry forever. `diff_attributes()` therefore only ever compares
   keys present in `desired`; a key present only in `current` is ignored.
   **Not this module's concern, but worth knowing about here**: a
   related problem — a `desired` key naming a field the driver's
   `read()` can never populate at all (write-only at the CSP level, e.g.
   a droplet's `ssh_keys`) — is deliberately *not* solved by excluding
   the key from this diff. An earlier version of this fix did exactly
   that, and it silently dropped genuine, intended changes to the field
   (the diff never contained the key at all, so a real edit produced the
   same empty diff as no edit) — caught by `/code-review` and reverted.
   The actual fix lives one layer up, in `orchestrator.py`'s
   `refresh_resource()` (`specs/orchestrator.md`,
   `aiform/driver.py`'s `NON_DIFFABLE_FIELDS`): it carries a prior state
   entry's value for such a field forward across a `read()` refresh
   instead of letting the fresh response blank it out, so by the time
   `current_attributes` reaches this module's `diff_attributes()`, it's
   already correct — an ordinary, unmodified diff then does the right
   thing on its own, with no special-casing needed here at all.
2. **`destroy` is never derived from a diff or an `intent-orchestration-model` call — it's always
   an explicit instruction, constructed deterministically by
   `destroy_entry()`.** `PLAN.md`'s "Resource deletion" section fixes this:
   there is no implicit deletion at all (a resource's `.aiform.md` file
   going missing is never a destroy signal), and destroy is triggered by
   exactly two mechanisms — an `aiform plan destroy` CLI argument, or an
   `AIFORM-DELETE-<name>.aiform.md` filename. Both are identified by
   `orchestrator.py` (file discovery, argument parsing, the
   `AIFORM-DELETE-` prefix check — none of which this module has any
   involvement in), which then calls `destroy_entry(resource_key,
   rationale)` to get the `PlanEntry` — the same division of labor
   `plan_resource()`/`categorize_diff()` already have with their callers,
   just without a diff or an LLM call in the mix. `diff_attributes()` and
   `categorize_diff()` never produce a `destroy` action themselves:
   `PLAN_CATEGORIZATION_SCHEMA`'s `action` enum omits `"destroy"` entirely
   (diverging from `PLAN.md` §5 step 6's literal 4-value list) — a
   diff-based comparison has no structural basis to ever conclude a
   resource should stop existing, so rather than declare the option and
   rely on `prompts/diff_plan.md` telling the model not to pick it,
   narrowing the schema makes an errant `destroy` response structurally
   impossible instead of merely discouraged.

## Interface

```python
def destroy_entry(resource_key: str, rationale: str) -> PlanEntry: ...


def diff_attributes(
    current: dict[str, Any], desired: dict[str, Any]
) -> dict[str, dict[str, Any]]: ...


PLAN_CATEGORIZATION_SCHEMA: dict[str, Any]


def categorize_diff(
    resource_key: str,
    diff: dict[str, Any],
    *,
    intent_notes: list[dict[str, str]],
    param_schema: dict[str, Any],
    likely_replace_fields: list[str],
    drifted_missing: bool = False,
    client: anthropic.Anthropic | None = None,
    llm_config: LLMConfig | None = None,
) -> PlanEntry: ...


def plan_resource(
    resource_key: str,
    current_attributes: dict[str, Any],
    desired_params: dict[str, Any],
    *,
    intent_notes: list[dict[str, str]],
    param_schema: dict[str, Any],
    likely_replace_fields: list[str],
    state_aiform_md_sha256: str | None,
    current_aiform_md_sha256: str,
    drifted_missing: bool = False,
    client: anthropic.Anthropic | None = None,
    llm_config: LLMConfig | None = None,
) -> PlanEntry: ...
```

`intent_notes` is a plain list of `{"concerns_field": str, "guidance":
str}` dicts (§2's `INTENT_NOTES_SCHEMA` items), not a Pydantic model —
`aiform/parser.py` owns that extraction (`specs/parser.md`), wrapping it
in `ParsedResource.intent_notes` alongside the parsed `ResourceSpec` and
file hash; this module only ever forwards whatever it's given straight
into an `intent-orchestration-model` prompt.

### `destroy_entry(resource_key, rationale) -> PlanEntry`

Deterministic, zero-LLM, no diff involved — the constructor for the one
`PlanEntry` shape this module doesn't derive from `current`/`desired` at
all. Always returns `PlanEntry(resource_key=resource_key,
action=PlanAction.DESTROY, rationale=rationale, likely_replace=False)`.
`rationale` is required, not defaulted — the caller (`orchestrator.py`,
not built yet) already knows *why* (an `aiform plan destroy` argument naming
this resource, or the specific `AIFORM-DELETE-` file that named it) and
is expected to say so, the same way `categorize_diff()`'s rationale
always names the field(s) that changed.

### `diff_attributes(current, desired) -> dict[str, dict[str, Any]]`

Deterministic, zero-LLM. For every key in `desired`, compare against
`current.get(key)`; if unequal, the result includes `key: {"current":
<value or None>, "desired": <value>}`. Keys present only in `current`
are never included (judgment call 1 above). An empty result means every
`desired` key already matches `current`.

### `categorize_diff(...) -> PlanEntry`

Always makes exactly one `intent-orchestration-model` call via
`llm.intent_orchestration_call()`,
system prompt `prompts/diff_plan.md` (loaded from `aiform.llm.PROMPTS_DIR`,
same convention as `driver_gen.draft_driver()`), `output_schema=
PLAN_CATEGORIZATION_SCHEMA`. User content is a JSON object:
`{"diff": diff, "intent_notes": intent_notes, "param_schema": param_schema,
"likely_replace_fields": likely_replace_fields, "drifted_missing":
drifted_missing}`. Parses the JSON response into a `PlanEntry` with
`resource_key` filled in from the argument (never asked of the model —
it already knows which resource this is from context, but the caller
owns the canonical `"<provider>.<resource_type>.<name>"` key, not the
model). `PlanEntry`'s own validator normalizes `likely_replace` to
`False` whenever `action != update`, so this function does not
duplicate that check.

### `plan_resource(...) -> PlanEntry`

The no-op short-circuit plus dispatch, per `PLAN.md` §5 steps 5–6:

1. `diff = diff_attributes(current_attributes, desired_params)`.
2. If `diff` is empty, **and** `state_aiform_md_sha256 ==
   current_aiform_md_sha256`, **and** `drifted_missing` is `False` →
   return `PlanEntry(action=PlanAction.NO_OP, ...)` directly. **Zero
   Anthropic API calls** on this path — this is the entire reason the
   no-op short-circuit exists.
3. Otherwise, delegate to `categorize_diff()` with the same
   `resource_key`/`intent_notes`/`param_schema`/`likely_replace_fields`/
   `drifted_missing`, and return its result.

`state_aiform_md_sha256=None` (no prior state entry — brand new resource)
never equals a real hash string, so step 2's condition is always false
for an untracked resource; combined with `desired_params` producing a
non-empty diff against an empty/absent `current_attributes` in the
common case, this reliably routes new resources to `categorize_diff()`
without a separate "is this new" branch.

## Behavior

- `diff_attributes()` performs a plain `!=` comparison per key — correct
  for JSON-shaped values (str/int/bool/None/list/dict) since Python
  already does deep equality for `list`/`dict`. No special-casing for
  nested structures.
- `categorize_diff()` never inspects or branches on the model's returned
  `action` string beyond constructing `PlanAction(data["action"])` — an
  unrecognized value naturally raises `ValueError` from the `Enum`
  constructor, propagated uncaught (a malformed structured-output
  response is a bug in the model call, not a case this module recovers
  from).
- `plan_resource()`'s no-op rationale is a fixed, deterministic string
  (no LLM call, so no LLM-authored rationale) — one of two `PlanEntry`
  shapes in the system never carrying a model-generated explanation; the
  other is `destroy_entry()`'s, which takes its rationale as a caller-
  supplied argument instead of fixing one string, since the caller (not
  this module) is the one who knows which of the two deletion mechanisms
  triggered it.
- `destroy_entry()` doesn't validate that `resource_key` actually
  corresponds to a tracked resource, or that `rationale` is non-empty —
  `PlanEntry`'s own field constraints are the only validation applied;
  this function is a thin, trusted constructor, not a guard.
- **Logging** (`specs/log.md`): `categorize_diff()` logs
  `resource_key=... action=... likely_replace=...` at INFO after the
  LLM decides. `plan_resource()`'s zero-diff no-op fast path (no LLM
  call) logs `resource_key=... action=no-op reason=zero-diff` at
  INFO — the concrete, independently-verifiable evidence for the
  "second `plan create` run makes zero Anthropic API calls" claim
  `PLAN.md`'s MVP walkthrough asks to actually verify, not just assume.

## Edge cases / errors

- `desired` containing a key whose value is legitimately `None` is
  compared the same as any other value — `diff_attributes()` cannot
  distinguish "key absent from current" from "key present with value
  `None`" (both read as `current.get(key)` → `None`); accepted as a
  known limitation, not worth a sentinel for a params shape that in
  practice (`ResourceSpec.params`, `PARAM_SCHEMA`) never uses `None` as
  a meaningful value.
- `desired_params` empty (`{}`) makes `diff_attributes()` return `{}`
  unconditionally, regardless of `current_attributes` — this module does
  **not** treat an empty `desired` as "the user wants this destroyed";
  see judgment call 2 above for why that path lives elsewhere.
- `categorize_diff()` raising from `llm.intent_orchestration_call()` (network
  error, bad API key, etc.) or from `json.loads()`/`PlanEntry` validation
  on a malformed response propagates uncaught — same "let it fail loudly"
  stance as `llm.review_driver()`/`llm.review_plan()` take on their own
  call sites.

## Out of scope

- **Refresh** (`driver.read()` before diffing, `PLAN.md` §5 step 4) —
  `orchestrator.py`'s job; this module only ever receives
  already-refreshed `current_attributes`.
- **Driver existence/trust checks** (`PLAN.md` §5 step 3) —
  `orchestrator.py`, per `PLAN.md` §1's repo layout.
- **Identifying *which* resources are being destroyed** — parsing
  `aiform plan destroy`'s file/resource arguments, detecting the
  `AIFORM-DELETE-` filename prefix, and file discovery generally (`PLAN.md`
  §5 step 1) are all `orchestrator.py`'s job (judgment call 2 above).
  This module only ever constructs the resulting `PlanEntry` once handed
  a `resource_key` and a `rationale` — via `destroy_entry()`.
- **Moving a destroyed resource's file into `.aiform/trash/` and calling
  `driver.delete()`** (`PLAN.md`'s "Resource deletion" / §5 `apply` step
  3) — `orchestrator.py`; this module produces plan-time `PlanEntry`
  objects only, never touches the filesystem or a driver.
- **Printing/persisting the plan** (`PLAN.md` §5 step 7) — `cli.py`/
  `orchestrator.py`.
- **`prompts/diff_plan.md`'s exact prose** — real production content
  written alongside this implementation (same as `review_driver.md`/
  `review_plan.md` were for `llm.py`), not fully specified in this
  document beyond what it must instruct the model to do: use `diff`,
  `intent_notes`, `param_schema`, and `likely_replace_fields` to pick one
  of `create`/`update`/`no-op` (see judgment call 2 for why `destroy`
  isn't one of its options at all), set `likely_replace` conservatively,
  and write a rationale that names the specific field(s) that changed.
