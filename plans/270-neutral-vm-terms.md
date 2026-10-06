# Plan: #270 — neutral VM terms, and the firewall's VM-reference schema behind the driver

Point-in-time record, per `plans/README.md`. Not rewritten as implementation proceeds; a changed approach is a new plan.

## Approval

**Not yet approved.** No implementation agent is spawned and no code, spec or test file is written until the repo owner approves this specific plan (`PROCESS.md`, "Before the loop: plan and get explicit approval"). Approval is recorded in the implementing PR's `## Plan` section, not by editing this file.

Baseline taken while writing this plan, on `890c1da`: `env -u DIGITALOCEAN_TOKEN -u ANTHROPIC_API_KEY python -m pytest` from the worktree gives 2457 passed, 45 deselected (the `system` marker).

## What a maintainer relies on

Whoever adds the second provider expects the provider-neutral layers to read as neutral: no DigitalOcean vocabulary and no DigitalOcean field names outside `drivers/digitalocean/` and clearly provider-keyed data. Today `aiform/orchestrator.py` hard-codes DigitalOcean's firewall rule schema and names `droplet_id` in neutral variables, and `tests/system/conftest.py` exposes a helper whose signature names `droplet_ids`.

## Inventory (grep, verified on `890c1da`)

`grep -rn -i droplet aiform/` = **49** hits. Unit throughout: matching lines (`grep -rn ... | wc -l`), never occurrences. Per-file rows below sum to the file's total; file totals sum to 49 (17 + 15 + 6 + 3 + 3 + 2 + 2 + 1).

| File | Hits | Category | Disposition |
|---|---|---|---|
| `aiform/orchestrator.py` | 17 | 13 repair code: `_REPAIRABLE_EDGES` (942, 945), `_is_repairable` call (959), comment + `_rule_names_droplet` (962, 966, 970, 971), `_as_int_id` comment (1618), `_apply_repair` (1641, 1642, 1645, 1646, 1650) | **Move/rename.** Table and rule scan leave the file; remaining names become VM/target terms |
| | | 4 neutral comments: 149, 150 (`_will_be_recreated`), 1063, 1064 (`_PROTECTS`) | **Rename** to VM |
| `aiform/cli.py` | 15 | 1 DigitalOcean example scaffold, line 62 (`_EXAMPLE_COMPUTE_AIFORM_MD`, frontmatter `provider: digitalocean`) | **Stays**: provider-keyed example data |
| | | 5 lines of user-facing strings (4 strings): 454, 455 (init key message, one string), 615, 617, 652 (scope-check details) | **Rename** (owner question 2); no test asserts on the droplet wording (`tests/test_cli.py:859` asserts only `"unverified"`); `specs/cli.md:339, 359` document the `(droplet scope unverified)` string |
| | | 7 scope probe: `_check_droplet_scope` call and def and docstring and `config` lookup (552, 593, 596, 600), comments 550 and 613, and `"droplets" not in body` (635), a DigitalOcean response key read in a neutral layer | **Rename**; 635 becomes data from the provider table (below). Siblings of the function name and the phrase "droplet scope" outside `aiform/` are listed under the sibling greps |
| | | 2 comments: 467, 1158 | **Rename** |
| `aiform/ssh.py` | 6 | all comments (85, 105, 108, 281, 284, 285) | **Rename** to VM |
| `aiform/driver.py` | 3 | contract docstring examples (161, 198, 260); `PLAN.md:809`, `:845` carry the same wording | **Rename**; mirror in `PLAN.md` (wording only, owner question 5) |
| `aiform/observability.py` | 3 | comments/docstring (147, 446, 621) | **Rename** |
| `aiform/config.py` | 2 | `PROVIDER_DROPLET_PROBES` name (27) and DigitalOcean URL (28) | Table **name becomes neutral**; the URL line is provider-keyed data and **stays** |
| `aiform/references.py` | 2 | comments (70, 260) | **Rename** |
| `aiform/compare.py` | 1 | docstring (133) naming `sources.droplet_ids` | **Rename** to a neutral example |

DigitalOcean field names beyond the word "droplet", in `aiform/` (`grep -rnw NAME aiform/ | wc -l`, lines; `droplet_id` and `droplet_ids` are distinct words under `-w`):

| Name | Lines | Where | Disposition |
|---|---|---|---|
| `droplet_ids` | 5 | `compare.py:133`; `orchestrator.py:945, 970, 1618, 1646` | all move or rename (already counted in the table above) |
| `droplet_id` | 6 (7 occurrences: `:1650` holds two) | `orchestrator.py:966, 971, 1641, 1642, 1645, 1650` | rename |
| `inbound_rules`, `outbound_rules` | 1 line, holding both | `orchestrator.py:967` | move into the driver declaration |
| `sources` | 5 | `compare.py:133`; `orchestrator.py:405` (unrelated English "two sources"), `962, 969, 1645` | 4 move or rename; 405 stays (not a field) |
| `destinations` | 3 | `orchestrator.py:962, 969, 1645` | move or rename |
| `"digitalocean"` literals | 31 across 9 files | provider keys in tables, spec cross-references | **Out of scope** (not "droplet"); the orchestrator's `_PROTECTS` table keeps its provider keys and is **provider-keyed table data that stays** |

`tests/system/conftest.py`: 129 lines match `droplet` (case-insensitive), 10 match `droplet_ids`.

- **In scope: 1 symbol.** `wait_until_firewall_droplet_ids` (def `:812-819`, parameters `token, firewall_id, expected`; docstring `:820`; body `:844`). The DigitalOcean field name is in the helper's name and body, not in its parameters. Callers: `tests/system/test_cli_interrupt.py:55` (import), `:458`, `:474`; `tests/system/test_cli_references.py:67` (import), `:417`. Specs naming it: `specs/system_test_references.md:93`, `specs/system_test_interrupt.md:367`. No offline test references it.
- **Stays (DigitalOcean-specific by design, per the issue):** the other 128 lines, including `wait_until_droplet_gone(token, droplet_id, ...)` (`:431-432`, a helper for DigitalOcean's droplet endpoint, whose parameter is DigitalOcean's own id), and including `write_firewall_aiform_md(droplet_ids=...)`, whose keyword is the DigitalOcean `PARAM_SCHEMA` key it writes (owner question 4), and the callers' own `["droplet_ids"]` reads in DigitalOcean-specific test modules.

## Where the firewall's VM-reference knowledge lives

### What the orchestrator needs to know

For a dependent `D` whose target `T` is being destroyed (`aiform/orchestrator.py:944-971`, `:1611-1670`):

1. Which top-level field of `D` lists `T`'s ids (`"droplet_ids"`), so the repair can strip it.
2. Which other places in `D`'s attributes may also name `T` (`inbound_rules[].sources.droplet_ids`, `outbound_rules[].destinations.droplet_ids`), because stripping only the top-level list would leave a dead id behind, so such a `D` is not repaired.

Both are facts about the firewall's schema. Today the orchestrator holds them.

### Alternatives

| | Alternative | For | Against |
|---|---|---|---|
| **A** | **Declarative `REFERENCE_FIELDS` class attribute on `ResourceDriver`** (recommended) | Same shape `specs/dependency_detection.md` "The declaration contract" already designed for Phase 3, so the repair table and Phase 3 detection read one fact; orchestrator keeps a generic path walker and no provider vocabulary; matches the four existing declarative attributes | Contract addition (flagged below); destroy planning must now read a driver class at plan time (no network, no credentials, `PlannedResource.driver` stays `None`) |
| B | A driver method, e.g. `drop_references(attributes, target_ids) -> params \| None` | All schema logic, including id typing, inside the driver; no walker | Behavioural method needing a live instance; cannot enumerate fields before anything exists, so Phase 3 would add a second declaration of the same fact; harder to test across drivers |
| C | Keep it in the orchestrator behind one DigitalOcean-named accessor (the issue's "one provider-named accessor" read literally) | Smallest diff | Leaves the rule schema in the neutral file, which the issue's third bullet forbids; the second provider repeats the work |
| D | A is read by AST without importing, as `driver_gen.py` does | No plan-time driver exec | A second mechanism for no measured gain: `load_driver()` execs a file and builds an object, nothing else |

**Recommendation: A.** The tradeoff accepted: one additive contract attribute and a plan-time driver load, against zero DigitalOcean schema in neutral code and one declaration for two consumers.

### The declaration (concrete)

In `aiform/driver.py`, beside the four existing declarative attributes (`PARAM_SCHEMA` `:74`, `LIKELY_REPLACE_FIELDS` `:85`, `NON_DIFFABLE_FIELDS` `:106`, `UNORDERED_FIELDS` `:124`):

```python
class ReferenceField(NamedTuple):
    path: str
    target: tuple[str, str]


# on ResourceDriver
REFERENCE_FIELDS: list[ReferenceField] = []
```

- `path` grammar: `.` descends a key, `[]` fans out a list. `"droplet_ids"` is top-level; `"inbound_rules[].sources.droplet_ids"` is nested.
- `target` is `(provider, resource_type)`.
- Default is empty, same reassign-don't-mutate rule as the other four. `compute` and `domain` declare nothing.
- A small `values_at(attributes, path)` walker in `aiform/driver.py` yields the values at a path; it is the one reader of the grammar.

`drivers/digitalocean/firewall.py` declares three entries, all targeting `("digitalocean", "compute")`: `droplet_ids`, `inbound_rules[].sources.droplet_ids`, `outbound_rules[].destinations.droplet_ids`. Those are exactly the paths `PARAM_SCHEMA` allows (`PARAM_SCHEMA` `firewall.py:78-94`; nested target schema `_RULE_TARGET_SCHEMA` `:48-58`; `_TARGET_KEY_FOR` `:46`; `additionalProperties: False` at `:57` and `:93`). `_project_rule` (`:360`) keeps only the side `_TARGET_KEY_FOR` maps, so `inbound_rules[].destinations` and `outbound_rules[].sources`, which the old scan also checked, cannot occur in state or in a live read.

Orchestrator rules, replacing `_REPAIRABLE_EDGES` and `_rule_names_droplet`:

- A dependent is repairable for target `T` when its driver declares at least one top-level path (no `.`/`[]`) for `T`'s `(provider, resource_type)`, `T`'s id is ASCII digits, and no declared nested path for `T` holds that id (compared as strings, as today).
- The repair strips the id from each declared top-level path; the prompt and the refusal message print the path from the declaration.
- A dependent whose driver declares nothing for `T`'s type is not repairable, same verdict as a `(provider, resource_type)` absent from `_REPAIRABLE_EDGES` today.
- A dependent whose driver cannot be loaded at classification time is classified not repairable. This is **not** today's behaviour: `_REPAIRABLE_EDGES` (`orchestrator.py:944-951`) is keyed on `(provider, resource_type)` and never touches the file, so today a DigitalOcean firewall with no driver file is classified repairable and fails later in `_apply_repair` (`:1634`, `load_driver`, `PlanBlockedError` "no driver found for ...") before that repair's own write. Identical behaviour is impossible through a declaration read from the driver: with the file gone the declaration is unreadable, and "repairable as today" would also flip non-firewall dependents, which today are refused at plan time. Stated as a behaviour change under "Behaviour" and open question 6.
- Classification checks the target's type and ASCII-digit id first and loads the dependent's driver last, so `test_a_non_ascii_digit_target_id_is_not_repairable_and_does_not_raise` (`tests/test_orchestrator.py:4735`, calls `_is_repairable` with no `drivers_dir`) passes without a driver on disk.
- `_repair_prompt` (`:1611`, called at `:1328`) and `_apply_repair` (`:1628`) read the field path from the declaration at the point of use. `_apply_repair` already loads the driver (`:1634`); `_repair_prompt` loads it once more. A missing driver there raises the same `PlanBlockedError` "no driver found" as today, but at the prompt instead of after the confirm; reachable only if the file vanishes between classification and the prompt.
- Plan-time loads pass `reserved_tags=deployment_tags(st.deployment)`, the rule `TestReservedTagsReachDrivers` (`tests/test_orchestrator.py`) enforces.
- The integer assumption (`int(target.id)`, `_as_int_id`) stays and its comments say "the declared field holds integer ids". It is the one neutral-layer assumption this plan leaves; owner question 3.

### Phase 3 reads the same declaration (not built here)

`specs/dependency_detection.md` ("The declaration contract") designed `REFERENCE_FIELDS` as path, target, attribute. This plan builds the first two columns because the repair reads exactly those; it does not build `attribute` because nothing would read it. Phase 3 later adds one field to `ReferenceField` and reads the same list:

```python
for field in driver.REFERENCE_FIELDS:                      # repair, built here
    if field.target == target_type:
        ... values_at(live_attributes, field.path) ...

for field in driver.REFERENCE_FIELDS:                      # detection, Phase 3, not built
    for value in values_at(params, field.path):
        if value == tracked[field.target].attributes[field.attribute]:
            edge(dependent, tracked[field.target])
```

Both consumers share the declaration and the walker; neither needs a new schema fact from the orchestrator. Phase 3 stays paused (`plans/pause-phase-3-dependency-detection.md`).

### Contract flag (needs the owner's decision)

- `CLAUDE.md` says to follow `PLAN.md` §4's `ResourceDriver` contract exactly with four declarative class attributes (`:203-206`). `REFERENCE_FIELDS` is a fifth, additive: no existing driver changes and the default is empty.
- Justified under "no abstractions for scenarios that can't happen yet" because it replaces a hard-coded table that exists and runs today (`_REPAIRABLE_EDGES`), rather than anticipating Phase 3.
- Edits that follow, all design changes under `PROCESS.md`: `PLAN.md` §4 (attribute list and code block `:750`), `CLAUDE.md` (the "four declarative class attributes" sentence), `specs/driver.md` addendum. `PLAN.md` §4 and `CLAUDE.md` already omit `UNORDERED_FIELDS` (#133); `specs/dependency_detection.md` says to fix that first or the gap doubles. This plan **adds `REFERENCE_FIELDS` only** and leaves #133 open (owner question 5).

## Changes by file

- `aiform/driver.py`: `ReferenceField`, `REFERENCE_FIELDS`, `values_at`; 3 docstring examples to VM terms.
- `drivers/digitalocean/firewall.py`: the three-entry declaration, with a comment saying what it declares and no history.
- `aiform/orchestrator.py`: delete `_REPAIRABLE_EDGES` and `_rule_names_droplet`; `_is_repairable`, `_repair_prompt`, `_apply_repair` read the driver's declaration; rename `droplet_id` locals to `target_id`; `_as_int_id` comment; comments at `149-150` and `1063-1064`.
- `aiform/config.py` and `aiform/cli.py`: rename `PROVIDER_DROPLET_PROBES` to `PROVIDER_SCOPE_PROBES`, valued `(url, collection_key)` with `("https://api.digitalocean.com/v2/droplets?per_page=1", "droplets")` as the DigitalOcean row (the only place the word stays, as provider-keyed data); `_check_droplet_scope` to `_check_scope`, reading the key from the table instead of `"droplets"`; four user-facing strings to VM wording (owner question 2).
- `aiform/ssh.py`, `observability.py`, `references.py`, `compare.py`: comments and docstrings only.
- `tests/system/conftest.py`: `wait_until_firewall_droplet_ids` to `wait_until_firewall_vm_ids`; the one `firewall["droplet_ids"]` read (`:844`) moves into a private accessor `_digitalocean_firewall_vm_ids(firewall)`, the issue's "one provider-named accessor per read": the neutral helper name calls an accessor that names DigitalOcean, and the field name `droplet_ids` appears only inside it; docstring neutral. Update the 3 call sites in 2 test files and the two specs.
- `tests/test_cli.py:787, 829, 973-974`: follow the table rename.

## Behaviour: identical except one stated change, and the proof

**One owner-visible behaviour change** (open question 6): a dependent whose driver file is missing or does not import.

| | Today | After this plan |
|---|---|---|
| DigitalOcean firewall, driver file missing, its VM destroyed | Classified repairable at plan time; the plan shows a repair entry; apply fails at `_apply_repair` (`:1634`) with `no driver found for (provider='digitalocean', resource_type='firewall') -- expected <path>` before that repair writes | Refused at plan time with the orphaned-dependents message (`_orphaned_dependents_reason`, `:928`; hint `pass --force ...` on the paths route via `_resolve_reverse_dependents` `:1050`, `destroy <file> --force` on the marker route `:1037-1043`); with `--force` on the paths route it becomes a warning that drops the edge, as for any unrepairable dependent |
| Firewall driver file does not import | Same as above, but apply raises the import error | Same refusal as the row above |
| Non-firewall dependent, driver file missing | Refused at plan time with the orphaned-dependents message | Unchanged |

Everything else is unchanged for every state the drivers can produce. One further difference exists only for a hand-edited `state.json`: the old scan also checked `inbound_rules[].destinations` and `outbound_rules[].sources`, which the firewall driver rejects on input (`_validate_rule`, `firewall.py:243-254`) and never returns from `read()` (`_project_rule`, `:360`), so the new declaration omits them.

- Proof is the existing offline suite, unchanged in assertions: `env -u DIGITALOCEAN_TOKEN -u ANTHROPIC_API_KEY python -m pytest` from the worktree (the interpreter is the primary checkout's `.venv/bin/python`; `import aiform` from the worktree resolves to the worktree, verified). Baseline 2457 passed; the count after must be 2457 plus the new tests below, with no existing test deleted or weakened. `ruff check .` and `ruff format --check .` also pass (CI runs both).
- Run under `env -u` always: the direnv token makes credential tests falsely green locally.
- One test edit is forced, not behavioural: the fake firewall driver in `tests/test_orchestrator.py` (`LOGGING_DRIVER_SOURCE`, `:4052`) gains a `REFERENCE_FIELDS` declaring the paths `NESTED_RULE_PLACEMENTS` (`:4126-4130`, 3 entries) exercises. Two of the three, `inbound_rules`/`sources` and `outbound_rules`/`destinations`, are exactly the paths the real firewall driver declares. Only `inbound_rules`/`destinations` is unproducible by the real driver; it stays covered because the orchestrator is generic over whatever a driver declares, and the fake declares all three. Every assertion stays.
- User-visible text changes by exactly: the repair prompt and the live-refusal message print the declared path instead of the literal `droplet_ids` and "sources or destinations"; for the firewall that is `droplet_ids` (prompt) and the nested path (refusal). No test or spec asserts the prompt or refusal wording: `grep -rn -E "leave behind|sources or destinations|before destroying\?" aiform/ specs/ tests/ PLAN.md` returns only `aiform/orchestrator.py:1614, 1645, 1646` (3 lines, all code), and 0 lines under `specs/`, `tests/` and `PLAN.md`.

### New tests (red before green, per `PROCESS.md`)

Each is written first and run against the unchanged code to show it fails for the stated reason.

1. `tests/test_driver.py`: `ResourceDriver.REFERENCE_FIELDS == []`; a subclass that reassigns it does not alter the base (mirrors `TestUnorderedFields`, `:106-121`). Red: attribute missing.
2. `tests/test_driver.py`: `values_at` on top-level, nested-through-list, missing key, `None` and empty-list inputs. Red: function missing.
3. `tests/drivers/test_digitalocean_firewall.py`: the declaration equals the set of `droplet_ids` paths found by walking `PARAM_SCHEMA` (guards drift if a rule side is added), and every declared path resolves in `PARAM_SCHEMA`. Red: attribute missing.
4. `tests/drivers/test_*`: every driver in `drivers/` has every `REFERENCE_FIELDS` path resolving in its own `PARAM_SCHEMA` (compute and domain: vacuous pass). Red: attribute missing.
5. `tests/test_orchestrator.py`: a fake dependent declaring a differently-named top-level field (`vm_ids`) targeting a fake compute type is repaired; a fake dependent whose driver declares nothing is refused as before. Red: the hard-coded table ignores the fake. This is the test that proves the orchestrator holds no firewall schema.
6. `tests/test_orchestrator.py`: pins the stated behaviour change. (a) A firewall dependent whose driver file is missing, and (b) one whose driver file has a syntax error, are each refused at plan time with the orphaned-dependents message, not planned as repairs and not crashed. (c) A non-firewall dependent with a missing driver is refused with the same message as today (green before and after; guards the unchanged row). Red for (a) and (b): the unchanged code plans a repair entry for the firewall.
7. `tests/test_system_conftest.py`: `wait_until_firewall_vm_ids` exists; `wait_until_firewall_droplet_ids` does not; the new helper's name and every one of its parameter names (`inspect.signature`) contain neither `droplet` nor `droplet_ids`. `wait_until_droplet_gone` (`:431`) is not asserted on and stays. Red: old name present, new name absent. (This is the machine check of the issue's second "done when" clause, scoped to the one helper that carries a DigitalOcean field name.)

## Specs, and the greps that find their siblings

Updated in the same PR, each by editing in place (`specs/README.md`, "Lifecycle"):

- `specs/orchestrator.md` (`:1577-1630`): replace the `_REPAIRABLE_EDGES` description and the `sources`/`destinations` `droplet_ids` rule scan with the declaration-driven rule; state that destroy planning reads the dependent's driver class at plan time while `PlannedResource.driver` stays `None`.
- `specs/driver.md`: addendum for `REFERENCE_FIELDS` and `values_at`.
- `specs/digitalocean_firewall.md`: "Resource graph" section lists the three declared paths and the `droplet_ids` edge as the one the repair reads.
- `specs/dependency_detection.md`: build-status rows `REFERENCE_FIELDS` (`:25-26`) change from "not built" to "path and target built, read by the destroy repair; `attribute` not built"; "The declaration contract" notes the two columns now exist.
- `specs/MULTI_RESOURCE_PRD.md` (`:580`): "Designed but not built" becomes "partly built" with the same split.
- `specs/system_test_references.md:93`, `specs/system_test_interrupt.md:367`: helper rename.
- `specs/cli.md:331-359` (table name, `(droplet scope unverified)` at `:339` and `:359`) and `specs/cli.md:368-369` ("droplet probe" at `:368`, "without droplet scopes" at `:369`): probe rename and the four strings; `specs/config.md` if it names the table.
- `specs/system_test_domain.md:258`, and the comment at `tests/system/test_cli_domain.py:212`, both naming `_check_droplet_scope`: renamed with the function. The comment at `tests/test_cli.py:759` ("no droplet scope") is a test-name/comment sibling: reworded to VM terms with the strings (owner question 2).
- `specs/dependency_detection.md:250-251`: the four `driver.py` line citations (`:62/:73/:94/:112`) are corrected to `:74/:85/:106/:124` in the same edit that updates its build-status rows.
- `PLAN.md` §4, `CLAUDE.md`: contract flag above; `PLAN.md:809, 845` wording.

Sibling greps the author runs after each correction and lists, with counts printed first, in the PR (never piped through `head`; `grep` here is ugrep, so no `-qv`, use `awk` for inversion):

- `_REPAIRABLE_EDGES`: today code + `specs/orchestrator.md:1582` only; must reach 0 everywhere.
- `_rule_names_droplet`: today code only; must reach 0.
- Nested-rule wording, `sources./.destinations` and `sources/destinations` across `specs PLAN.md CLAUDE.md README.md`: today `specs/digitalocean_firewall.md:22, 25, 64, 130, 416, 433`, `specs/orchestrator.md:1585, 1616, 1629`, `specs/dependency_detection.md:121, 122, 429`, `specs/resource_dependencies.md:1036`, `specs/MULTI_RESOURCE_PRD.md:438, 535`; each is read and either still true (DigitalOcean-specific spec) or fixed.
- `wait_until_firewall_droplet_ids`: today 2 specs + 3 call sites + def; must reach 0.
- `PROVIDER_DROPLET_PROBES`: today `config.py:27`, `cli.py:600`, `tests/test_cli.py:787, 829, 973, 974`, `specs/cli.md:331` (7 lines); must reach 0.
- `_check_droplet_scope|droplet scope` across `aiform/ specs/ tests/ PLAN.md CLAUDE.md README.md`: today `aiform/cli.py:550, 552, 593, 613, 652`, `specs/cli.md:339, 359, 369`, `specs/system_test_domain.md:258`, `tests/test_cli.py:759`, `tests/system/test_cli_domain.py:212` (11 lines; `cli.py:550` and `:613` read "droplet scopes"/"droplet scope"); must reach 0 for the function name and for user-facing and prose uses of "droplet scope".
- `droplet's base image`, `same droplet would read`, `drifted droplet`: `PLAN.md:809, 845`, `specs/resource_references.md:471` are siblings of renamed comments.
- Final: `grep -rn -i droplet aiform/` must return only the allowed set (below).

**Done means:** `grep -rn -i droplet aiform/` returns exactly `cli.py` line 62 (example scaffold) and the DigitalOcean row of `PROVIDER_SCOPE_PROBES` in `config.py` (the URL and `"droplets"` key), both provider-keyed data; and the renamed helper `wait_until_firewall_vm_ids` has no DigitalOcean field name in its name or parameters (`wait_until_droplet_gone` stays, as a DigitalOcean droplet helper).

## Live system run

Required before push: the PR touches `aiform/orchestrator.py` and `drivers/digitalocean/firewall.py`, so `system-test` applies (`github-commit-process` SKILL, "system-test"). This is also the first live run on post-Phase-4 `main` plus this diff.

- Run by the coordinating session, serialized, one run at a time, from the worktree root (`LOG_DIR` is relative to the working directory):
  `/Users/juan/src/aiform/.venv/bin/python scripts/run_system_tests.py`, as the sole command. It runs `pytest -m system tests/system/ -v` and writes `.aiform/testlog/system-test-<UTC>.log` in the worktree (the directory exists).
- Exit code: read it from the script directly or capture `$?` immediately; a trailing `echo` or `tail` steals it. Then read the log; confirm the passed/failed/xfailed counts, not the shell status.
- Credentials: `DIGITALOCEAN_TOKEN` and `ANTHROPIC_API_KEY` come from the environment (direnv / Keychain `*_AIFORM`; the Anthropic key must be "Not linked"). Never print the environment; use `[ -n "$VAR" ]` checks only.
- Nothing edits `aiform/` or `drivers/` while the suite runs: `load_driver` re-execs the file on every call.
- No other live run (another worktree, another agent) overlaps: the suite shares deployment `default` and tag-keyed sweeps.
- Afterwards, read-only listings on the development account (the token points at team `TellezTecDevelopment`), filtered to the `aiform-system-test` tag: droplets, firewalls, and domains under the test zone. Any survivor is a bug report. Cleanup is scoped to resources carrying that tag or recorded in the run's ledger; the production WordPress droplet (589098829) is never listed for deletion or touched, and nothing without the tag is deleted.
- Record the log filename in the `system-test` status description, per the SKILL.

## Comment rule

- Comments say what, never how we got here: no "previously", "was renamed", "earlier version", and no issue-number provenance (`#268`, `#235`, `#226`) in any line this change touches or adds.
- Neutral layers say "VM"; DigitalOcean's word survives only in `drivers/digitalocean/`, DigitalOcean-specific system tests, and the provider-keyed rows listed above.
- After each wording fix the author greps the corrected phrase for sibling copies (list above) and prints counts.
- Lines this change does not touch keep their comments, including history ones (see non-goals).

## Pairing

**Sonnet 5 authors; Opus 5 reviews** via `/code-review` run on the branch name `270-neutral-vm-terms` (not the PR number), per `github-commit-process` SKILL, "Choosing who authors the diff also chooses who reviews it": the cheap default pairing. The coordinating session authors nothing and reviews nothing. The pairing holds through every fix round. No change to any model-tiering default. The change is a mechanical rename plus one small declarative addition; the contract flag is the part that warrants Opus rather than a lighter reviewer.

## Work order

1. Owner approves this plan; the coordinator records the approval in the PR `## Plan` section.
2. Red commit: new tests 1-7 and the fake-driver `REFERENCE_FIELDS`; run the suite and show each fails for the stated reason.
3. Green commit: `driver.py`, `firewall.py`, `orchestrator.py` repair path.
4. Commit: scope probe table and `cli.py` strings (or its carve-out, per owner question 2).
5. Commit: comment/docstring renames in the remaining `aiform/` files.
6. Commit: `tests/system/conftest.py` rename, 3 call sites.
7. Commit: specs, `PLAN.md`, `CLAUDE.md`.
8. Offline suite under `env -u`, `ruff check .`, `ruff format --check .`, the sibling greps with counts.
9. Live run (coordinator), leak check, then push.
10. PR with `Closes #270` in the body (not only a commit), a `## Plan` section citing this file, and the pairing; Opus review on the branch; merge only on the owner's explicit approval.

## Risks

- **Plan-time driver exec is new for destroy planning.** Mitigated: no network or credentials, reserved tags passed, and tests 5 and 6.
- **`load_driver` (`orchestrator.py:199-214`) raises `PlanBlockedError` only on `FileNotFoundError`.** An unimportable driver (syntax error, failed import) would raise something else, and today's `_REPAIRABLE_EDGES.get()` classification cannot raise. Decision: the classification site catches `Exception` from `load_driver` and returns "not repairable", which falls into the existing refusal path and keeps classification non-raising. The refusal moves a failure that today surfaces at apply (for a missing file) to plan time; that is the behaviour change in the "Behaviour" section. Test 6 pins it. This is the one broad catch the plan adds, and it is scoped to that one call.
- **User-facing wording moves.** Mitigated: no test asserts the changed strings; `specs/cli.md` updated; live suite exercises the repair prompt.
- **`REFERENCE_FIELDS` going stale against `PARAM_SCHEMA`.** Mitigated by tests 3 and 4.
- **Comment-only edits in `.py` files still re-trigger `system-test`.** Accepted; the run is already required.
- **Fake-driver edit loosens a test's meaning.** Mitigated: assertions unchanged and the reviewer is told which hunk is the forced one.

## Non-goals, and what was noticed but excluded

- No repo-wide history-comment sweep. Noticed and left: `aiform/references.py:70` ("An earlier version tested..."), `aiform/compare.py:133` ("(#224)"; the line is rewritten for the word "droplet" and loses its issue number only because it is touched), and issue-number comments throughout `orchestrator.py` and `drivers/`.
- No split of `tests/system/` into a neutral layer plus a provider module (option B).
- No Phase 3, no `attribute` column, no inferred edges, no change to `_order_files`.
- No change to the other 128 droplet mentions in `tests/system/conftest.py`, nor to `write_firewall_aiform_md`'s keyword.
- No change to `drivers/digitalocean/` beyond the declaration, nor to `_PROTECTS` (provider-keyed data).
- No removal of the integer-id assumption in the repair (owner question 3).
- The 31 `"digitalocean"`/`DigitalOcean` mentions in `aiform/` are not touched.
- `driver_gen.py` and its prompts are not changed to mention `REFERENCE_FIELDS`; `driver_gen` has no caller (`CLAUDE.md`).

## Open questions for the owner

1. **Alternative A (declarative `REFERENCE_FIELDS`) or C (accessor kept in the orchestrator)?** A is recommended; C fails the issue's third bullet but touches no contract.
2. **Scope probe in `cli.py`/`config.py`: include (recommended) or carve out?** The issue names three places; its "done when" grep also hits `_check_droplet_scope`, which reads DigitalOcean's `"droplets"` response key in a neutral layer. Including it changes four user-facing strings to VM wording and the table's value shape. Carving it out means the done-when grep must allow that function.
3. **Keep the integer-id assumption in the repair (recommended), or make it schema-driven now?** Schema-driven handles string ids (AWS-shaped) but builds for a provider that does not exist yet.
4. **Is `write_firewall_aiform_md(droplet_ids=...)` DigitalOcean-specific (stays, recommended), or a "neutral helper signature" the done-when clause covers?**
5. **`PLAN.md` §4 and `CLAUDE.md`: add `REFERENCE_FIELDS` only (recommended, scope-tight), or also fix #133's missing `UNORDERED_FIELDS` in the same edit?** Also: approve the two-line wording-only edit to `PLAN.md:809, 845`.
6. **A dependent whose driver file is missing or unimportable: refuse at plan time (recommended) or keep today's apply-time failure?** Today a DigitalOcean firewall with no driver file is planned as a repair and fails at apply (`no driver found ...`); the plan refuses it at plan time with the `--force` message, because a declaration cannot be read from a missing file. Keeping today's timing needs a fallback `(provider, type)` table in the orchestrator, which is alternative C.
