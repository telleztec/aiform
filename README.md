# aiform

An AI-driven alternative to Terraform.

## The pitch

The origin of this idea comes from the experience working on two different DBaaS
implementations. What came out of that work was the recognition that Terraform code
generation is doable with AI but still more expensive than it should be, and that
the result is a somewhat brittle, mostly-works outcome. Customers still struggle
with timeouts, retries, resource scarcity at the CSP side, and other issues that
often require minor judgments from developers or SREs. Enter AI: the AI running in
the orchestration can make an educated guess that an additional retry is warranted,
or that perhaps we should quit immediately because the error is catastrophic.

Moreover, infrastructure as code (IaC) simply builds the infrastructure up: it does
not run it, alert when it fails, or adjust as the needs require. This project
explores the premise that an LLM will be a better orchestrator than the Terraform
engine, and that once the infrastructure is stood up, a set of skills can maintain the
system — doing software upgrades, rotating certs, performing white-hat security probes,
alerting when something goes wrong, and using AI techniques to resolve incidents and
propose fixes.

Terraform's plan/apply engine is powerful but rigid: every resource attribute is
statically flagged by the provider author as either updatable-in-place or
`ForceNew` (destroy + recreate), with no room for "it depends on the actual
diff." Real-world consequences include unnecessary destroy/recreate cycles
(e.g. AWS security-group description edits, Azure `zone_redundant`), and
`count`/`for_each` index-shift bugs that destroy unrelated resources.

The side-effects of the project are numerous. One of them is that we will provide an
agent to help create the drivers that implement resource deployment, update, deletion,
and query, and anyone will be able to use that agent to generate a new driver. That
matters because it removes the ceiling on how much of a system can be managed as code:
a project no longer has to stop at the 80% its provider happens to cover, and can go to
100%.

aiform replaces Terraform's *planning and diffing* logic with an LLM that
reasons about the actual diff each time — while keeping the mechanical,
repeated part (the CSP API calls that create/read/update/delete a resource)
in plain, deterministic, human-readable Python modules. Those modules are
written once per resource type, reviewed, and then reused forever with
**zero further LLM calls** on repeat applies — so the cost and latency of
"AI-driven" stays bounded to the parts that actually benefit from judgment.

This is a standalone CLI tool, not a Claude-Code-only workflow — it calls the
Anthropic API directly, the same way Terraform is independent of any editor.
That is the starting point rather than the end state: we expect to add LLM
brokers such as Bedrock, and locally hosted open models such as Llama 3,
Gemma 3, or DeepSeek-V3. We also anticipate needing a long-running server to
support ongoing deployment and monitoring, designed so that end users deploy
that monitoring and running infrastructure with the tool themselves, and then
use it to deploy and run their own SaaS application.

## Status

**Implementation in progress.** The full architecture is in
[`PLAN.md`](./PLAN.md): repo layout, the `.aiform.md` file format, the state
file schema, the resource module interface, the plan/apply algorithm, the
CLI surface, credential handling, an MVP walkthrough, and what's not yet
implemented.

All of the core modules are now built and tested against their specs:
`aiform/models.py`, `state.py`, `config.py`, `llm.py`, `driver.py`,
`driver_gen.py`, `parser.py`, `planner.py`, `orchestrator.py`, `cli.py`, the
`python -m aiform` entry point, and the curated
`drivers/digitalocean/compute.py` driver. `python -m aiform` exposes `init`
along with `plan create`, `plan apply`, `plan destroy`, `plan refresh`, and
`plan show` — aiform says "hello world" against DigitalOcean, creating,
refreshing, resizing, and destroying droplets. In-place updates are narrower
than the pitch above suggests: the curated driver resizes a droplet in place,
and any other changed field forces a replace.

MVP scope is intentionally narrow: one cloud provider (DigitalOcean), one
resource type (a droplet). Prove the loop end to end before expanding.

## How it works, in short

1. You describe a resource in an `.aiform.md` file — structured YAML
   frontmatter (type, name, provider, params) plus a free-form prose
   "Intent" section for nuance a rigid schema can't capture.
2. `aiform plan create` parses it, refreshes state against the live cloud
   resource, diffs, and — only when there's something to decide — asks the
   **intent-orchestration-model** (default **Claude Sonnet 5**) to
   categorize the change (create/update/no-op) and explain why. Destroy
   is never one of the values this call can return — deletion is always
   an explicit user instruction, never inferred from a diff (see
   [`PLAN.md`](./PLAN.md)'s "Resource deletion").
3. Resource drivers (the small Python modules implementing
   `create`/`read`/`update`/`delete` against a given CSP's API) are
   **written ahead of time, never generated mid-run**. A missing driver is
   an error, not a trigger to generate one. Today every driver is
   hand-authored by a developer on the aiform core team, through this
   repo's own spec-first/test-first development loop — a development-time
   process, separate from aiform's four runtime model roles. One of those
   roles does run on this path: at `plan` time the **code-review-model**
   reviews any driver whose file doesn't match a hash already recorded in
   state, before that driver is trusted. Putting driver authoring in an
   end user's hands is the part still to build — see "Not yet implemented"
   below.
4. `aiform plan apply` re-plans, has the **review-orchestration-model**
   (default **Claude Opus 5**) review anything destructive as a second
   safety gate, then executes — via the deterministic Python module, not
   another LLM call.

See [`PLAN.md`](./PLAN.md) for the full detail, including the exact schemas
and function signatures, and the mapping from each of aiform's four
configurable model roles to the flow it drives.

## Not yet implemented

Beyond the MVP's narrow scope (one CSP, one resource kind, no dependency
graph — see [`PLAN.md`](./PLAN.md) §10 for the full list), two things worth
calling out explicitly since they change how the project grows over time:

- **Self-service driver creation.** Creating a new `(provider, resource)`
  driver is not yet something you can do for yourself, and it will not stay
  something this repo's maintainers do on your behalf either. The goal is an
  agent that helps you draft, review, and approve a driver as its own
  deliberate step, built once the plan/apply loop against curated drivers is
  stable. Generating a driver on the fly, in the middle of a `plan`, is not
  the direction: authoring one stays a step somebody invokes deliberately.
- **Driver submission and publishing.** A methodology for contributing a
  driver back so other aiform users can install and trust it, so the set
  of usable drivers isn't limited to what this repo's maintainers have
  personally built.

## Development

New to this repo? Start with [`CLAUDE.md`](./CLAUDE.md) for the guidelines
and context a fresh session needs. The git/PR workflow is documented as a
project skill at
[`.claude/skills/github-commit-process/SKILL.md`](./.claude/skills/github-commit-process/SKILL.md).
