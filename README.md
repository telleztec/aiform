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
CLI surface, credential handling, an MVP walkthrough, and known limitations.

MVP scope is intentionally narrow: one cloud provider (DigitalOcean), one
resource type (a droplet). Prove the loop end to end before expanding.

## How it works, in short

1. You describe a resource in an `.aiform.md` file — structured YAML
   frontmatter (type, name, provider, params) plus a free-form prose
   "Intent" section for nuance a rigid schema can't capture.
2. `aiform plan` parses it, refreshes state against the live cloud resource,
   diffs, and — only when there's something to decide — asks **Claude Sonnet
   5** to categorize the change (create/update/destroy) and explain why.
3. The first time a resource type is used, Sonnet drafts a small Python
   module implementing `create`/`read`/`update`/`delete` against that CSP's
   API, and **Claude Opus 5** reviews it before it's trusted for reuse.
4. `aiform apply` re-plans, has Opus review anything destructive as a second
   safety gate, then executes — via the deterministic Python module, not
   another LLM call.

See [`PLAN.md`](./PLAN.md) for the full detail, including the exact schemas
and function signatures.

## Development

New to this repo? Start with [`CLAUDE.md`](./CLAUDE.md) for the guidelines
and context a fresh session needs. The git/PR workflow is documented as a
project skill at
[`.claude/skills/github-commit-process/SKILL.md`](./.claude/skills/github-commit-process/SKILL.md).
