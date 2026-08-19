# specs/parser.md — `aiform/parser.py`

## Purpose

`PLAN.md` §2 / §5 step 2: turn one `.aiform.md` file on disk into a
validated `ResourceSpec` (frontmatter, zero LLM calls) plus
`intent_notes[]` (the prose `## Intent` section, one
`intent-orchestration-model` call via `llm.intent_orchestration_call()`
— skipped whenever there's nothing worth interpreting). Also computes
the file's sha256, the same value
`orchestrator.py` (not built yet) will record as `StateEntry.aiform_md_sha256`
(`PLAN.md` §3) and pass into `planner.plan_resource()` as
`current_aiform_md_sha256`. `specs/planner.md` already names this module
by name as the owner of `intent_notes`'s extraction and its eventual
model, if any (see its Interface section) — this spec is that follow-up.

**Four judgment calls made explicit here** (not fully specified in
`PLAN.md`, resolved before writing this spec):

1. **`intent_notes` stays a plain `list[dict[str, str]]`, not a new
   Pydantic model.** `planner.categorize_diff()`'s interface is already
   fixed to accept exactly that shape (`specs/planner.md`). Wrapping each
   note in a typed `IntentNote` model here would just add a conversion
   step at the parser/planner boundary for no consumer that needs it —
   nothing downstream inspects a note's fields individually outside the
   JSON blob handed to the `intent-orchestration-model`. `ParsedResource` (new, in `aiform/models.py`
   — see below) is still a typed Pydantic wrapper for parser's *overall*
   return value, consistent with every other module's typed return; only
   the notes themselves stay untyped dicts, matching `INTENT_NOTES_SCHEMA`'s
   own shape one-to-one.
2. **The file-hash short-circuit lives inside `parse_file()` itself**,
   mirroring `planner.plan_resource()`'s own embedded no-op check rather
   than pushing the decision up into `orchestrator.py`. `parse_file()`
   takes `previous_aiform_md_sha256: str | None` (the hash
   `orchestrator.py` already loaded from `StateEntry.aiform_md_sha256`,
   or `None` for a resource never before applied) and skips the intent
   `intent-orchestration-model` call whenever it equals the freshly
   computed hash of the current file — `PLAN.md` §5 step 2's "no reason
   to re-extract intent from unchanged prose."
3. **Known limitation of judgment call 2, accepted as-is**: a hash match
   only proves the *file* — frontmatter and prose both — is byte-identical
   to what was last applied; it says nothing about whether the *live*
   resource has drifted on the CSP side since. `PLAN.md` §5 orders parsing
   (step 2) before refresh and diffing (steps 4–5), so `parse_file()`
   cannot know yet whether `planner.plan_resource()` will end up calling
   `categorize_diff()` for a live-drift reason even though the file
   itself didn't change. When that happens, `categorize_diff()` runs with
   `intent_notes=[]` for this call — the model loses the prose's nuance
   for that one rationale, but nothing unsafe follows from it: intent
   notes are advisory context for the `intent-orchestration-model`'s
   rationale text, never the mechanism that gates a destructive action
   (gate #2, `review-orchestration-model`, is). Accepted
   rather than restructuring `PLAN.md` §5's step ordering to fix a
   low-stakes, rare edge case.
4. **A second, independent short-circuit inside `extract_intent_notes()`
   itself**: empty (or whitespace-only) prose always returns `[]` with
   zero LLM calls, regardless of the hash comparison in judgment call 2 —
   there is structurally nothing to extract from no text, most commonly
   the very first parse of a file with no `## Intent` section at all
   (`previous_aiform_md_sha256` is `None` there, so judgment call 2's
   check alone would not have skipped the call). This one loses no
   information (empty prose cannot contain guidance), unlike judgment
   call 2's tradeoff.
5. **Locating the closing `---` delimiter uses PyYAML's own document
   composer (`yaml.compose_all()`), not a naive "any line that strips to
   `---`" scan.** Discovered necessary during `/code-review`: a `---` line
   indented inside a YAML block scalar (e.g. a cloud-init
   `params.user_data: |` value) is content, not a document boundary, and
   the naive scan silently truncated `params` at that false delimiter —
   `parse_frontmatter()` returned a `ResourceSpec` missing everything
   after it, with no exception at all. `yaml.compose_all(content)`'s
   first document's `end_mark.line` is exactly the real closing
   delimiter's line index, correctly ignoring block-scalar content —
   confirmed empirically (`compose_all` is a lazy generator, so pulling
   only the first document via `next()` never touches the prose body
   after it, even when that body isn't valid YAML on its own). Both
   `parse_frontmatter()` and `extract_intent_prose()` now share this via
   a private `_closing_delimiter_index(content, lines) -> int` helper
   that **raises** `ValueError` when no valid closing delimiter is found
   (see `extract_intent_prose()` below — this replaces the "treat the
   whole file as body" fallback the first draft had, which turned out to
   be unreachable through `parse_file()`'s real call graph and untested).

## Interface

```python
INTENT_NOTES_SCHEMA: dict[str, Any]  # PLAN.md §2's literal schema


def compute_sha256(content: str) -> str: ...


def parse_frontmatter(content: str) -> ResourceSpec: ...


def extract_intent_prose(content: str) -> str: ...


def extract_intent_notes(
    prose_intent_text: str,
    *,
    client: anthropic.Anthropic | None = None,
    llm_config: LLMConfig | None = None,
) -> list[dict[str, str]]: ...


def parse_file(
    path: Path,
    *,
    previous_aiform_md_sha256: str | None = None,
    client: anthropic.Anthropic | None = None,
    llm_config: LLMConfig | None = None,
) -> ParsedResource: ...
```

New in `aiform/models.py` (judgment call 1):

```python
class ParsedResource(BaseModel):
    spec: ResourceSpec
    intent_notes: list[dict[str, str]]
    aiform_md_sha256: str
```

### `compute_sha256(content: str) -> str`

`hashlib.sha256(content.encode("utf-8")).hexdigest()`. A small pure
function purely so tests (and `parse_file()`) don't need to compute this
inline — `PLAN.md` doesn't specify bytes-on-disk vs. decoded-text hashing;
this spec fixes it as the UTF-8-encoded *decoded* text, matching how
`parse_file()` reads the file (see Behavior).

### `_closing_delimiter_index(content: str, lines: list[str]) -> int` (private)

Shared by `parse_frontmatter()` and `extract_intent_prose()` (judgment
call 5). The file must start with a line that is exactly `---`
(surrounding whitespace ignored); if not, or if `yaml.compose_all(content)`
finds no first document, or that document's `end_mark.line` doesn't land
on a real `---` line, raises `ValueError` naming the problem (a plain
`ValueError`, no new exception class — matching `aiform/config.py`'s
`_require_mapping` style: this module has only one failure shape to
report at a time here, unlike `driver_gen.DriverValidationError`'s
multi-reason accumulation). A `yaml.YAMLError` from `compose_all()`
(e.g. genuinely malformed YAML syntax) is caught and re-raised as
`ValueError` too.

### `parse_frontmatter(content: str) -> ResourceSpec`

Zero LLM calls. Takes the **full** file content (not a pre-sliced
frontmatter block) and does its own splitting:

1. `closing_index = _closing_delimiter_index(content, lines)` — see above.
2. `yaml.safe_load()` the text between the two delimiter lines. A
   `yaml.YAMLError` is caught and re-raised as `ValueError` (belt and
   suspenders alongside `_closing_delimiter_index()`'s own catch — the
   compose and construct phases can diverge on subtle inputs). A result
   that isn't a `dict` (`None` for an empty block, or a YAML list/scalar)
   also raises `ValueError` — not passed into `ResourceSpec` as-is. A
   `dict` whose keys aren't all `str` also raises `ValueError` naming the
   problem — PyYAML's "Norway problem" resolves an unquoted bareword key
   like `on`/`yes`/`no`/`off` to a Python `bool`, and `ResourceSpec(**data)`
   with a non-`str` key raises a bare, undocumented `TypeError` instead
   of `ValueError`/`pydantic.ValidationError` if this isn't caught first.
3. `ResourceSpec(**data)` — `pydantic.ValidationError` from a missing
   required field, wrong type, or an unexpected key (`ResourceSpec.model_config`
   already sets `extra="forbid"`) propagates **uncaught**, same
   "structured errors need no extra wrapping" stance `planner.py` takes
   on `PlanEntry`/`PlanAction` construction.

### `extract_intent_prose(content: str) -> str`

Zero LLM calls. Takes the full file content. `closing_index =
_closing_delimiter_index(content, lines)` — **raises** `ValueError` when
the content has no valid frontmatter delimiters (this function assumes
well-formed frontmatter, same as `parse_frontmatter()`; in the real
`parse_file()` pipeline, `parse_frontmatter()` always runs first and
would already have raised, so this is only reachable via a direct call —
see judgment call 5). Finds the first line, in the text after the
closing delimiter, that equals `## Intent` exactly (surrounding
whitespace ignored, case-sensitive, matching `PLAN.md` §2's one example
literally). Returns every line after it up to (not including) the next
line starting with `##`, or end of file — stripped of leading/trailing
blank lines. Returns `""` if no such heading exists anywhere after the
frontmatter. A line whose stripped text starts with ` ``` ` or `~~~`
toggles "inside a fenced code block" state; while inside one, a
`##`-prefixed line is treated as ordinary prose, not a section-ending
heading — found necessary during `/code-review`: an Intent section
containing a fenced example whose contents happen to include a
`##`-prefixed line was truncating extraction at that line.

### `extract_intent_notes(prose_intent_text, *, client=None, llm_config=None) -> list[dict[str, str]]`

Per judgment call 4: `prose_intent_text.strip() == ""` returns `[]`
immediately, zero LLM calls. Otherwise, exactly one call to
`llm.intent_orchestration_call()`: `system_prompt=llm.load_prompt("parse_intent.md")`,
`user_content=prose_intent_text` (the raw prose itself, **not**
JSON-wrapped — `PLAN.md` §2's own code sample passes it as plain message
content, unlike `planner.categorize_diff()`'s JSON-serialized user
content), `output_schema=INTENT_NOTES_SCHEMA`. Parses
`json.loads(response_text)["intent_notes"]` and returns it directly — no
further validation beyond what the constrained schema already
guarantees, matching `llm.review_driver()`'s trust in its own schema-shaped
response.

### `parse_file(path, *, previous_aiform_md_sha256=None, client=None, llm_config=None) -> ParsedResource`

1. `content = path.read_text(encoding="utf-8-sig")` — the `-sig` variant
   matches `aiform/config.py`'s existing convention for user-edited text
   files, transparently stripping a BOM rather than letting one corrupt
   the first frontmatter delimiter line.
2. `aiform_md_sha256 = compute_sha256(content)`.
3. `spec = parse_frontmatter(content)` — propagates `ValueError` or
   `pydantic.ValidationError` uncaught; intent extraction never runs for
   a file whose frontmatter doesn't even parse.
4. Per judgment call 2: if `aiform_md_sha256 == previous_aiform_md_sha256`,
   `intent_notes = []`. Otherwise `intent_notes =
   extract_intent_notes(extract_intent_prose(content), client=client,
   llm_config=llm_config)`.
5. Return `ParsedResource(spec=spec, intent_notes=intent_notes,
   aiform_md_sha256=aiform_md_sha256)`.

## Behavior

- `parse_frontmatter()` and `extract_intent_prose()` are independent,
  idempotent functions that each re-derive the small slice of `content`
  they need via the shared `_closing_delimiter_index()` helper — no
  shared "split into (frontmatter, body)" intermediate *object*, just the
  one boundary-finding helper both call.
- A file with no `## Intent` heading at all is not an error anywhere in
  this module — `extract_intent_prose()` returns `""`,
  `extract_intent_notes()` short-circuits on it (judgment call 4).
  `PLAN.md`'s Intent section is documented as prose guidance, never
  described as required.
- If more than one `## Intent` heading exists, only the first is used —
  a natural consequence of "extract until the next `##` line," not a
  special-cased branch; a second `## Intent` heading is itself a line
  starting with `##` and so ends the first section exactly like any other
  level-2 heading would.
- `parse_file()` never touches `.aiform/state.json` or does file
  discovery/globbing — `previous_aiform_md_sha256` is a plain argument
  the caller (`orchestrator.py`, not built yet) already resolved from
  state.
- **Logging** (`specs/log.md`): `extract_intent_notes()` logs
  `notes_count=<n>` at INFO after a real LLM call, and
  `intent_prose_empty=true notes_count=0` at INFO on the zero-LLM
  short-circuit (judgment call 4) — the "no LLM calls happened" path
  gets its own visible signal rather than silence, symmetric with
  `planner.py`'s no-op-path logging.

## Edge cases / errors

- `params`-shaped edge cases (empty `params`, etc.) are `ResourceSpec`'s
  and `planner.py`'s concern, not this module's — `parse_frontmatter()`
  only checks the frontmatter parses to a valid `ResourceSpec`, never
  inspects `params`' contents.
- A frontmatter block containing YAML but not a mapping at the top level
  (e.g. a bare list) raises `ValueError`, same as an unparseable block —
  `parse_frontmatter()` treats "parsed but not a dict" and "didn't parse"
  identically, since neither can become a `ResourceSpec`.
- `parse_frontmatter()`'s "expected a YAML mapping" `isinstance(data, dict)`
  check duplicates the shape of `aiform/config.py`'s `_require_mapping(value,
  key, config_path)` (isinstance check + a `ValueError` naming the actual
  type) rather than calling it directly — flagged by `/code-review`'s
  reuse pass. Left as its own small check rather than refactored to share
  code: `_require_mapping` takes a `config_path: Path` for its error
  message, which `parse_frontmatter(content: str)` doesn't have (this
  module's functions are deliberately pure over `content`, never touching
  the filesystem — see `parse_file()`'s "Out of scope" boundary above);
  generalizing `_require_mapping` to work without a path, or plumbing one
  through this module's otherwise-pure interface, is more churn than a
  three-line duplicated check justifies right now.
- `extract_intent_notes()` raising from `llm.intent_orchestration_call()`
  (network error, bad API key) or from `json.loads()`/a missing
  `"intent_notes"` key on a malformed response propagates uncaught — same
  "let it fail loudly" stance `planner.categorize_diff()` takes on its
  own model call.
- A non-UTF-8-decodable file raises `UnicodeDecodeError` uncaught from
  `path.read_text()` — not specifically handled, same as
  `config.resolve_credentials()` makes no special provision for it either.
- A missing file raises `FileNotFoundError` uncaught — `orchestrator.py`
  already resolved `path` via file discovery immediately before calling
  this, so this is only reachable via a race (the file disappearing
  between discovery and parse), not a case worth a dedicated message.

## Out of scope

- **File discovery / globbing for `*.aiform.md`** (`PLAN.md` §5 step 1)
  — `orchestrator.py`'s job, per `specs/planner.md`'s identical framing
  of the same boundary.
- **Detecting the `AIFORM-DELETE-` filename prefix and routing to a
  destroy `PlanEntry`.** `PLAN.md` §5 step 2 states frontmatter parsing
  is "still required" for such a file (to know *which* resource to
  destroy) but intent extraction is "skipped unconditionally... regardless
  of hash." Since detecting the prefix is `orchestrator.py`'s job (not
  this module's, same division of labor `specs/planner.md`'s
  `destroy_entry()` section establishes), the orchestrator satisfies this
  by calling `parse_frontmatter()` directly for such a file instead of
  `parse_file()` — never reaching this module's hash/intent-extraction
  logic at all. No prefix-awareness is built into `parser.py` itself.
- **Validating `params` against a driver's `PARAM_SCHEMA`** (`PLAN.md`
  §2) — `orchestrator.py`, once a driver is resolved for `(provider,
  resource)`; this module has no notion of drivers at all.
- **Reading/writing `.aiform/state.json`**, and resolving
  `previous_aiform_md_sha256` from it — `orchestrator.py` /
  `aiform/state.py`.
- **`examples/compute.aiform.md`** (`PLAN.md` §1) — a separate,
  standalone deliverable (the MVP walkthrough's fixture file), not needed
  for this module or its tests, which use inline fixture strings.
- **`prompts/parse_intent.md`'s exact prose** — real production content
  written alongside the implementation (same as every other
  `prompts/*.md` file was for its owning module), not fully specified
  here beyond what it must instruct the model to do: read prose Intent
  text and return `intent_notes[]`, each item naming the `params.*` field
  it concerns (or `"general"`) and one atomic, diff/plan-relevant
  instruction, per `PLAN.md` §2's `INTENT_NOTES_SCHEMA` description.
