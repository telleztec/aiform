# specs/planner.md — `aiform/planner.py`

## Purpose

`PLAN.md` §5 steps 5–6: turn a resource's refreshed live `attributes` and
its desired `params` into one `PlanEntry`, doing the deterministic
dict-diff first and only spending a Sonnet call when there's something
that actually needs interpreting. This is what makes a repeat `plan` run
against unchanged input free of Anthropic API calls.

**Two judgment calls made explicit here** (not fully specified in
`PLAN.md`, resolved before writing this spec):

1. **The diff is scoped to `desired`'s keys, not a symmetric dict-diff.**
   `attributes` returned by `driver.read()`/`driver.create()` legitimately
   contains CSP-managed fields with no `params` counterpart at all (e.g.
   a droplet's `ipv4_address`, `status`) — these can never match anything
   in `desired` and would otherwise show up as a permanent, un-resolvable
   diff entry forever. `diff_attributes()` therefore only ever compares
   keys present in `desired`; a key present only in `current` is ignored.
2. **`"destroy"` categorization from a removed `.aiform.md` file is out of
   scope for this module**, despite `PLAN.md` §5 step 6 listing `destroy`
   as one of the four values Sonnet's categorization call can return.
   Deciding that a state entry needs destroying because its `.aiform.md`
   file no longer exists on disk at all is a **set comparison** (state
   keys vs. the currently-discovered file list) with no `desired_params`
   to diff against — a different shape of input than this module's
   `current` vs. `desired` comparison. That comparison belongs to
   `orchestrator.py` (`PLAN.md` §1: "drives plan/apply..."), which already
   owns iterating discovered files against state. Likewise, `aiform
   destroy`'s explicit, user-requested destroy (`PLAN.md` §6) needs no
   LLM categorization at all — the user said what they want directly.
   `PLAN_CATEGORIZATION_SCHEMA` therefore diverges from `PLAN.md`'s literal
   4-value schema and **omits `"destroy"` from the enum entirely** — a
   diff-based comparison has no structural basis to ever conclude a
   resource should stop existing, so rather than declare the option and
   rely on `prompts/diff_plan.md` telling the model not to pick it,
   narrowing the schema makes an errant `destroy` response structurally
   impossible instead of merely discouraged.

## Interface

```python
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
`aiform/parser.py` (not built yet) owns that extraction and its
eventual model, if any; this module only ever forwards whatever it's
given straight into a Sonnet prompt.

### `diff_attributes(current, desired) -> dict[str, dict[str, Any]]`

Deterministic, zero-LLM. For every key in `desired`, compare against
`current.get(key)`; if unequal, the result includes `key: {"current":
<value or None>, "desired": <value>}`. Keys present only in `current`
are never included (judgment call 1 above). An empty result means every
`desired` key already matches `current`.

### `categorize_diff(...) -> PlanEntry`

Always makes exactly one Sonnet call via `llm.implementation_call()`,
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
  (no LLM call, so no LLM-authored rationale) — this is the one `PlanEntry`
  in the system never carrying a model-generated explanation.

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
- `categorize_diff()` raising from `llm.implementation_call()` (network
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
- **`destroy`-by-file-removal categorization** and **`aiform destroy`'s
  explicit destroy entries** — judgment call 2 above; both are
  `orchestrator.py`'s job, constructed without calling into this module.
- **Printing/persisting the plan** (`PLAN.md` §5 step 7) — `cli.py`/
  `orchestrator.py`.
- **`prompts/diff_plan.md`'s exact prose** — real production content
  written alongside this implementation (same as `review_driver.md`/
  `review_plan.md` were for `llm.py`), not fully specified in this
  document beyond what it must instruct the model to do: use `diff`,
  `intent_notes`, `param_schema`, and `likely_replace_fields` to pick one
  of `create`/`update`/`no-op` (see judgment call 2 for why `destroy`
  is declared but never actually exercised from this module's call
  sites), set `likely_replace` conservatively, and write a rationale
  that names the specific field(s) that changed.
