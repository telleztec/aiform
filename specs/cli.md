# specs/cli.md — `aiform/cli.py`

## Purpose

The `aiform` command-line surface (`PLAN.md` §7, minus the `driver ...`
subcommands, which are unbuilt and not currently being built — see Out
of scope): `init`, `plan
create`, `plan apply`, `plan destroy`, `plan refresh`, `plan show`,
`resource check`, `resource metrics`, `resource status`.
Argument parsing, plan/state output formatting, error-message
formatting, and process exit codes all live here — `orchestrator.py`
does none of this (`specs/orchestrator.md`'s "Out of scope": "All CLI
argument parsing, output formatting/printing... `cli.py`, not built
yet"). `aiform/__main__.py` is the one-line `python -m aiform` entry
point that calls `cli.main()`.

**Two supported invocations, both reaching `cli.main()`:**

- `python -m aiform ...` via `aiform/__main__.py`. Always available in a
  checkout; this is what CI and the test suite use.
- `aiform ...` via the `[project.scripts]` console script in
  `pyproject.toml` (`aiform = "aiform.cli:main"`). This is the surface a
  user is expected to type, and the one `init` names in its output.

`main`'s signature — optional `argv`, `int` return — is already the shape
a console-script entry point requires, so the entry point is a
`pyproject.toml` declaration with no code change. Note it materializes
only on install: an editable install created before the entry was added
does not gain the command until `pip install -e .` is re-run.

## Interface

```python
def main(argv: list[str] | None = None) -> int: ...
```

`argv=None` → `sys.argv[1:]` (argparse's own default). Returns a
process exit code; `aiform/__main__.py` is `sys.exit(main())`. Never
calls `sys.exit()` itself — kept a plain function so tests call it
directly and assert on the return value plus captured stdout/stderr,
without a `SystemExit` to catch.

Everything else in this module (the argparse construction, the five
per-subcommand handlers, the plan/state/apply-result printers, the
verbose Anthropic-call counter) is a private implementation detail of
`main()`, not a public interface another module imports — `cli.py` is
a leaf: every other `aiform/*.py` module is called *by* this one,
nothing calls back into it. Tests exercise it exclusively through
`main(argv)` plus `capsys`, the same way a real invocation would.

## Behavior

### Global flags, and where they attach

`-v`/`--verbose`, `--no-color` are accepted both before and after the
subcommand token (`aiform -v plan create` and `aiform plan create -v`
both work) via a shared argparse parent parser attached at every level.
**This is the intent, and it is currently false — see #134**: the
subparser's `store_true` default overwrites the root parser's parsed
value, so only the post-subcommand spelling takes effect. The
`aiform resource` commands inherit the bug unchanged — they attach the
same parent parser, so `aiform resource check -v` works and
`aiform -v resource check` silently does not. Not fixed here: #134 is
one fix across every subcommand, not three more instances of it.
`--state-file <path>` (default `state.DEFAULT_STATE_PATH`, i.e.
`.aiform/state.json`) is accepted on every subcommand that touches
state — `plan create`/`apply`/`destroy`/`refresh`/`show` and
`resource check`/`metrics`/`status` — but not on `init`, which never
reads or writes state.

### `aiform init [--provider digitalocean]`

- `--provider` must name a provider `config.PROVIDER_TOKEN_ENV_VARS`
  knows about (today: only `digitalocean`) — an unrecognized value is a
  clean `Error: ...` (exit 2), not a stack trace. Deliberately reads the
  supported-provider set from `config.py` rather than hardcoding it a
  second time here, so a future second provider doesn't need a matching
  edit in this module.
- Creates `.aiform/` (`mkdir(parents=True, exist_ok=True)`) — **never**
  `.aiform/credentials.env` itself, and never with any credential value
  or interactive prompt (`PLAN.md` §8, `CLAUDE.md`'s Credentials
  section): the user creates that file by hand.
- Appends the standard entries (`PLAN.md` §8) to the repo-root
  `.gitignore` if missing — `.aiform/credentials.env`,
  `.aiform/state.json`, `.aiform/state.json.backup`, `.aiform/logs/`
  (`specs/log.md`), `.aiform/ssh/` (issue #175 — see below), `.env`,
  `__pycache__/`, `*.pyc`. Idempotent: a line already present (exact
  string match) is not duplicated on a second `init` run. Creates
  `.gitignore` if it doesn't exist yet. Deliberately **excludes**
  `.aiform/trash/` — `PLAN.md`'s "Trash directory" section states it is
  "Not gitignored."
- **Generates aiform's managed SSH keypair, local-only, no network call
  (issue #175, `specs/ssh.md`).** Calls
  `aiform.ssh.ensure_managed_key(aiform.ssh.DEFAULT_SSH_DIR)` — get-or-
  create, so a second `init` run never regenerates an existing key — and,
  **only the first time the key is actually generated for this project**,
  also calls `aiform.ssh.generate_backup_script()` and prints a one-time
  pointer at it (the path, a one-line explanation of why this key exists
  and what it's for, and an explicit "read it before running it — aiform
  never runs this script itself"). A re-run prints neither the pointer
  nor regenerates anything, matching `credentials.env`'s existing
  "instruct once, don't nag" treatment. This is deliberately the *one*
  exception to `init` never writing a secret value itself (see this
  spec's Edge cases / errors, and `CLAUDE.md`'s Credentials section): the
  key is an aiform-internal operational credential aiform generates and
  uses for its own lifecycle calls, not a copy of a user-owned account
  secret typed in by hand — the terminal-echo/shell-history rationale
  behind that rule doesn't apply to a keypair the human never types.
  aiform never registers this key with DigitalOcean at `init` time (no
  credentials are resolved here) — that's deferred to the first real
  `create()` call, which is the earliest point real provider credentials
  are actually needed (`drivers/digitalocean/compute.py`'s
  `_ensure_do_key_registered()`).
- Writes `examples/compute.aiform.md` (creating `examples/`) **only**
  when it doesn't already exist — never overwrites a user's edited
  starter file on a repeat `init`. Only written for `--provider
  digitalocean` (the only driver that exists); an unsupported provider
  already failed above, so this is unconditional once past the check.
  Deliberately written under `examples/`, where
  `orchestrator.discover_files()` (cwd-only `*.aiform.md` glob) will
  **not** pick it up: `init` must never produce discoverable config, or
  a user's very first `plan create` would propose creating a real,
  billable droplet they never asked for. The scaffold is a template to
  copy, and the printed instructions say so (next bullet).
- The scaffold's `ssh_keys` placeholder is a **fingerprint**, not a key
  name. DigitalOcean's `POST /v2/droplets` accepts key IDs or
  fingerprints and rejects names with a 422, so a name-shaped
  placeholder teaches a shape that cannot work. Carries a comment
  pointing at `doctl compute ssh-key list`.
- Prints instructions: how to set `ANTHROPIC_API_KEY` (environment
  variable, never a CLI flag or file) and the resolved provider's token
  env var (`config.PROVIDER_TOKEN_ENV_VARS[provider]`) — env var first,
  else hand-created `.aiform/credentials.env` at
  `config.DEFAULT_CREDENTIALS_PATH`, in `KEY=value` form. Never
  scaffolds that file with a value, never prompts for one interactively
  — same rule stated three times over in `CLAUDE.md`/`PLAN.md` for a
  reason.
- Prints the **next step** explicitly: copy an example into the working
  directory and edit it, plus the fact that discovery is cwd-only.
  Without this, `init` followed literally yields `Plan: 0 to create`
  with nothing explaining why — the scaffold is where discovery cannot
  see it, by design (two bullets up), so the instruction is the only
  thing connecting the two.
- **"Verifies that the credentials work" (`PLAN.md` §7) is implemented
  literally here — a reversal of this spec's own earlier narrowing,
  which is preserved below so the reasoning is auditable.**

  The bullet this replaces claimed "there is no side-effect-free way to
  confirm a token is *valid* against either API without spending a real
  call," and on that basis reported only whether a credential was
  *configured at all* (`os.environ.get` for `ANTHROPIC_API_KEY`,
  `config.resolve_credentials(provider)` for the provider token).

  **That premise was false.** Both APIs expose a free, read-only,
  side-effect-free probe:

  - Anthropic: `GET /v1/models` — the Models API. No tokens billed, no
    message created.
  - DigitalOcean: `GET /v2/account` — reads the account record; touches
    no resource.

  The cost of the false premise was real: during the 2026-08-31 live
  walkthrough the presence check printed `[✓] ANTHROPIC_API_KEY` for an
  identity-linked key that returned 400 on *every* endpoint. The
  failure surfaced much later, inside `plan apply`, pointing at the call
  site rather than at the key.

  Note this is **not** the same call as `specs/orchestrator.md`'s
  judgment call 3, which still stands: that one narrows the phrase for
  `plan create`, where a probe on every run would violate `CLAUDE.md`'s
  zero-API-calls-on-unchanged-input rule. The probes specified here run
  **only in `init`**, never on the `plan`/`apply` path.

- The check reports **four** states per credential, not two. A key that
  is set but rejected is a different problem from one that is absent,
  and neither is the same as an unreachable API:

  | State | Marker | When |
  |---|---|---|
  | configured and accepted | `✓` | probe returned 2xx |
  | not configured | `✗` | env var unset / `resolve_credentials` raised |
  | configured but rejected | `✗` | probe returned a verdict status (below) — **carries the API's own error text** |
  | configured, unverifiable | `?` | probe could not reach the API, or returned a non-verdict status |

  The `?` state is load-bearing: reporting `✗` for a working key because
  the user is offline is its own defect. Any connection-level failure —
  DNS, timeout, refused — is `?`, never `✗`. Probes use a short timeout
  (`_PROBE_TIMEOUT`, 5s) and no retries.

  Note what that does and does not bound: **each request**, not the
  command, and not name resolution. `socket.create_connection` resolves
  the host *before* applying the timeout, so a network that black-holes
  DNS waits out the OS resolver instead — typically several seconds per
  nameserver per attempt. Three probes run in sequence, so ~15s is the
  floor for a network that refuses connections cleanly, not a ceiling for
  every failure mode. `init` still terminates; it is not bounded as
  tightly as a per-request timeout suggests. `init` prints `Checking credentials...` before them so
  the pause is explained rather than looking like a hang. Response bodies
  are read with a size cap (`_MAX_PROBE_BODY`) because a socket timeout
  bounds each read, not a slow endless stream.

  A 5xx is `?`, not `✗`, for the same reason: a provider outage is not a
  verdict on the credential.

- **A DigitalOcean 403 triggers a second probe rather than a verdict.**
  DigitalOcean's scoped tokens can be valid for droplet operations while
  lacking `account:read`, so `GET /v2/account` answers 403 for a token
  that works perfectly for everything aiform does. Only **401** says the
  token itself was not accepted.

  But "the token is real" is not the question worth answering — a token
  scoped *without* droplet access fails every `apply`. So the account
  probe is always followed by `GET /v2/droplets?per_page=1`
  (`config.PROVIDER_DROPLET_PROBES`):

  | `/v2/account` | `/v2/droplets` | Result |
  |---|---|---|
  | 2xx | 2xx | `✓`, detail is the account email |
  | 2xx | 403 | `✗` "token is valid but cannot read droplets" |
  | 2xx | 401 | `✗`, rejected |
  | 2xx | 3xx | `?` — a redirect is distrusted, never shrugged off |
  | 2xx | 408/429/5xx/malformed | `✓` "&lt;email&gt; (droplet scope unverified)" |
  | 403 | 2xx | `✓` "authenticated (scoped token)" |
  | 403 | 403 | `✗` "token is valid but cannot read droplets" |
  | 403 | 408/429/5xx/other | `?` |
  | 401 | — | `✗`, rejected |

  A **401 on the droplet probe outranks a 2xx on the account probe**: the
  token was not accepted at all, whatever the first endpoint said moments
  earlier (it may have been revoked in between, or served from a proxy
  cache). Only an *inconclusive* second result defers to the first.

  "Authenticated" is tracked separately from the email, because a 2xx whose
  body carries no email still proves the token works. Collapsing the two
  would make that case indistinguishable from the 403 case, which proves
  nothing. For the same reason such a token is reported as
  `"authenticated"`, **not** `"authenticated (scoped token)"` — that label
  is reserved for the token that could not read the account at all.

  A **malformed** droplet response is treated like a transient failure, not
  like a bad token: if the account probe already authenticated, the result
  stays `✓ (droplet scope unverified)`. Only a 3xx breaks that rule, since a
  redirect on a token-bearing request is exactly what the no-redirect opener
  exists to distrust.

  The 2xx-then-inconclusive row matters: a rate limit on the *second*
  request must not discard what the first already proved. The token
  authenticated; only the scope check is missing, and the result says
  exactly that rather than reporting a working token as unverifiable.

  **The droplet probe runs unconditionally, not only after a 403.** A
  token granted `account:read` without droplet scopes answers 2xx on the
  first probe, so gating the second on a 403 would let that token print a
  green check and fail on the first `apply` — the same false green
  reached by the other path.

  Note what the second probe does and does not establish: **read** scope
  on droplets. It cannot prove the token may create or destroy one, and
  no free probe can. `✓` means "this token can talk to DigitalOcean and
  see droplets", not "every `apply` will succeed".

- **A redirect is refused, not followed.** `urllib` re-sends the
  `Authorization` header verbatim to a redirect target, including a
  cross-host one — `requests` and `httpx` both drop *that* header. These
  probes carry a provider token, so the opener rejects 3xx and reports `?`
  rather than chasing it and leaking the token to wherever it pointed.

  The emphasis matters, and cost us once. `httpx` strips less, and less
  uniformly, than "it drops auth headers cross-origin" suggests: `Cookie`
  is popped on **every** redirect (the `pop` sits outside the
  `_same_origin` guard in `_redirect_headers`), while `Authorization` is
  dropped only cross-origin — and not even then on a same-host
  `http`→`https` upgrade, though that carve-out is narrower than it
  sounds: `_is_https_redirect` also demands the default ports on both
  ends, so `http://host:8080` → `https://host` *does* lose it. A custom
  auth header is dropped on **no** redirect, ever. The Anthropic probe in
  `specs/llm.md` sends `x-api-key`, so "httpx drops it" was never true of
  that probe, and it followed redirects with the key attached until #97.
  Both probes now refuse a 3xx, and so does every billed model call
  (`llm.build_client()`, #101); read this bullet as the shared policy
  rather than a fact about one HTTP client.

  **The `Location` is never parsed.** `HTTPRedirectHandler` calls
  `urlparse(newurl)` *before* consulting `redirect_request`, so a
  `Location: http://[::1` from a captive portal raises `ValueError` out of
  the probe — an error about a header we had already decided not to follow,
  which then has to be attributed to something. The opener raises the 3xx
  as an `HTTPError` itself instead of letting the base class parse first.
- **A `ValueError` while sending blames the token only when the token is
  the plausible cause** — when it could not be a legal HTTP header value at
  all (a stray `\r`/`\n`, or bytes that will not encode as latin-1). Any
  other `ValueError` reaching that branch — a malformed `https_proxy` in
  the environment is the live example — is reported as a send failure, not
  as a bad credential. Both messages are canned: `http.client` quotes the
  whole header value in its message, so the exception's own text can never
  reach `detail`, which is what makes the two cases indistinguishable by
  message and forces the decision to be made from the token itself.
- **A 2xx of the wrong shape is `?`, not `✓`.** A proxy or captive portal
  answering 200 with arbitrary JSON is not evidence the token works, so
  the account probe requires an `account` object and the droplet probe a
  `droplets` key before either counts as a pass.
- **Both probes name the statuses that are a verdict; everything else is
  `?`.** The provider token's set is `{401, 403}`
  (`config.PROVIDER_TOKEN_VERDICT_STATUSES`), the Anthropic key's is
  `{400, 401, 403}` (`config.ANTHROPIC_KEY_VERDICT_STATUSES`). A status
  outside the set is a verdict on the *endpoint*, not the credential, and
  telling a user to rotate a working credential is the same class of error
  as passing a broken one.

  They differ in exactly one status, **400**, and for a reason specific to
  each API. An identity-linked Anthropic key rejects with 400 rather than
  401, which is the case the Anthropic probe exists for. DigitalOcean has
  no such case, and its probe URLs are hardcoded, so a 400 there is far
  likelier to mean a moved endpoint, a corporate proxy or a hijacked DNS
  answer than a bad token. A 404 is outside both sets for that same
  reason — on the Anthropic side an `ANTHROPIC_BASE_URL` gateway that
  proxies `/v1/messages` but not `/v1/models` answers 404 for a key that
  works.

  **403 is kept in both sets even though that argument reaches it too** —
  an unmatched route on an AWS API Gateway REST API answers 403 rather
  than 404, so the same gateway that motivates excluding 404 can produce a
  false `✗`. It stays because against the real APIs a 403 is a genuine
  verdict: `permission_error` from Anthropic, and on DigitalOcean the
  scoped-token case the two-probe table above exists to resolve. Keeping a
  real rejection is worth the narrower false positive, and this is a
  deliberate trade rather than an oversight.

- **408 and 429 are `?`, never `✗`** — on both providers. A timeout or a
  rate limit says nothing about the credential, and DigitalOcean's
  limiter is shared with anything else using the token (`doctl`
  included), so a routine 429 must not tell a user their working token
  was rejected. This is the same reasoning as the 5xx rule above; a bare
  `code < 500` test gets it wrong.

- On a 2xx the DigitalOcean probe reports the **account email** as its
  `detail`. `CLAUDE.md` notes this machine has two DigitalOcean accounts;
  a token silently belonging to the wrong one is the failure mode most
  worth surfacing at `init`, and the email is what makes it visible.

- **`init` still never fails on a `✗` or `?`.** Exit stays 0 regardless
  of probe outcome — a brand-new project legitimately has no credentials
  yet, and `init`'s job is to scaffold and inform, not to gate. This
  rule is unchanged from the narrowed version.
- Presence for `ANTHROPIC_API_KEY` is decided by the environment
  variable alone — `CLAUDE.md` makes that the only supported source
  ("env var only, never a CLI flag"). A key supplied any other way is
  reported not-configured by design rather than probed.

  **Known divergence:** `llm._anthropic_call` builds its client without
  passing an `api_key`, so the SDK's own resolution order also accepts
  `ANTHROPIC_AUTH_TOKEN` and an `ant auth login` profile. So a user
  authenticated that way sees `[✗] ANTHROPIC_API_KEY -- not set` from
  `init` and then a working `plan create` — the mirror image of the false
  green this preflight exists to remove, and a smaller error (it
  understates rather than overstates). The `✗` names the exact variable it
  checked, which keeps it literally true. Closing the gap means deciding
  whether `CLAUDE.md`'s env-var-only rule binds the runtime too, which is
  a wider question than this command.
- The Anthropic probe lives in `aiform/llm.py` (`verify_api_key()`),
  which already owns Anthropic client construction. It takes **no**
  `credentials` parameter — the SDK reads `ANTHROPIC_API_KEY` from the
  environment itself — so `CLAUDE.md`'s grep-verifiable "no `credentials`
  identifier in `llm.py`" property is preserved. See `specs/llm.md`.
- Both probes are stubbed in tests **by an autouse fixture**, and
  `tests/test_cli.py::TestInitMakesNoNetworkCalls` asserts that `init`
  attempts no socket connection at all.

  Opt-in stubbing was tried first and was silently wrong: `_guarded`
  swallows a probe failure, so a test that forgot to stub still *passed*
  while firing live requests with the developer's real tokens and
  printing the account email into test output. The only symptom was the
  suite getting slower. Both the autouse default and the explicit
  assertion exist because this claim cannot be verified by reading the
  tests.
- Forward note: `PLAN.md` §10's "Use short lived tokens instead of API
  Keys" (workload identity federation) would change *what* is being
  verified, not whether verification happens. The four-state contract
  above survives that change; the probe implementations would not.
- Exit 0 on a successful scaffold (regardless of the credential
  check's ✓/✗ outcome); exit 2 on an unsupported `--provider`.

### `aiform plan create [<file>.aiform.md ...] [--state-file <path>] [--json]`

- `files` (positional, `nargs="*"`) → `None` when empty, so
  `orchestrator.build_create_plan` falls through to its own
  cwd-glob discovery, exactly matching `PLAN.md` §5 step 1's
  "default: all `*.aiform.md` in cwd."
- Calls `orchestrator.build_create_plan(paths, state_path=..., client=<counting client>)`
  (see "The verbose Anthropic-call counter" below).
- Default (non-`--json`) output: one line per planned resource in a
  Terraform-style summary (`+`/`~`/`-`/`=` for
  create/update/destroy/no-op) followed by its rationale, then a
  one-line tally (`N to create, N to update, N to destroy, N no-op.`),
  then any warnings (`PLAN.md` §5's "left alone... reported with a
  warning" case) each on their own line. `update` entries additionally
  print `(likely replace)` when `entry.likely_replace` is set. This
  command's tally line never carries the `--yes` marker described under
  `plan apply` below — `create` has no `--yes` flag at all (issue #162),
  and always is pure preview.
- `--json`: prints `{"plan": [...], "warnings": [...]}` instead, one
  `{"resource_key", "action", "rationale", "likely_replace"}` object
  per planned resource, `warnings` as given by `build_create_plan`.
  Nothing else is printed to stdout in this mode (the verbose call
  count, if requested, still goes to stderr — see below — so `--json
  --verbose` output stays parseable).
- Exit 0 on a successfully printed plan (regardless of what it
  contains — a plan showing destroys is not itself an error). Exit 2
  on `PlanBlockedError`/`DriverExecutionError`/`ValueError`
  (`parser`/pydantic validation failures propagate as `ValueError`
  subclasses, per `specs/orchestrator.md`'s "Edge cases")/
  `FileNotFoundError` (an explicitly-named file that doesn't exist) —
  `main()`'s shared error formatting, see below.

### `aiform plan apply [<file>.aiform.md ...] [--yes] [--state-file <path>]`

Re-plans in full immediately before executing — `specs/orchestrator.md`'s
"Out of scope" names this as `cli.py`'s job (`apply_plan()` only ever
takes an already-built plan):

1. `orchestrator.build_create_plan(...)`, same as `plan create`, using
   the **same** counting client as step 2 (one running tally covers the
   whole `apply` invocation, not two separate ones).
2. Prints the plan the same way `plan create` does (no `--json` option
   here — `PLAN.md` §7 doesn't list one for `apply`), so the user sees
   what's about to happen before any confirmation prompt — **with one
   difference**: `_print_plan(..., yes=args.yes)` appends
   `" (auto-approved via --yes, executing now)"` to the tally line when
   `--yes` is set **and the plan has at least one non-`NO_OP` entry**
   (issue #162), printed at this step, before step 3 runs gate #2 or
   executes anything. The `NO_OP`-only exception matters on a repeat
   `apply --yes` against an unchanged project: every entry is skipped by
   `apply_plan()`, nothing executes, and a marker claiming otherwise
   would be actively wrong rather than merely premature. Without `--yes`
   the tally line is unmarked, same as `plan create`'s — the `(y/n)`
   prompt that follows it in step 3 already reads unambiguously as a
   pending decision, so no marker is needed there. The marker describes
   one specific fact: the `(y/n)` prompt step 3 would otherwise ask is
   skipped. It is not a promise that nothing can still stop the run —
   gate #2's `block` flag (unconditional, even under `--yes`) and
   judgment call 7's single-resource replace re-review
   (`specs/orchestrator.md`) can both still halt or pause execution
   after this line prints, exactly as they could without `--yes`.
3. `orchestrator.apply_plan(planned, state_path=..., yes=args.yes, confirm=_confirm, on_review=_print_review_flags, client=<same counting client>)` —
   `_confirm` is this module's own confirmation function (see "Confirmation and
   non-interactive runs" below), always passed regardless of `--yes`,
   since `apply_plan`'s single-resource `DriverUpdateNotSupported`
   fallback confirmation is never skippable by `yes=True`
   (`specs/orchestrator.md` judgment call 7) and needs a sane behavior
   if that path is hit with no TTY attached. `_print_review_flags`
   (`resource_key: concern [severity]`, one line per flag) is passed as
   `on_review` (issue #166) so each gate #2 review's non-blocking flags
   print live, immediately before the confirmation prompt they explain —
   under `--yes`, in the same spot the (skipped) prompt would have been,
   since the flags are the record of what the gate said regardless of
   whether anyone was asked to confirm.
4. Prints the `ApplyResult`: one line per executed `PlanEntry`
   (`resource_key: action` — a replace is reported as `update (replaced)`
   when `likely_replace` is `True` on the returned entry, distinguishing
   it from a plain in-place update), then, if `aborted`, a final `Apply
   aborted.` line. `review_flags` is **not** printed here — step 3's
   `on_review` callback already printed each review's flags live, before
   its confirmation; printing them again from the returned `ApplyResult`
   would duplicate that output and was the ordering bug issue #166
   describes (flags only ever appeared here, after the prompt they were
   meant to inform).
- Exit 0 if `apply_plan` returns `aborted=False`. Exit 1 if
  `aborted=True` (the user declined, or a mid-loop replace confirmation
  declined — a legitimate, non-exceptional outcome, but not "success"
  for a script's purposes). Exit 2 on the same exception set `plan
  create` uses, from either the planning or the apply call.

### `aiform plan destroy [<file>.aiform.md ...] [--yes] [--state-file <path>]`

Mechanism A (`PLAN.md` "Resource deletion"): plans and applies in one
pass, unconditionally subject to gate #2 by construction (every entry
`build_destroy_plan` produces is `action=DESTROY`, and `apply_plan`'s
`needs_review` is true whenever any entry is a destroy).

1. `orchestrator.build_destroy_plan(paths, state_path=...)` — no `client`
   parameter (`build_destroy_plan` never calls an LLM — Mechanism A
   skips categorization entirely, `specs/orchestrator.md`).
2. Prints the plan the same way `plan create`/`apply` do, including
   `plan apply`'s `--yes` tally-line marker (issue #162) — destroy
   routes through the same `_plan_apply_and_report` call site, so this
   isn't a separate implementation, just the same behavior reached from
   a second command.
3. `orchestrator.apply_plan(planned, state_path=..., yes=args.yes, confirm=_confirm, on_review=_print_review_flags, client=<counting client>)` —
   the counting client is still passed here even though step 1 made
   no LLM calls, since `apply_plan` itself may (gate #2's batch review
   always fires for a destroy plan).
4. Same `ApplyResult` printing as `plan apply`, including `on_review`
   printing that review's flags live rather than `_print_apply_result`
   printing them after the fact.
- Same exit-code convention as `plan apply` (0 / 1 aborted / 2 error).

### `aiform plan refresh [--state-file <path>]`

- `orchestrator.refresh_state(state_path=...)` — no `client` argument
  passed or accepted here (`refresh_state` takes none; `PLAN.md` §7:
  "no LLM calls at all").
- Prints the refreshed `State` the same way `plan show` does (see
  below) — this command differs from `show` only in that it calls
  `refresh_state()` (which also durably writes the refreshed
  attributes) instead of `state.load()`.
- Exit 0 on success, 2 on `PlanBlockedError`/`DriverExecutionError`
  (a missing driver or bad credential for a tracked resource).

### `aiform plan show [--state-file <path>]`

- `state.load(args.state_file)` directly — no orchestrator
  involvement at all (`specs/orchestrator.md`'s "Out of scope": "`plan
  show`... needs no orchestrator involvement... a direct `state.load()`
  plus formatting, entirely in `cli.py`").
- Prints, per tracked resource: `resource_key`, `id`, `attributes`
  (pretty-printed JSON), driver `path` and a short (12-character)
  prefix of `sha256`, `last_applied_at`/`last_refreshed_at`, and
  `aiform_md_path`. An empty `state.resources` prints a single `no
  resources tracked` line rather than an empty table.
- Exits 0 on a successfully loaded, printed state. A corrupt or
  schema-violating `state.json` is **not** a special case this command
  adds — but it isn't uncaught either: `state.load`/pydantic's
  `ValidationError` (a `ValueError` subclass) and a malformed file's
  `json.JSONDecodeError` (likewise) both fall through to `main()`'s
  shared `_HANDLED_EXCEPTIONS` handling (see "Error formatting and exit
  codes" below) the same as every other command's operational errors —
  printed as `Error: ...` on stderr, exit 2. Corrected from an earlier
  draft of this spec that claimed these propagate uncaught: `main()`'s
  error handling is a single, command-agnostic `try`/`except` around
  all dispatch, so no per-command carve-out like that is actually
  possible without deliberately narrowing that `except` clause, which
  this module doesn't do — a clean, consistent error message beats an
  uncaught traceback here, same as everywhere else in this module.

### Confirmation and non-interactive runs

`_confirm(prompt: str) -> bool` is this module's confirmation callback,
passed to every `apply_plan()` call — **not** `orchestrator.default_confirm`.
It checks `sys.stdin.isatty()` first: if `False`, raises `RuntimeError`
naming the prompt and telling the user `--yes` is required for
non-interactive runs, **without ever calling `input()`**. If `True`, it
calls `orchestrator.default_confirm(prompt)` — the no-TTY check is the
only thing it adds, so the `y`/`n` prompt (re-asked until the answer is
exactly `y` or `n`, no implied default — #182) and the pre-prompt input
flush that makes the answer trustworthy (`specs/orchestrator.md` step 2,
#163) live in one implementation rather than two copies that can drift. The
raise-on-no-TTY behaviour stays this module's, which is why `_confirm` —
not `default_confirm` — is still what `apply_plan()` is handed.
This exists specifically for `specs/orchestrator.md` judgment call
7's scenario: a fully non-interactive `--yes` run that still hits the
single-resource `DriverUpdateNotSupported` fallback confirmation (never
skippable by `--yes`) needs to fail cleanly instead of hanging forever
on `input()` with no TTY attached to answer it. The `RuntimeError` this
raises is caught by `main()`'s shared error handling (exit 2), same as
any other operational error.

`_print_review_flags(flags: list[PlanReviewFlag]) -> None` is the
separate seam for flag visibility (issue #166) — `resource_key: concern
[severity]`, one `print()` per flag, no other formatting. Passed as
`apply_plan()`'s `on_review` argument, it is unaffected by the TTY check
above: it never blocks on input and has no TTY dependency (a broken-pipe
`print()` failure here is the same pre-existing, un-special-cased
possibility every other `print()` in this module already has, not
something this function adds), and is called the same way whether or not
a prompt follows it (`--yes` included).

### The verbose Anthropic-call counter

`CLAUDE.md`'s implementation-order section calls out, by name, verifying
`PLAN.md`'s "second `plan create` run makes zero Anthropic API calls"
claim with "`--verbose` logging or a request counter... don't just
assume it." This module implements exactly that, narrowly — not the
fuller, not-yet-designed logging system `PLAN.md` §10 names as a future
item (line format, log levels, output routing are all still open
questions there; this feature answers one specific question — "how many
model calls did this invocation make" — and nothing broader):

- A private `_CountingClient` wraps a real SDK client, exposing the same
  `.messages.create(**kwargs)` shape `llm._anthropic_call` actually
  calls (duck-typed — `llm.py` never imports or checks against a
  stricter interface than that). It increments an internal counter on
  every `.create()` call and only constructs the real client **lazily,
  on the first such call** — never at `_CountingClient()` construction
  time.
- This laziness is pinned by tests, not merely intended:
  `fail_if_anthropic_constructed` (`tests/test_cli.py`) makes the
  zero-call second run fail if `anthropic.Anthropic` is called. That it
  patches only that one name is enough because `build_client` is what
  constructs, and `build_client` is held to that name by every test that
  monkeypatches `llm.anthropic.Anthropic` — in both `tests/test_llm.py`
  and `tests/test_cli.py` — each of which fails if it switches to the
  `anthropic.Client` alias, since patching one name does not rebind the
  other. `TestBuildClientIsTheOnlyConstructor` contributes a different
  guarantee, that no other module in `aiform/` constructs one; it asserts
  a call site rather than a name, so it alone would not notice that swap.
  The property is stated as narrowly as the counter's own scope above —
  *no client is constructed* on the zero-call run, which is what
  `fail_if_anthropic_constructed` checks, and not the broader "the run
  costs nothing".
  What an eager construction would additionally cost is an httpx
  connection pool and SSL context that a zero-call run has to close for
  nothing, and — since `llm.build_client()` passes `http_client=` —
  closing it is now the caller's job rather than the SDK's.

  **Corrected:** earlier revisions of this bullet said eager
  construction reads `ANTHROPIC_API_KEY`, "which would make a truly
  zero-call `plan create` run newly *require* that variable to be set".
  The read is real — `Anthropic.__init__` does call
  `os.environ.get("ANTHROPIC_API_KEY")`, in every `anthropic` version in
  range — but the requirement is not: the client constructs fine with
  `api_key=None` and raises `TypeError: Could not resolve authentication
  method` only at the first request. So the conclusion survives on the
  grounds above, and the environment-footprint reason given for it never
  held. `llm._anthropic_call` builds only inside the function actually
  making a call, for the same reason; `_CountingClient` preserves it end
  to end.
- **It builds through `llm.build_client()`, not `anthropic.Anthropic()`
  directly** (`specs/llm.md`), so the client refuses redirects. This is
  not a stylistic preference: `_dispatch` injects a `_CountingClient`
  into *every* `plan create`/`apply`/`destroy`, which means
  `llm._anthropic_call`'s own client-building branch is never reached
  from this CLI. Hardening only that branch would have closed the #101
  leak on no shipping code path — the key would still have followed a
  3xx from here, and the prompt with it on a 307 or 308 (`specs/llm.md`
  is exact about which statuses carry a body).
- `_CountingClient.close()` closes the real client if one was built and
  does nothing if none was, and `_dispatch` calls it in the same
  `finally` that reports the count. `llm.build_client()` passes
  `http_client=`, which costs the SDK's `SyncHttpxClientWrapper.__del__`
  — the thing that used to close the pool — so the socket is now this
  module's to release. Doing nothing when no client was built is what
  keeps the laziness above intact: a zero-call run must not construct
  one merely to close it.
- One `_CountingClient` instance is created per CLI invocation that
  might need it (`create`/`apply`/`destroy` — never `init`/`refresh`/`show`,
  none of which ever call an LLM) and passed as every relevant
  orchestrator call's `client=` argument, so one running total covers
  the whole invocation (e.g. `apply`'s re-plan step and its execute
  step share one count, per that subcommand's Behavior entry above).
- When `--verbose` is set, the final count is printed to **stderr**
  (`[verbose] N Anthropic API call(s) made`) after the command's normal
  stdout output — stderr specifically so `--json --verbose` together
  still yield parseable stdout, and so `--verbose` never changes a
  command's exit code or its stdout contents.
- **This mechanism is unchanged by, and stays completely separate
  from, the structured logging described below** — it predates
  `specs/log.md` and continues to answer its own one narrow question
  ("how many model calls did this invocation make"), not folded into
  the newer system.

### Structured logging (`specs/log.md`)

`main()` resolves `config.resolve_logging_config()` and calls
`log.configure(verbose=args.verbose, logging_config=...)` immediately
after argument parsing, before any subcommand dispatch — the only
wiring this module does; every other module's own logger (`aiform.llm`,
`aiform.orchestrator`, `aiform.planner`, `aiform.parser`,
`aiform.driver_gen`) does its own logging independently once this is
called. `--verbose` only ever affects the live stderr echo — the
`.aiform/logs/` file always captures at `logging_config.level`
regardless of the flag; see `specs/log.md`'s "Two handlers, one
logger". **Additive, not a replacement**, for this module's existing
`print()`-based human-facing output (`_print_plan`/
`_print_apply_result`/`_print_state`) and for the verbose Anthropic-call
counter above — logging is a parallel channel for debugging/operational
visibility, not a UX replacement; nothing in this module's existing
print-based output changes.

One further, in-scope addition beyond pure wiring: `main()`'s own
exception handler (see "Error formatting and exit codes" below) also
calls `logger.error(...)` — naming the exception type and its
formatted message — immediately alongside the existing `Error: ...`
stderr `print`, not instead of it. This is the single most likely place
a real user hits a failure during `plan`/`apply`/`destroy`/`refresh`,
and structured logging covering it was part of the point of building
this module at all.

**Every invocation that reaches `log.configure()` logs at least two
lines, unconditionally** — an entry line immediately after `configure()`
returns, before any subcommand dispatch, and an exit line immediately
after dispatch returns, wrapping whatever the invoked subcommand itself
does (or doesn't) log in between:

- Entry: `logger.info("invoked: %s", " ".join(argv))`, where `argv` is
  exactly what `main()` resolved for `parser.parse_args()` (`argv if
  argv is not None else sys.argv[1:]`). This goes through the log
  *message*, not a `key=value` extra field — an argv element (e.g. a
  `--state-file` path) can contain spaces, and `specs/log.md`'s
  Formatter only quotes/escapes `msg`, not `extra` values. Safe to log
  verbatim: `_build_parser()` defines no credential-bearing flag
  anywhere (`DIGITALOCEAN_TOKEN`/`ANTHROPIC_API_KEY` are both
  env-var-only, per `CLAUDE.md`'s Credentials rules), so nothing
  sensitive can appear in argv.
- Exit: `logger.log(level, "", extra={"exit_code": <n>, "outcome":
  "success" | "unhealthy" | "error"})`, where `<n>` is exactly the value
  `main()` returns to its caller. `"success"` iff `exit_code == 0`;
  `"unhealthy"` **only** for `aiform resource check` exiting 1, which is
  a verdict about a resource rather than a failed command; `"error"` for
  everything else. `level` is `logging.INFO` for the first two and
  `logging.ERROR` for the third — the same outcome-driven
  severity `aiform/orchestrator.py`'s `_log_driver_outcome()`
  (`specs/orchestrator.md`) already uses, so a `grep ERROR
  .aiform/logs/` sweep for "any failure" also catches a failed
  invocation's own top-level exit line, not just the driver-level ones.
  Caught by `/code-review` twice: the first version always logged at
  INFO regardless of outcome, and a later one derived the outcome from
  the bare exit code, which quietly reclassified a *declined*
  `plan apply` (also exit 1, via `ApplyResult.aborted`) as `unhealthy`
  at INFO — dropping it out of the very sweep this bullet promises. The
  `unhealthy` case is therefore keyed on the command, not the integer.

This guarantees every `.aiform/logs/<...>.log` file traces back to a
specific command line and a specific result even when the invoked
subcommand logs nothing on its own path — `plan show`, `plan refresh`
with no drift, a `plan destroy` finding nothing left to destroy (see
`specs/log.md`'s file-per-invocation design). Previously these produced
genuinely empty (0-byte) files with no way to tell, after the fact,
which command produced them or whether it succeeded.

Two cases stay outside this guarantee, unavoidably: `argparse`'s own
error handling (missing subcommand, unknown flag) calls `sys.exit(2)`
from inside `parser.parse_args()`, before `log.configure()` has even
run — no log file exists yet at that point, exactly as before this
addition. A malformed `.aiform/config.yaml` `logging:` section is the
same shape of problem one step later: `main()` calls
`config.resolve_logging_config()` *before* `log.configure()` (it has
to — the config is what tells `configure()` what to do), so a
`ValueError`/`ValidationError` there is caught by `main()`'s own
narrow `try`/`except _HANDLED_EXCEPTIONS` and formatted/printed exactly
like `_dispatch()`'s handling does, exit code 2 — but, same as the
`argparse` case, no log file is created, since the config controlling
where and how to log is itself what failed to resolve. Likewise, an
exception outside the `_HANDLED_EXCEPTIONS` set propagates uncaught past
the exit-line log call — consistent with this
module's existing "let it fail loudly" stance (see below), not a gap
this addition tries to paper over.

### Error formatting and exit codes

`main()` wraps subcommand dispatch in one `try`/`except` over a fixed,
named set of "expected, operational" exception types —
`PlanBlockedError`, `DriverExecutionError`, `ValueError` (covers
pydantic's `ValidationError`, a `ValueError` subclass), `FileNotFoundError`,
`RuntimeError` (only ever actually raised here by `_confirm`'s no-TTY
case, since `config.resolve_credentials`'s `RuntimeError` is already
translated to `PlanBlockedError` before it reaches this module, per
`specs/orchestrator.md`) — printing `Error: <message>` to stderr and
returning exit code 2. `PlanBlockedError.reason` is used as the message
verbatim (its `str()` is the same text, but going through `.reason`
documents the intent); every other caught type uses `str(exc)`.
**Anything else propagates uncaught** — an unrecognized exception is a
real bug, not a formattable operational condition, matching this
codebase's consistent "let it fail loudly" stance
(`specs/orchestrator.md`, `specs/parser.md`, `specs/planner.md`) rather
than papering over it with a generic `except Exception`.

## Edge cases / errors

- `argparse`'s own error handling (missing required subcommand, unknown
  flag) is used as-is — it prints its usage message to stderr and calls
  `sys.exit(2)` itself, *inside* `parser.parse_args()`, before `main()`'s
  `try` block is even reached. Not reimplemented or caught here.
- `plan show`/`plan refresh` on a `--state-file` that doesn't exist yet:
  `state.load()` returns an empty `State()` (its own documented
  behavior, `specs/state.md`) rather than raising — printed as `no
  resources tracked`, exit 0. Consistent with `state.load`'s existing
  contract; `cli.py` adds no special-case here.
- A `plan create --json` run that also hits a warning (a tracked
  resource left alone) still reports it, inside the JSON envelope's
  `"warnings"` array — `--json` changes the plan's own representation,
  not whether warnings are surfaced at all.
- `init` run a second time in the same directory: `.gitignore` entries
  aren't duplicated, `examples/compute.aiform.md` isn't overwritten,
  `.aiform/` already existing is fine (`exist_ok=True`), the managed SSH
  key isn't regenerated and its backup-script pointer isn't reprinted —
  every step is independently idempotent, so re-running `init` is always
  safe.
- **`init` writing `.aiform/ssh/aiform_managed_key` is a deliberate,
  named exception to "init never writes a secret value itself"
  (`CLAUDE.md`'s Credentials section, and the "hand-created
  `.aiform/credentials.env`" bullet above).** The distinction: a value
  typed by a human into `credentials.env` risks terminal echo, shell
  history, or the output of a command an AI agent is driving — none of
  which applies to a keypair aiform itself generates locally and the
  human never types. `ANTHROPIC_API_KEY`/`DIGITALOCEAN_TOKEN` stay
  exactly as strict as before; only this one aiform-internal operational
  credential is exempt, and only because it's aiform's own, not a copy
  of a user-owned account secret.

## Out of scope

- **Every `aiform driver ...` subcommand** (`create`/`refresh`/`show`/
  `delete`/`publish`) — `PLAN.md` §7 lists these under its explicit "Not
  yet implemented below, and not currently being built" banner; §10
  confirms none of the interactive session shape is designed yet. Not
  part of this module. Note that these are the *only* commands driver
  generation is ever reached through: no `plan` subcommand specified
  here generates a driver, and a missing one is a permanent error.
- **The fuller logging system** named in `PLAN.md` §10's "Logging" item
  is now designed and wired (`specs/log.md`, "Structured logging"
  above) — this bullet is left here, corrected, rather than deleted, so
  the history of "this was once explicitly out of scope" isn't lost.
  What remains genuinely out of scope: a third `DEBUG` output tier
  (`specs/log.md`'s own Out of scope), and a `redact()`/`_redact()`
  helper for dumping raw request/response payloads under `--verbose`
  (`PLAN.md` §8) — no call site in this module logs one.
- **Colorized/`--no-color` output beyond plain ANSI codes on the plan's
  action markers** (`+`/`~`/`-`/`=`) — no theming, no configurability
  beyond the on/off switch `PLAN.md` §7 already names.
- **A `--json` mode for `apply`/`destroy`/`refresh`/`show`** — `PLAN.md`
  §7 only lists `--json` under `plan create`; not extended to the other
  commands here.
- **Concurrent-invocation safety** — `PLAN.md` §10's "Single local state
  file, no locking" limitation is orchestrator/state-level and applies
  unchanged here; this module adds no locking of its own.

## Addendum: `aiform resource ...` (`specs/driver_observability.md`)

A new **noun group** with three verbs, matching `aiform driver`'s shape rather
than `aiform plan`'s — none of these plans or applies anything, and none writes
state.

```
aiform resource check   [<name>] [--format text|json] [--state-file <path>]
aiform resource metrics [<name>] [--format text|json]
                        [--output <path>] [--state-file <path>]
aiform resource status  [<name>] [--format text|json] [--state-file <path>]
```

**Notation.** `<lower-case>` in angle brackets is a placeholder you replace;
everything else is typed literally. This follows docopt's angle-bracket
convention rather than the upper-case one, because upper-case `NAME` collides
with `aiform.md`'s literal `name:` field and a reader cannot tell the two
apart. `PLAN.md` §7 uses the same notation throughout.

---

### `aiform resource check [<name>]`

**Does:** asks the provider whether a resource is working right now, and
prints a one-line verdict per resource. Calls the driver's `health()`.

**Arguments:** `<name>` — the resource's `name:` from its `.aiform.md`, e.g.
`web-01`, not the full `digitalocean.compute.web-01` state key. Omitting it
checks every tracked resource, like its siblings.

**Output:** one line per resource, plus a coverage line when checking the
fleet — the coverage line is part of the report, so it goes to stdout in text
form and is a field of the document in `--format json`, never a stray line
that would make the JSON unparseable. **Beware piping `check`**: a pipeline
exits with the last command's status, so `aiform resource check | tee log`
discards the verdict this command exists to produce. Use `set -o pipefail` or
`${PIPESTATUS[0]}`.

When a verdict is **not** `ok`, the driver's `observations` print
indented beneath it — the detail you want exactly when something is wrong,
with no flag to remember, because the gate case and the diagnosis case never
overlap. `--format json` emits `status`, `summary` and `observations` for
every resource unconditionally.

```
$ aiform resource check web-01
ok  compute  digitalocean.compute.web-01  active, public v4 203.0.113.10

$ aiform resource check db-01
failing  compute  digitalocean.compute.db-01  status is "off"
    status       off
    locked       false
    last_action  power_off

$ aiform resource check
ok           compute  digitalocean.compute.web-01  active, public v4 203.0.113.10
failing      compute  digitalocean.compute.db-01   status is "off"
unsupported  domain   digitalocean.domain.example  no per-domain health signal
2 of 3 resources reported a health verdict; 1 unsupported
```

The second column is the resource type. It is a column of its own rather
than being left as the middle segment of the dot-joined key, which only a
reader who already knows that convention can pick out.

**Exit code — this is the only command whose exit code carries the answer:**

| Code | When |
|---|---|
| 0 | at least one verdict was produced, and every verdict is `ok` |
| 1 | any verdict is `degraded`, `failing` or `unknown` |
| 2 | no verdict at all: unknown or ambiguous `<name>`, unreadable state, or — checking the fleet — not one driver implements `health()` |

It is built this way to be a gate: `aiform resource check web-01 &&
./smoke-test.sh` for one resource, or a bare `aiform resource check` in CI.

**A resource whose driver declines `health()` is listed but does not fail the
aggregate.** Requiring every driver to implement `health()` before the fleet
gate is usable would make it unusable today, when none do. The coverage line
exists so that leniency is visible: a gate passing because it checked nothing
is the failure mode to avoid, and that case is exit 2, not exit 0.

With nothing tracked at all, no coverage line's arithmetic would mean
anything, so `check` prints the plain `no resources tracked` instead — the
exit code is still 2, unchanged from every other no-verdict case above.

---

### `aiform resource metrics [<name>]`

**Does:** prints counters and gauges — for one named resource, or for every
tracked resource when `<name>` is omitted. Calls the driver's `metrics()`, and
only that: `check` is the verb that asks `health()`.

**Arguments:** `<name>` — the resource's `name:` from its
`.aiform.md`, e.g. `web-01`, not the full `digitalocean.compute.web-01` state
key. **Omitting it means every tracked resource**, matching `plan refresh`,
`plan show` and `plan create`, which all operate on everything when given no
arguments. There is no `--all` flag: omitting `<name>` already says it, and a
second spelling of one meaning is what `specs/driver.md`'s "one writable
spelling per value" rule warns against.

`--format` selects the rendering, default `text`. `--output <path>` **appends**
to a file instead of writing stdout. No temp file, no rename, no
suffix rule, no rotation — aiform never deletes or truncates it; a run adds to
the end and the operator removes the file when done.

Without `--output` every format goes to **stdout, which is a clean stream** —
the report and nothing else. Logs and error
messages all go to stderr, so `aiform resource metrics --format json | jq ...`
works. A closed pipe (`| head -20`) is a successful run, not a
`BrokenPipeError` traceback.

Each run is preceded by a delimiter line carrying a UTC timestamp and the
invocation, so an accumulating file can be split back into runs. aiform never
reads the file back, never rotates it and never deletes it; a missing parent
directory or unwritable path is an ordinary error.

**Output**, `--format text` — aligned columns, meant to be read by eye and run
again a minute later to watch a number move:

```
gauge  memory_bytes  2147483648
gauge  cpu_percent   41.2
```

`--format json` emits the same data as structured records. There is no
exposition format: it has no consumer until `PLAN.md` §10's "Metrics pipeline
integration" gives it one.

**Exit code:** `0` if the command ran, `2` if it could not (name not found or
ambiguous, unwritable `--output`, unreadable state). `metrics` reports no
health verdict at all, so there is nothing for its exit code to carry.

**Both forms** make zero Anthropic API calls and write no state, and neither
aborts because one resource is sick: a driver that declines reports
`unsupported`, one that raises has the error recorded against that resource,
and the rest still render. A single broken driver must not blank a dashboard.

---

### `aiform resource status [<name>]`

**Does:** answers four independent questions about a resource — has aiform
deployed it, is it still there, does it still match what you declared, and is
it healthy. Composes a state lookup, a live `read()`, a config diff, and
`health()`; adds no new driver method.

**Arguments:** `<name>` as above; omitting it reports every tracked resource,
each under a header line naming it. This is the most expensive of the three commands — a live `read()`
**and** a `health()` per resource, so the fleet form costs 2N provider calls
against a rate limit shared with `plan`/`apply`.

**Output:** the resource's type, then four labelled lines, any of which can
be the surprising one:

```
type      compute
deployed  2026-09-10T14:02:11Z, id 123456789
live      present
config    in sync with examples/web.aiform.md
health    failing — status is "off"
```

`--format json` carries the same answers as structured fields rather than
as these sentences — `deployed_at` and `id` separately, and `config` as an
object with a real `in_sync` boolean — so a script reads a field where a
human reads a line. `specs/driver_observability.md` fixes that shape.

**Exit code:** `0` if the command ran, `2` if it could not. A resource that is
missing or drifted is an *answer*, not a failure of the command.

---

### Implementation notes

Each handler is a thin wrapper over `aiform/observability.py` — named for the sweep
it performs, not for a command, since no command is called `scan` — matching how
`_cmd_plan_refresh` wraps `orchestrator.refresh_state()`. All belong to the
**plain** dispatch set, not the LLM set: they make zero Anthropic API calls by
contract, so none is ever handed a `_CountingClient`.

Three things that will bite whoever wires up the parser:

1. `_dispatch()` currently reads `if args.command == "init"` and otherwise
   falls through to `args.plan_command`. A second noun group needs its own
   explicit branch, or it raises `AttributeError` on a `Namespace` that has no
   `plan_command`.
2. The sub-subparser needs `dest="resource_command", required=True`, mirroring
   `plan_sub`.
3. Every parser needs `parents=[global_parent, state_parent]` like its
   siblings — not because `args.verbose` would otherwise be missing (the root
   parser carries `global_parent` too), but so `aiform resource metrics -v`
   parses at all. See the `-v` caveat under "Global flags" and #134.

The rendering rules, `--output`'s append behaviour, and the per-resource
name-resolution errors are specified in
`specs/driver_observability.md` and not restated here.
