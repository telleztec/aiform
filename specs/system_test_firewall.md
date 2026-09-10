# specs/system_test_firewall.md — `tests/system/test_cli_firewall.py`

## Purpose

Live end-to-end proof that `drivers/digitalocean/firewall.py` works
against the real DigitalOcean API, driving `aiform.cli.main()` rather
than the driver directly. Settles the assumptions `firewall.py`
hardcodes that a mock structurally cannot falsify, because the mock
encodes the same assumption the driver does
(`specs/digitalocean_domain.md`, "How this was gotten wrong twice").

Folded into the driver's own PR rather than deferred to a follow-up, a
change from the `#113 → #121` split. The reason is
`specs/driver_creation.md`'s Gate #1: "system-test bugs on the first
run" is one of the two signals for whether mechanism 1 is smooth enough
to automate, and a signal deferred to a later PR is a signal not
measured. Mechanism 2 will have to ship a system test with every
generated driver, so mechanism 1 should too.

## Interface

`pytest -m system tests/system/`, excluded from the default run by
`pyproject.toml`'s `addopts`. Needs `ANTHROPIC_API_KEY` and a
`DIGITALOCEAN_TOKEN` carrying firewall scope; skips (never fails) when
the token cannot read `/v2/firewalls`, since `aiform init`'s preflight
probes droplets only.

Helpers live in `tests/system/conftest.py`: `unique_firewall_name`,
`write_firewall_aiform_md`, `token_has_firewall_scope`,
`get_firewall_or_none`, `list_firewalls`, `delete_firewall_directly`,
and the session-scoped `_sweep_leaked_system_test_firewalls`.

## Behavior

- **Nothing billable, no traffic.** Every firewall is unattached
  (`droplet_ids: []`), which DigitalOcean hosts free and which cannot
  affect any droplet's connectivity. Both cases assert `droplet_ids ==
  []` against the live object rather than trusting the request — the
  suite's safety property is checked, not assumed. The only real cost is
  Anthropic-priced calls, so this must never run on the default
  `pull_request`/`push` trigger.
- **Case 1, full lifecycle**: create → re-plan is a no-op → edit rules →
  apply → re-plan is a no-op → destroy → confirm gone. Asserts the
  server-added `action` field is still returned, and that a tag *not* in
  the diff survives an update (the whole-object PUT must send every
  managed field).
- **Case 2, every supported rule shape**: tcp single port, tcp range,
  udp `"0"`, icmp `"0"`, sources-by-tag, and an IPv6 range — all applied
  at once and required to converge to a stable no-op. This is the case
  that would surface a field the spec calls required but DO rejects, or
  demands but the spec omits.

### Anthropic call counts, measured

Not derived from reasoning about which gates fire — reasoning about it
is what made the domain suite wrong. Measured by running the sequence:

| Step | Calls | Why |
|---|---|---|
| first `plan create` | 1 | intent parse: the `.aiform.md` sha256 is new |
| first `plan apply` | 1 | intent parse again — `plan create` does not write the tracked sha, only a completed apply does |
| **re-plan, unchanged** | **0** | sha matches, diff empty |
| `plan create` after an edit | 2 | intent parse + `categorize_diff` |
| `plan apply` of that update | 2 | the same pair |
| **re-plan after the update** | **0** | converged |
| `plan destroy` | 1 | gate #2 reviews a DESTROY |

All seven rows are asserted by the suite, not merely tabulated — an
earlier version left the update-apply and destroy rows unmeasured
because those steps ran without `--verbose`.

The two zeros are the point. Zero is only reachable if `read()`
round-trips exactly against the params the user wrote, which is what
every rejection in `_validate_rule()` exists to guarantee.

**`tests/system/test_cli_domain.py` asserts zero on its first
`plan create` and is red on `main` for this reason** — its comment
reasons about categorization (#118) and gate #1 (#119) and overlooks
intent parsing entirely. Not fixed here: it is a pre-existing defect in
another suite, and `PROCESS.md` says to file rather than fold in.

### Cleanup

Two layers. `teardown_tracked_resources` drives a real `plan destroy` in
a `finally`, which is the ordinary path but depends on the code under
test *and* on a state entry existing. `_sweep_leaked_system_test_firewalls`
is the independent backstop: it re-implements listing and deletion
against the raw API rather than importing the driver, per
`specs/system_test.md`'s rule that a backstop must not depend on what it
backs up.

The sweep is stricter than the zone sweep and simpler: firewalls carry
tags *and* return `created_at`, so identity is the name prefix **and**
`aiform-system-test` **and** an age past `SWEEP_MIN_AGE_MINUTES`, with
no timestamp parsed back out of a name. All three must hold; anything
unrecognized is skipped, never deleted. A non-empty sweep warns loudly —
it is a bug report, never routine maintenance.

### Preconditions

The `aiform-system-test` tag must exist before the first apply.
Firewalls are the first resource where that matters: a referenced tag
must already exist (probes `19`/`20`), unlike droplet creation which
auto-creates one. `ensure_system_test_tag()` creates it idempotently
rather than skipping, because a suite that silently does not run on a
fresh account protects nobody. On this account the droplet suite had
created it incidentally, which is why the gap was invisible until
review.

## Edge cases / errors

- **A driver that raises after its POST leaks a live resource**, and the
  sweep's age floor means it stays leaked for an hour. Observed: the
  first run of this suite hit a reserved-`LogRecord`-attribute collision
  in `create()` *after* the firewall existed, so no state entry was
  written and teardown had nothing to destroy. `create()` now rolls back
  explicitly, mirroring `domain.py`.
- **Two bugs on the first run**, both invisible to 991 unit tests: that
  collision (which only raises once a handler has the level enabled, and
  unit tests never call `log.configure()`), and the call-count
  assumption above. Both are recorded here rather than quietly fixed,
  because "system-test bugs on the first run" is a Gate #1 measurement.
- **A token without firewall scope skips**, and the sweep tolerates the
  same, since it is autouse for all of `tests/system/` and runs on a
  droplet-only session too.

## Out of scope

- **Attachment.** No case attaches to a droplet. That is what keeps the
  suite free and zero-blast-radius, per
  `specs/digitalocean_firewall.md`'s Out of scope; the
  `waiting → succeeded` and `pending_changes` transitions stay
  unexercised and are marked recalled-not-verified there.
- **Re-proving the CLI, orchestrator, gates or state machinery.**
  `test_cli_digitalocean.py` owns that.
