# aiform — Implementation Architecture

## Context

Terraform's plan/apply engine uses rigid, hardcoded heuristics — most notably its provider schema system, where every resource attribute is flagged by the provider author as either updatable-in-place or `ForceNew` (destroy+recreate), with no nuance for "sometimes updatable" cases (e.g. AWS security-group descriptions, Azure `zone_redundant`). Other real pain points identified in research: `count`/`for_each` index-shift bugs that cause unrelated resources to be destroyed, and state drift/corruption with no built-in versioning unless the backend provides it.

aiform's idea: replace the *planning/diffing* logic with an LLM making those decisions per-actual-diff (not a static per-attribute flag), while the mechanical CSP API calls (create/read/update/delete a resource) are delegated to plain, deterministic Python drivers, reused/executed directly with **zero further LLM calls** on repeat applies. **Revised from the original design**: drivers were originally meant to be generated once per (provider, resource) pair by the LLM at `aiform plan` runtime, unattended. Empirically (see "Driver curation" below) that didn't hold up — the MVP curates drivers instead, built ahead of time by aiform's own maintainers, with unattended runtime generation deferred to a future version. This is deliberately scoped as a real standalone CLI tool (not a Claude-Code-only workflow) to genuinely test how much of this an AI can drive outside an interactive coding session — that test is about the `plan`/`apply`/diff loop itself, which still runs entirely inside the standalone `aiform` binary against the Anthropic API directly, independent of driver curation happening via Claude Code at development time.

**Prior art**: nothing found puts an LLM directly in the runtime plan/diff/sequencing loop against live provider APIs in production today. Closest is an arXiv paper on AI-driven IaC *drift reconciliation* — reconciliation-focused, not full lifecycle CRUD. Worth holding in tension: Martin Fowler has argued DSLs are valuable specifically because they shrink the LLM's solution space and enable deterministic validation. aiform's generated-driver interface (§4) partially serves that same "shrink the solution space" role Terraform's schema does, while the AI supplies the plan/diff/sequencing nuance Terraform's schema has none of.

**Model tiering** (cost-conscious, deliberate): one *role* handles all routine, repeated work — parsing prose intent, diffing, categorizing plan actions — and a second, stronger *role* is used as a review/approval gate. **In the MVP this gate fires in three different places**: (1) at *development* time, reviewing a new curated driver via `/code-review` before it ships as part of the aiform package (see "Driver curation" below) — not a `plan`-time runtime call; (2) at `plan` *runtime*, but only for the hash-mismatch re-review case (§5 step 3) — a driver already on disk whose sha256 no longer matches its trusted record (a hand-edit, or an untrusted file) gets re-reviewed before being trusted again, the one driver-trust check that *does* run live in the MVP; and (3) at `apply` *runtime*, reviewing the final plan before it executes anything destructive. A future version restores driver *generation* review as a runtime checkpoint too (distinct from (2)'s re-review of existing files), for on-the-fly driver generation gated on explicit user approval. This bounds ongoing cost while still getting a stronger model's judgment where mistakes are expensive. Which model (and which model source/vendor) fills each role is **configuration, not a hardcoded constant** — see `specs/llm.md` and `specs/config.md` for the `LLMConfig`/`resolve_llm_config()` design. The MVP default — and the only model source implemented at all right now — is Claude **Sonnet 5** (`claude-sonnet-5`) for the implementation role and Claude **Opus 5** (`claude-opus-5`) for the review role, both via the Anthropic API. Do not change either *default* for cost reasons without asking — this split was chosen deliberately, not by default. Users may override the configured model/source per role; that's an intentional escape hatch, not a violation of this rule.

## MVP scope (locked)

Single CSP (DigitalOcean), single resource kind (`compute`, realized against DO's droplet API). No cross-resource dependency graph yet — deferred explicitly (see Known Limitations).

## Driver curation (MVP) vs. future on-the-fly generation

**Revised from the original design** after the first real driver-generation
attempts. The original §5 described drivers as generated at `aiform plan`
runtime by an LLM (Sonnet drafts, Opus reviews), unattended, the first
time a `(provider, resource)` pair was needed. In practice, across three
consecutive generation attempts against the DigitalOcean compute driver —
Sonnet, Sonnet again with the prompt fixed to include the correct
credentials key and the full acceptance-criteria spec verbatim, then Opus
for *both* drafting and review — every attempt got the exact,
explicitly-stated `credentials` dict key wrong (`credentials["api_token"]`,
then `credentials["token"]`, then a five-candidate guess list that still
didn't include the real key), and two of the three also silently dropped
the entire resize power-cycle sequence the spec details at length. This
wasn't a context-starvation problem — the correct answer was verified to
be in the prompt, twice — it's a real reliability ceiling on one-shot
generation against a spec this detailed. That matters specifically because
the whole point of *runtime* generation is that no human is present in a
real user's session to catch and fix a mistake like this.

**MVP: drivers are curated, not generated at `plan` time.** The set of
usable `(provider, resource)` drivers is fixed by aiform's own maintainers
and ships as part of the aiform package itself
(`drivers/<provider>/<resource>.py`) — not generated per end-user-project.
Two ways a driver gets added:

1. **Via Claude Code, now.** The same spec-first/test-first/Opus-reviewed
   development loop (`PROCESS.md`) used for every other module in this
   codebase: `specs/<provider>_<resource>.md` is the acceptance-criteria
   spec, a hand-written test suite
   (`tests/drivers/test_<provider>_<resource>.py`) checks a candidate
   implementation against it, the driver itself is implemented directly
   against both (by a human, or by Claude Code under human supervision —
   not by an unattended `generate_driver()` call), reviewed via
   `/code-review`, and merged through the normal PR process with human
   approval. This is how `drivers/digitalocean/compute.py` gets built.
2. **On-the-fly generation, deferred.** A future version where `aiform`
   itself, at `plan` time, offers to generate a missing driver and prompts
   the *aiform user* for explicit approval before trusting it — analogous
   to how Claude Code prompts for tool-use permission. `aiform/driver_gen.py`
   already implements the underlying draft/validate/review pipeline (built,
   tested, and itself Opus-reviewed via the normal dev loop) — what's
   deferred is wiring it into `plan`/`apply` behind that approval prompt,
   and further work on generation reliability given the finding above
   before it's trusted unattended.

`aiform plan` against a `(provider, resource)` pair with no driver on disk
fails with a clear, actionable error in the MVP — it does not attempt
generation. §5 below reflects this.

## Terminology

Three distinct concepts, previously conflated under the single overloaded
word "module" — worth being precise about since the whole point of this
design is that the orchestrator treats all three combinations uniformly:

- **Provider** — the CSP/platform integration boundary: `digitalocean`,
  `aws`, `vmware`. Owns credential conventions and API access, nothing
  else. Unchanged from earlier drafts of this doc.
- **Resource** — the abstract, provider-agnostic *kind* of infrastructure
  component being managed: `compute` (a VM/processing unit), `network` (a
  VPC/private network), `load_balancer` (distributes traffic across
  compute). MVP implements exactly one: `compute`. This is what makes
  aiform.md vocabulary portable across providers — an AWS EC2 instance and
  a DigitalOcean droplet are both `resource: compute`, just under
  different `provider:` values. Provider-specific product names (like
  "droplet") are never part of this vocabulary; they're an implementation
  detail hidden inside a driver.
- **Driver** — the concrete implementation of one Resource for one
  Provider: a single Python file at `drivers/<provider>/<resource>.py`
  defining a class named `Driver` that subclasses `ResourceDriver` (§4).
  This is the *only* per-(provider, resource) artifact the system curates
  and reuses — built ahead of time by aiform's maintainers (see "Driver
  curation" above), not generated at runtime in the MVP. DigitalOcean's
  compute driver (`drivers/digitalocean/compute.py`) is the one built in
  the MVP; it happens to call DO's droplet API internally, but nothing
  above the driver ever needs to know that.

The orchestrator is written entirely against the `ResourceDriver`
contract — it dynamically imports `drivers/<provider>/<resource>.py`,
instantiates its `Driver` class, and calls `create`/`read`/`update`/
`delete` on it exactly the same way regardless of which provider or
resource is involved. Adding `aws` or a second resource kind later means
writing a new driver file, not touching the orchestrator.

## 1. Repo layout

```
aiform/
├── pyproject.toml
├── README.md
├── .gitignore                      # .aiform/credentials.env, .aiform/state.json, __pycache__/, *.pyc
├── aiform/
│   ├── __init__.py
│   ├── __main__.py                 # `python -m aiform` entry point
│   ├── cli.py                      # plan / apply / destroy / init / refresh / show
│   ├── config.py                   # env var + credentials-file resolution (§7)
│   ├── parser.py                   # aiform.md -> ResourceSpec
│   ├── state.py                    # state.json load/save, Pydantic models, backup-on-write
│   ├── planner.py                  # diff desired vs actual -> Plan
│   ├── orchestrator.py             # drives plan/apply, dynamic driver import, credential wiring
│   ├── llm.py                      # model-source dispatch: implementation_call(), review_driver(), review_plan()
│   ├── driver.py                   # ResourceDriver ABC + DriverUpdateNotSupported
│   ├── driver_gen.py                # draft/validate/review pipeline; built, not yet wired into plan/apply (deferred on-the-fly generation, see "Driver curation")
│   ├── models.py                   # Pydantic: ResourceSpec, PlanAction, PlanEntry, StateEntry, DriverReview
│   └── exceptions.py               # DriverUpdateNotSupported, ResourceNotFoundError, DriverExecutionError, PlanBlockedError
├── drivers/
│   ├── __init__.py
│   └── digitalocean/
│       ├── __init__.py
│       └── compute.py              # hand-authored via PROCESS.md's dev loop, Opus-reviewed via /code-review, then reused deterministically forever
├── prompts/
│   ├── parse_intent.md             # Sonnet system prompt: prose Intent -> intent_notes[]
│   ├── diff_plan.md                # Sonnet system prompt: raw diff + intent_notes -> PlanAction + rationale
│   ├── generate_driver.md          # Sonnet system prompt: interface spec + CSP context -> driver source
│   ├── review_driver.md            # Opus system prompt: gate #1 checklist
│   └── review_plan.md              # Opus system prompt: gate #2 checklist
├── .aiform/                        # gitignored; created by `aiform init`
│   ├── credentials.env             # DIGITALOCEAN_TOKEN=... (hand-edited, never scaffolded with a value)
│   ├── state.json                  # default state file location
│   └── state.json.backup           # written before every overwrite
├── examples/
│   └── compute.aiform.md           # MVP example
└── tests/
    ├── test_state.py
    ├── test_planner.py
    └── drivers/test_digitalocean_compute.py
```

**Driver convention**: `drivers/<provider>/<resource>.py`, where `<provider>` and `<resource>` are exactly the lowercase `provider:` and `resource:` frontmatter values from an `.aiform.md` file. MVP: `drivers/digitalocean/compute.py`. This is the *only* per-(provider, resource) file the system curates and reuses — everything else in `aiform/` is hand-written, static orchestration code, and in the MVP the driver itself is hand-authored too (see "Driver curation" above), not generated.

**State file location**: `.aiform/state.json`, overridable with `--state-file`.

## 2. `aiform.md` format spec

### Frontmatter schema (exact fields, MVP compute case)

```yaml
---
resource: compute          # required — abstract resource kind; maps to drivers/<provider>/<resource>.py
name: telleztec-app-01     # required — primary key in state: "<provider>.<resource>.<name>"
provider: digitalocean     # required — MVP: only "digitalocean" is supported
params:                    # required — structured, resource-specific
  region: sfo3
  size: s-1vcpu-2gb
  image: ubuntu-24-04-x64
  ssh_keys:
    - "juan-macbook-ed25519"   # DO SSH key name or fingerprint
  backups: false
  monitoring: true
  tags:
    - aiform
    - production
---
```

`resource: compute` is the abstract, provider-agnostic kind (§ Terminology) — never a provider-specific product name like "droplet". `params` is intentionally an open, resource-specific object — its expected shape is not fixed at the aiform.md-format level. It is validated against the target driver's `PARAM_SCHEMA` (§4). In the MVP a driver must already exist (curated — see "Driver curation" above) for `(provider, resource)`; if none does, `aiform plan` fails with a clear error rather than accepting `params` as-is (that fallback — `params` handed to a generation prompt as ground truth for what a new driver needs to accept — is part of the deferred on-the-fly generation flow, not live today).

### Prose "Intent" section

```markdown
## Intent

This droplet runs the primary application server. It should always have
monitoring enabled. If I change the `size` to something bigger, prefer an
in-place resize over destroying and recreating — this droplet holds local
state in /var/lib/app that I don't want to lose to a fresh boot disk.
However, if I ever change the `image`, that always requires a full recreate
since you can't swap a running droplet's base OS image in place.

If backups get turned on, that's a mutable account-level toggle — apply it
without hesitation, no need to flag it as risky.
```

**How it's used**: `aiform/parser.py` sends only this prose block (not the frontmatter) to Sonnet with `output_config.format` constrained to:

```python
INTENT_NOTES_SCHEMA = {
    "type": "object",
    "properties": {
        "intent_notes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "concerns_field": {
                        "type": "string",
                        "description": "params.* key this note applies to, or 'general'",
                    },
                    "guidance": {
                        "type": "string",
                        "description": "One atomic, diff/plan-relevant instruction extracted from the prose.",
                    },
                },
                "required": ["concerns_field", "guidance"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["intent_notes"],
    "additionalProperties": False,
}
```

```python
response = client.messages.create(
    model="claude-sonnet-5",
    max_tokens=2048,
    output_config={"format": {"type": "json_schema", "schema": INTENT_NOTES_SCHEMA}},
    system=open("prompts/parse_intent.md").read(),
    messages=[{"role": "user", "content": prose_intent_text}],
)
intent_notes = json.loads(response.content[0].text)["intent_notes"]
```

`intent_notes` is passed into the **diff/plan step** (§5) as context for Sonnet's create/update/destroy/no-op categorization and rationale — it is *not* passed to the generated Python driver. Drivers stay dumb and deterministic; only the plan step interprets nuance.

One `.aiform.md` file describes exactly one resource in the MVP (no dependency graph). Multiple resources = multiple files, planned/applied independently in sequence. This is the natural extension point for a future graph, deliberately not built now.

## 3. State file schema

`.aiform/state.json`:

```json
{
  "aiform_state_version": 1,
  "resources": {
    "digitalocean.compute.telleztec-app-01": {
      "provider": "digitalocean",
      "resource_type": "compute",
      "name": "telleztec-app-01",
      "id": "123456789",
      "attributes": {
        "region": "sfo3",
        "size": "s-1vcpu-2gb",
        "image": "ubuntu-24-04-x64",
        "ssh_keys": ["juan-macbook-ed25519"],
        "backups": false,
        "monitoring": true,
        "tags": ["aiform", "production"],
        "ipv4_address": "203.0.113.10",
        "status": "active"
      },
      "driver": {
        "path": "drivers/digitalocean/compute.py",
        "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b8",
        "generated_at": "2026-07-30T18:22:11Z",
        "opus_review": {
          "approved": true,
          "blocking_issues": [],
          "concerns": ["update() resizes on any diff, not just size/region — should scope the resize action"],
          "reviewed_at": "2026-07-30T18:22:40Z",
          "model": "claude-opus-5"
        }
      },
      "last_applied_at": "2026-07-30T18:23:05Z",
      "last_refreshed_at": "2026-07-31T09:10:00Z",
      "aiform_md_path": "examples/compute.aiform.md",
      "aiform_md_sha256": "5f4dcc3b5aa765d61d8327deb882cf99..."
    }
  }
}
```

**Key**: `"<provider>.<resource_type>.<name>"` — mirrors Terraform's `<type>.<name>` addressing.

**Fields**:
- `resource_type` — the abstract resource kind (e.g. `compute`), never a provider-specific product name. Deliberately named `resource_type` here, not `resource`, to stay unambiguous next to `name` (the specific instance) — this is the same value as the aiform.md frontmatter's `resource:` key (§2), just named more precisely once it's sitting next to other fields in state. Also what fills `<resource>` in the driver path convention (§1).
- `id` — the CSP's resource identifier, opaque string (DO droplet IDs are numeric-as-string).
- `attributes` — the last-known **actual** attributes as returned by `driver.read()` or `driver.create()`. This is the cache Terraform-style state provides.
- `driver.sha256` — hash of the driver source on disk at the moment its Opus review was recorded. On every `plan`, the orchestrator recomputes the on-disk hash and compares; a mismatch (hand-edit, or a newer generation) invalidates the "trusted, reviewed" status and forces re-review before the driver is used again. This is the drift-detection mechanism for the driver itself, not just the resource.
- `driver.opus_review` — audit trail of gate #1.
- `aiform_md_sha256` — hash of the source file at last successful apply. Used by the planner as a cheap short-circuit: if this matches the current file's hash *and* a refresh shows no live drift, the diff step can skip its Sonnet call entirely and report `no-op` deterministically.

**Refresh mechanism** (`driver.read()` before diffing):

1. For every resource already present in state matching a requested aiform.md file (or all tracked resources, for `aiform refresh`), dynamically import `drivers/<provider>/<resource>.py` and instantiate its `Driver` class.
2. Call `driver.read(id=state_entry.id, credentials=...)`.
   - On success: overwrite `state_entry.attributes` with the fresh values.
   - On `ResourceNotFoundError`: the resource was deleted out-of-band — leave `attributes` from the last known state but mark the entry `drifted_missing: true` in the in-memory plan context, so the diff step proposes a `create` (recreate) rather than treating it as unchanged.
3. The refreshed attributes are **written back to `.aiform/state.json` immediately**, even during a bare `plan` with no changes — matching `terraform plan`'s default `-refresh=true` behavior.
4. A `.aiform/state.json.backup` copy of the previous file is written before every overwrite.

## 4. Resource driver interface

Every driver is a single Python file defining one class, `Driver`, subclassing the hand-written `ResourceDriver` ABC (`aiform/driver.py`). The ABC is what lets the orchestrator call any `(provider, resource)` combination identically — it never inspects a driver's internals, only calls the four contract methods below. Exact contract:

```python
# aiform/driver.py — hand-written, not generated

from abc import ABC, abstractmethod
from typing import Any


class DriverUpdateNotSupported(Exception):
    """Raised by update() when this SPECIFIC diff cannot be applied
    in-place against the live API. The orchestrator catches this and
    falls back to delete() + create() — this is the deliberate
    replacement for Terraform's static, per-attribute ForceNew flag:
    the decision is made per-call against the real diff, not declared
    once and for all up front."""

    def __init__(self, reason: str, unsupported_fields: list[str] | None = None):
        self.reason = reason
        self.unsupported_fields = unsupported_fields or []
        super().__init__(reason)


class ResourceDriver(ABC):
    """
    The contract every (provider, resource) driver implements. The
    orchestrator dynamically imports a driver module, instantiates its
    `Driver` class, and calls these four methods identically regardless
    of provider or resource kind — this class is what makes that
    genericity a structural property, not a convention.
    """

    # Declares the `params` shape this driver accepts. Used by the
    # orchestrator to validate a parsed aiform.md spec before ever
    # calling create()/update(), and shown to Opus at review time
    # (dev-time /code-review for curated drivers in the MVP; generation
    # review once on-the-fly generation is wired up) as ground truth for
    # what the driver claims to handle.
    PARAM_SCHEMA: dict[str, Any]

    # Optional, advisory only (never authoritative — update() is the
    # real arbiter). Populated when the driver is authored so `aiform plan`
    # can WARN that a change is likely to force a replace, purely for UX,
    # without pretending to know for certain the way Terraform's
    # ForceNew does. Subclasses that don't override it get an empty list.
    LIKELY_REPLACE_FIELDS: list[str] = []

    @abstractmethod
    def create(self, params: dict[str, Any], credentials: dict[str, str]) -> dict[str, Any]:
        """
        Create the resource via the CSP API.

        params: the resource's `params` block from aiform.md, already
            validated by the orchestrator against PARAM_SCHEMA before
            this is ever called.
        credentials: e.g. {"DIGITALOCEAN_TOKEN": "..."}. Resolved by
            aiform/config.py. Never logged, never passed through any
            Anthropic API call.

        Returns: dict with at least {"id": str, **attributes} reflecting
            the resource as actually created. Becomes the state entry's
            initial `attributes`.
        """

    @abstractmethod
    def read(self, id: str, credentials: dict[str, str]) -> dict[str, Any]:
        """
        Fetch current live attributes for `id`.

        Returns: dict, same attribute shape as create()'s return.
        Raises: aiform.exceptions.ResourceNotFoundError if the resource
            no longer exists on the CSP side (signals drift).
        """

    @abstractmethod
    def update(
        self, id: str, current: dict[str, Any], desired: dict[str, Any], credentials: dict[str, str]
    ) -> dict[str, Any]:
        """
        Attempt to reconcile `current` -> `desired` in place.

        Inspects the ACTUAL diff (not a static per-field flag) and
        decides per-call whether an in-place update is possible. E.g.
        for a compute resource: resizing UP may be a live resize
        action; resizing DOWN may require powering off first (the
        driver may do this automatically within the call); some fields
        (e.g. a droplet's base image) are never in-place-updatable.

        Returns: dict of attributes after the update (same shape as
            create()).
        Raises: DriverUpdateNotSupported when THIS diff can't be
            applied in place. The orchestrator catches this, treats
            the operation as a "replace" (delete() then create()), and
            — if this wasn't already flagged as a likely replace
            during planning — pauses for the single-resource Opus
            safety gate before proceeding.
        """

    @abstractmethod
    def delete(self, id: str, credentials: dict[str, str]) -> None:
        """
        Destroy the resource. MUST be idempotent: a 404 from the CSP
        (resource already gone) is treated as success, not an error.
        """
```

A driver subclasses this and does nothing more — no shared base-class logic beyond the contract itself:

```python
# drivers/digitalocean/compute.py — hand-authored via PROCESS.md, Opus-reviewed via /code-review

from aiform.driver import ResourceDriver, DriverUpdateNotSupported


class Driver(ResourceDriver):
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

    def create(self, params, credentials): ...

    def read(self, id, credentials): ...

    def update(self, id, current, desired, credentials): ...

    def delete(self, id, credentials): ...
```

**Orchestrator invocation contract** (`aiform/orchestrator.py`):
- Drivers are imported dynamically via `importlib.util.spec_from_file_location`, resolved from `(provider, resource)` in the parsed spec, then instantiated: `driver = module.Driver()`. The class name is always exactly `Driver` — the orchestrator never searches a module's namespace for it.
- `credentials` is assembled once by `aiform/config.py` and passed as a plain dict into `orchestrator.py`'s execution path only — `aiform/llm.py` (every model call, regardless of configured source) never has a `credentials` parameter anywhere in its call signatures. This is a structural property of the codebase, not just a convention: there is no function that has both an LLM client and a credentials dict in scope.
- All four methods are required to be synchronous and side-effect-free of any LLM calls. Gate #1's review checklist explicitly checks for "does this driver import `anthropic`, call any Anthropic endpoint, or read `ANTHROPIC_API_KEY`?" and blocks approval if so.
- Raw CSP API errors raised inside driver methods are caught by the orchestrator, logged with full detail, and re-raised as `aiform.exceptions.DriverExecutionError` for uniform CLI error formatting. The orchestrator does **not** attempt LLM-driven error recovery on this path — a failed mechanical call fails the apply and stops, matching the design goal of the execution being the boring, deterministic part.

## 5. Plan / apply algorithm

### `aiform plan`

1. **Locate `.aiform.md` files** — default: all `*.aiform.md` in cwd, or explicit paths from argv.
2. **Parse each file**:
   - Frontmatter: `yaml.safe_load()`, validated against a Pydantic `ResourceSpec` model (`resource`, `name`, `provider`, `params: dict`). Zero LLM calls.
   - Prose Intent section → one Sonnet call → `intent_notes[]` (§2). This call is skipped entirely if `aiform_md_sha256` in state matches the file's current hash — no reason to re-extract intent from unchanged prose.
3. **Ensure a driver is usable** for `(provider, resource)`:
   - **Driver file missing** → `aiform plan` fails immediately with a clear, actionable error (raises `aiform.exceptions.PlanBlockedError`) naming the unsupported `(provider, resource)` pair — drivers are curated in the MVP (see "Driver curation" above), not generated at `plan` time. A future version replaces this branch with an interactive prompt offering to generate one, subject to explicit user approval before it's trusted; the **Generation** and **Opus gate #1** steps below describe the pipeline that prompt would drive (`aiform/driver_gen.py` already implements it) — not invoked by `plan` today.
   - **Driver file present, but its on-disk sha256 doesn't match the sha256 recorded against any state entry that trusts it** (hand-edit, or an untrusted file dropped in from elsewhere) → **re-review the existing content as-is** — send it straight to Opus gate #1c. This is what makes hand-editing a driver ("you can read/edit/vendor the exact code that runs") an actual supported workflow rather than something the next `plan` quietly discards, and it's the one driver-trust check that *does* run at `plan` time in the MVP. If approved, the new hash is recorded as trusted. If `blocking_issues` comes back non-empty, **do not overwrite** — fail `aiform plan` with an explicit error naming the concerns (raises `aiform.exceptions.PlanBlockedError`); a hand-edit failing review means the human's edit needs fixing, not the AI's.

   **Generation** (deferred — not invoked by `plan` in the MVP; described here for when the future on-the-fly-generation prompt is wired up):
     a. Sonnet (`claude-sonnet-5`, plain-text output — Python source isn't a good fit for `output_config.format`) drafts the driver against `prompts/generate_driver.md`, which embeds the exact interface contract from §4 plus the desired `params` shape as a hint for `PARAM_SCHEMA`.
     b. Static validation: `ast.parse()` for syntax, then AST inspection (not import — untrusted code isn't executed pre-review) to confirm a class named `Driver` exists, subclasses `ResourceDriver`, and implements `create`, `read`, `update`, `delete` with the right argument names, and no `import anthropic` / `os.environ.get("ANTHROPIC` pattern is present.

   **Opus gate #1** (`llm.review_driver()` — used live today only by the re-review branch above; also the gate for the deferred generation path once wired up):
     c. Opus (`claude-opus-5`) reviews the full source — the existing on-disk file in the live re-review case, or a freshly generated draft once generation is wired up — against `prompts/review_driver.md`'s checklist (idempotent `delete`, correct credential sourcing, no LLM calls, sane in-place-vs-replace logic in `update`, error handling that raises rather than swallows). Structured verdict:
        ```python
        DRIVER_REVIEW_SCHEMA = {
            "type": "object",
            "properties": {
                "approved": {"type": "boolean"},
                "concerns": {"type": "array", "items": {"type": "string"}},
                "blocking_issues": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["approved", "concerns", "blocking_issues"],
            "additionalProperties": False,
        }
        response = client.messages.create(
            model="claude-opus-5",
            max_tokens=4096,
            output_config={"format": {"type": "json_schema", "schema": DRIVER_REVIEW_SCHEMA}},
            system=open("prompts/review_driver.md").read(),
            messages=[{"role": "user", "content": driver_source_text}],
        )
        ```
     d. **Approval rule**: a `blocking_issues`-free result is trusted in place (live re-review case) or, once generation is wired up, written to disk; non-empty `concerns` are printed as advisory warnings but do not block. The re-review path never retries automatically. (For the deferred generation path: `blocking_issues` non-empty → retry generation once with the concerns fed back to Sonnet (max 2 attempts total), then fail with an explicit error if still blocked, asking the user to hand-fix or hand-author the driver — this is `driver_gen.py`'s existing, tested behavior, just not reachable from `plan` yet.)
4. **Refresh state** for any resource in the plan that already exists in state.
5. **Diff, deterministically first**: compute a plain dict-diff between refreshed `attributes` and the desired `params`. If the diff is empty **and** `aiform_md_sha256` matches the current file **and** no `drifted_missing` flag is set → the action is `no-op`, decided with **zero LLM calls**. This is what makes the second-and-later `plan` runs cheap.
6. **Categorize with Sonnet, only when there's something to interpret** (a real diff, a missing/drifted resource, or a changed aiform.md file): one call passing the raw diff, `intent_notes`, and the driver's `PARAM_SCHEMA` / `LIKELY_REPLACE_FIELDS`. Sonnet returns a `PlanAction` (`create` / `update` / `destroy` / `no-op`), a natural-language rationale, and a `likely_replace: bool` hint.
7. **Print the plan** and persist refreshed state.

### `aiform apply`

1. Re-run `plan` in full immediately before executing (no separate saved-plan-file flow in the MVP).
2. **Gate #2, batch pass**: if the plan contains any `destroy` actions or any `update` actions flagged `likely_replace: true`, call Opus once with the *entire* plan (all actions, for context) against `prompts/review_plan.md`:
   ```python
   PLAN_REVIEW_SCHEMA = {
       "type": "object",
       "properties": {
           "safe_to_proceed": {"type": "boolean"},
           "flags": {
               "type": "array",
               "items": {
                   "type": "object",
                   "properties": {
                       "resource_key": {"type": "string"},
                       "concern": {"type": "string"},
                       "severity": {"type": "string", "enum": ["info", "warning", "block"]},
                   },
                   "required": ["resource_key", "concern", "severity"],
                   "additionalProperties": False,
               },
           },
       },
       "required": ["safe_to_proceed", "flags"],
       "additionalProperties": False,
   }
   ```
   Any `severity: "block"` flag halts `apply` unconditionally — this cannot be bypassed by `--yes`. Non-blocking flags are printed, then the user is asked for final `y/N` confirmation (`--yes` skips only this prompt, never a `block`).
3. **Execute**, in file order (trivial for MVP's single-resource-per-file model; multi-resource sequencing is explicitly deferred):
   - `create` → `driver.create(params, credentials)`, write state.
   - `update` → `driver.update(id, current, desired, credentials)`.
     - If it raises `DriverUpdateNotSupported` **and this resource was not already covered by the batch review in step 2**: pause, run a single-resource Opus review with the same schema, require fresh confirmation, then `driver.delete()` + `driver.create()`.
     - If it was already covered in step 2 as `likely_replace: true`, proceed directly to `delete()` + `create()` — it already passed the gate.
   - `destroy` → `driver.delete(id, credentials)`, remove from state.
   - `no-op` → skip.
4. **State is written after each resource completes**, not batched at the end — a crash mid-apply doesn't lose successfully-applied resources' state.
5. Step 3 makes **zero Anthropic API calls** per resource beyond what steps 1–2 already spent.

## 6. CLI command surface

```
aiform init [--provider digitalocean]
    Scaffolds .aiform/, .gitignore entries, an examples/*.aiform.md
    starter file. Never creates or prompts for credential VALUES —
    prints instructions for ANTHROPIC_API_KEY / DIGITALOCEAN_TOKEN.

aiform plan [FILE.aiform.md ...] [--state-file PATH] [--json]
    Parse, refresh, verify the curated driver is present (re-review on a
    hash mismatch, else fail with a clear error), diff, print plan.
    Persists refreshed state even with no changes. --json emits the
    Plan as machine-readable output for scripting.

aiform apply [FILE.aiform.md ...] [--yes] [--state-file PATH]
    Re-plans, runs Opus gate #2 for any destructive step, executes.
    --yes skips the interactive confirmation only — never a `block` flag.

aiform destroy [FILE.aiform.md ...] [--yes] [--state-file PATH]
    Plans a destroy of every resource matching the given file(s) (or
    all tracked resources if none given), then applies it. 100% subject
    to Opus gate #2 by definition.

aiform refresh [--state-file PATH]
    driver.read() for every tracked resource, updates state to match
    live reality. No aiform.md parsing, no plan, no LLM calls at all —
    purely mechanical drift detection.

aiform show [--state-file PATH]
    Prints current state contents (id, attributes, driver version,
    last-applied) in readable form.
```

Global flags: `--state-file` (default `.aiform/state.json`), `-v`/`--verbose`, `--no-color`.

## 7. Credentials handling

- **`ANTHROPIC_API_KEY`** — environment variable only, resolved automatically by `anthropic.Anthropic()`. Never accepted as a CLI flag (keeps it out of shell history / `ps`).
- **`DIGITALOCEAN_TOKEN`** — resolution order in `aiform/config.py`:
  1. `DIGITALOCEAN_TOKEN` environment variable, checked first.
  2. Fallback: `.aiform/credentials.env` (dotenv-style, `DIGITALOCEAN_TOKEN=dop_v1_...`), a local gitignored file the user creates directly with a text editor. `aiform init` prints the instructions and the expected filename but never scaffolds it with a value or prompts for the token interactively.
  3. Neither present → clear error naming both options.
- **`.gitignore`**: `.aiform/credentials.env`, `.aiform/state.json` (state can carry sensitive-adjacent data like IPs and resource IDs — treated as sensitive by default), `.env`, `__pycache__/`, `*.pyc`.
- **Structural enforcement that credentials never reach an LLM prompt**: `aiform/llm.py` — every function that talks to a model source — has no parameter, local, or import that carries a `credentials` dict. All credential-bearing code lives in `orchestrator.py`'s driver-execution path, which never imports or calls into `llm.py`. This is verifiable by grep (no `credentials` symbol appears in `llm.py`), not just a documented convention.
- **Logging**: `config.py`'s credential resolver never logs the resolved value. Any `--verbose` output that would dump request/response payloads passes through a `_redact(d)` helper that blanks known-sensitive keys (`credentials`, `*_TOKEN`, `*_KEY`) before printing.

## 8. MVP walkthrough

1. Author `examples/compute.aiform.md`, set `ANTHROPIC_API_KEY` + `DIGITALOCEAN_TOKEN`. `drivers/digitalocean/compute.py` already exists — curated, built ahead of time (see "Driver curation" above) — so nothing about this walkthrough triggers driver generation.
2. **`aiform plan`** — driver file present, on-disk sha256 matches the trusted hash recorded from its last review → no Opus gate #1 call at all. Diff shows `create`.
3. **`aiform apply`** — no destroy/likely-replace actions present → gate #2 is skipped entirely, straight to y/N prompt (or `--yes`). Executes `driver.create(params, credentials)` — one real DO API call. `.aiform/state.json` written with the resource entry, including `driver.sha256` and the (dev-time, pre-recorded) Opus review it shipped with.
4. **Second `aiform plan`** — `driver.read(id, credentials)` refreshes attributes (one DO call), `aiform_md_sha256` matches the unchanged file, dict-diff empty → `no-op` reported with **zero Anthropic API calls**. This is the concrete proof of the "no LLM tokens for the mechanical/repeat path" goal — and in the MVP, curated drivers mean even the *first* `plan` in step 2 makes no Opus call either, only step 6's Sonnet categorization call for the real `create` diff.

## 9. Known limitations (flagged, not solved in MVP)

- **Driver set is curated and closed in the MVP.** Only `(provider, resource)` pairs aiform's own maintainers have hand-built via `PROCESS.md`'s dev loop are usable — `digitalocean`/`compute` is the only one. A user needing an unsupported pair has no self-service path today; they'd have to request it (or contribute it) upstream. On-the-fly generation with an explicit per-use user approval prompt is the planned fix (see "Driver curation" above), deferred specifically because three real generation attempts (documented there) showed it isn't reliable enough yet to run unattended — that's a reliability problem to solve, not just an engineering task to wire up.
- **Driver correctness still drifts as CSP APIs change**, curated or not. No mechanism auto-detects "this driver is stale" — a DO API deprecation just fails loudly at apply time, requiring a maintainer to fix and re-release it. No driver versioning or migration story exists yet.
- **No dependency graph.** MVP supports only independent resources planned/applied one file at a time (e.g. a compute resource followed by a DNS record referencing its IP is out of scope — a real system eventually needs this).
- **Only one resource kind is implemented.** `network` and `load_balancer` are named in Terminology as resource kinds the vocabulary already accommodates, but no `ResourceDriver` subclass exists for either yet — `compute` (via DigitalOcean) is the only one built. Adding a second kind or a second provider is expected to require zero orchestrator changes, but that claim is untested until it actually happens.
- **Opus review cost at `plan`/`apply` runtime is now much smaller than originally designed.** With curated drivers, the only *runtime* Opus calls are the rare hash-mismatch re-review (a hand-edited driver) and gate #2 before a destructive `apply` — driver review itself moved to development time (`/code-review`, not billed per end-user run). At current pricing (`claude-sonnet-5` ≈ $3/$15 per MTok, `claude-opus-5` ≈ $5/$25 per MTok) this is a real but now much smaller ongoing operating cost than Terraform's zero-cost static plan, traded deliberately for the flexibility Terraform's static ForceNew flag can't offer.
- **Single local state file, no locking, no multi-user story.** Two concurrent `aiform apply` runs against the same `state.json` can race or corrupt it. Deliberately deferred, mirroring Terraform's own early single-operator local-state era.
- **No state versioning/corruption recovery beyond a single `.aiform/state.json.backup`** written before every overwrite. Cheapest possible mitigation, not a real history/rollback mechanism.
- **No state schema migration story.** `aiform_state_version` exists in the schema (§3) but nothing reads or acts on it yet — a future schema change has no defined upgrade path for existing `.aiform/state.json` files. Deferred until the schema actually needs to change.
- **LLM plan/diff decisions are non-deterministic by nature.** Even with `output_config.format` constraining the *shape* of Sonnet's/Opus's answers, two `plan` runs against byte-identical input could produce differently-worded rationale or, rarely, a materially different categorization — something `terraform plan` on unchanged input structurally cannot do. This is inherent to the project's premise, not a bug to eliminate.
- **Opus review is a second opinion, not a proof — for curated drivers too.** A subtle bug in a driver could pass `/code-review` (or, for the runtime re-review/gate #2 paths, Opus review) and only manifest on an untested attribute combination during a live apply — the DigitalOcean compute driver's own build process is direct evidence review doesn't catch everything (see "Driver curation" above: Opus approved drafts with the wrong credentials key intact). The real backstop remains the human confirmation prompt and, for curated drivers, the hand-written acceptance test suite. There's currently no mechanical equivalent of Terraform's `lifecycle { prevent_destroy = true }` — a user's prose "don't destroy this" in the Intent section is advisory to the LLM, not enforced. A structured `lifecycle: {prevent_destroy: true}` frontmatter field is a strong candidate for a near-term follow-up, not the MVP.
