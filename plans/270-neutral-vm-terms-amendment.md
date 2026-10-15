# Plan amendment: #270 — typed reference ids

Amends `plans/270-neutral-vm-terms.md`. Owner decisions of 2026-10-07.

## Approved as recommended

- Decision 1: add `REFERENCE_FIELDS` to the `ResourceDriver` contract (alternative A); edit `PLAN.md` §4 and `CLAUDE.md` to add it only.
- Decision 2: include the `cli.py`/`config.py` scope probe rename.
- Decision 5: do not fix #133; `UNORDERED_FIELDS` is not added to `PLAN.md` or `CLAUDE.md`. The wording-only edit to `PLAN.md` lines 809 and 845 is approved.
- Decision 6: a dependent whose driver is missing or unimportable is refused at plan time.
- Decision 4 stands: `write_firewall_aiform_md` keeps its `droplet_ids` keyword. The owner may revisit it.

## Replaced: the integer-id assumption

The plan's "keep the integer-id assumption" is rejected. The id type is declared by the driver.

- `ReferenceField` is `(path: str, target: tuple[str, str], id_type: str)`.
- `id_type` is a JSON-schema type name: `"integer"` or `"string"`.
- The firewall declares `"integer"` for all three entries.
- Any other `id_type` is rejected with a clear error when the declaration is read.
- For `"integer"` the target id must be ASCII digits to be repairable, is converted with `int()` when written, and is compared to stored values as strings. This is today's behaviour.
- For `"string"` any target id is repairable, is used as-is, and is compared as strings.
- The orchestrator holds no `isdigit`, `int(target.id)` or `_as_int_id` rule of its own.
- A declared `id_type` must equal the `items.type` of that path in the driver's `PARAM_SCHEMA`; tests 3 and 4 check it.
- `specs/driver.md`, `specs/orchestrator.md` and `specs/dependency_detection.md` show the declaration columns as path, target, id_type; `attribute` stays unbuilt.
- Open question 3 of the plan is resolved by this amendment: schema-driven now.
