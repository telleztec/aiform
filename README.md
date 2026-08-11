# aiform

An AI-driven alternative to Terraform.

## The pitch

Terraform's plan/apply engine is powerful but rigid: every resource attribute is
statically flagged by the provider author as either updatable-in-place or
`ForceNew` (destroy + recreate), with no room for "it depends on the actual
diff." Real-world consequences include unnecessary destroy/recreate cycles
(e.g. AWS security-group description edits, Azure `zone_redundant`), and
`count`/`for_each` index-shift bugs that destroy unrelated resources.

aiform replaces Terraform's *planning and diffing* logic with an LLM that
reasons about the actual diff each time — while keeping the mechanical,
repeated part (the CSP API calls that create/read/update/delete a resource)
in plain, deterministic, human-readable Python modules. Those modules are
generated once per resource type, reviewed, and then reused forever with
**zero further LLM calls** on repeat applies — so the cost and latency of
"AI-driven" stays bounded to the parts that actually benefit from judgment.

This is a standalone CLI tool, not a Claude-Code-only workflow — it calls the
Anthropic API directly, the same way Terraform is independent of any editor.

## Status

**Design phase — no implementation yet.** The full architecture is in
[`PLAN.md`](./PLAN.md): repo layout, the `aiform.md` file format, the state
file schema, the resource module interface, the plan/apply algorithm, the
CLI surface, credential handling, an MVP walkthrough, and what's not yet
implemented.

MVP scope is intentionally narrow: one cloud provider (DigitalOcean), one
resource type (a droplet). Prove the loop end to end before expanding.

## How it works, in short

1. You describe a resource in an `.aiform.md` file — structured YAML
   frontmatter (type, name, provider, params) plus a free-form prose
   "Intent" section for nuance a rigid schema can't capture.
2. `aiform plan` parses it, refreshes state against the live cloud resource,
   diffs, and — only when there's something to decide — asks the
   **intent-orchestration-model** (default **Claude Sonnet 5**) to
   categorize the change (create/update/no-op) and explain why. Destroy
   is never one of the values this call can return — deletion is always
   an explicit user instruction, never inferred from a diff (see
   [`PLAN.md`](./PLAN.md)'s "Resource deletion").
3. Resource drivers (the small Python modules implementing
   `create`/`read`/`update`/`delete` against a given CSP's API) are
   **curated, not generated on the fly** in the current MVP — they're
   built ahead of time via this repo's own spec-first/test-first dev loop
   and reviewed before they ship. Self-service driver creation, where
   `aiform` itself walks a user through generating and approving a new
   driver at `plan` time (drafted by the **code-generator-model**,
   reviewed by the **code-review-model**), is designed but not yet built
   — see "Not yet implemented" below.
4. `aiform apply` re-plans, has the **review-orchestration-model** (default
   **Claude Opus 5**) review anything destructive as a second safety gate,
   then executes — via the deterministic Python module, not another LLM
   call.

See [`PLAN.md`](./PLAN.md) for the full detail, including the exact schemas
and function signatures, and the mapping from each of aiform's four
configurable model roles to the flow it drives.

## Not yet implemented

Beyond the MVP's narrow scope (one CSP, one resource kind, no dependency
graph — see [`PLAN.md`](./PLAN.md) §9 for the full list), two things worth
calling out explicitly since they change how the project grows over time:

- **Self-service driver creation.** Creating a new `(provider, resource)`
  driver is never automatic today, and it will never be something aiform's
  own maintainers do on your behalf going forward either — the goal is an
  interactive flow where `aiform` itself helps you generate and approve a
  driver, built once the primary plan/apply loop against curated drivers
  is stable.
- **Driver submission and publishing.** A methodology for contributing a
  driver back so other aiform users can install and trust it, so the set
  of usable drivers isn't limited to what this repo's maintainers have
  personally built.

## Development

New to this repo? Start with [`CLAUDE.md`](./CLAUDE.md) for the guidelines
and context a fresh session needs. The git/PR workflow is documented as a
project skill at
[`.claude/skills/github-commit-process/SKILL.md`](./.claude/skills/github-commit-process/SKILL.md).
