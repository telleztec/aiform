# Plan: write the deployment name and an aiform marker onto every resource (#249)

Approved by the repo owner on 2026-10-01 through the decision page
https://claude.ai/artifact/VfAd7cPoAoqjPZMU23Q6py (plan id `deployment-tags`,
page version `2026-10-01T18:58:28Z`), read back from the page database after
the owner said in chat they had answered. Recorded choices: `d1`
orchestrator-passes, `d2` txt-record, `d3` probe-then-tag-or-name, `d4`
no-backfill, `approve` approved. Earlier revisions the owner requested: a
separate PR from #201, colon format without the word "deployment", marker and
deployment tag together, and "all resources must have the deployment name, no
exceptions".

Pairing: Sonnet 5 authors, Opus 5 reviews.

## Problem

A droplet created by deployment `a` looks the same on DigitalOcean as one
created by deployment `b`. No code adds any aiform-owned tag. `aiform-managed`
is specified in `specs/resource_tagging.md` but not built, and each driver
treats `tags` as its own parameter (droplet labels, firewall targets, none for
domains).

## Decisions

- The orchestrator computes the reserved tags and passes them to every driver;
  the base class applies them, so a new driver cannot forget. This adds a
  base-class step to the driver contract in `PLAN.md` §4.
- The reserved tags are `aiform-managed` and `aiform:<name>` (no word
  "deployment"). Example: `aiform:prod`. PLAN.md §10's future
  `aiform:<short-uuid>:...` shares the prefix; §10 is amended to say the first
  segment is the deployment name.
- Droplets carry both tags.
- Firewalls: a tag if the probe shows DigitalOcean can tag a firewall, else the
  deployment name goes into the name of firewalls aiform creates.
- Domains: a TXT record at the zone apex with value `aiform:<name>`, ignored by
  the diff engine. A domain name is the DNS hostname and cannot be prefixed.
- No backfill: resources created before this change stay unlabeled, and the
  spec says so.
- Runs in parallel with #201 PR 2 (#246), branched from `main`. Expect
  conflicts in `aiform/orchestrator.py`; update from `main` when #246 lands.

## What is built

- A probe session first (per `specs/driver_creation.md`), against
  DigitalOcean's tag API, for which resource types can carry a tag. No billable
  resources. The result goes in the spec before any code. If the probe
  contradicts the choices above for firewalls, follow the fallback recorded
  above; if it contradicts anything else, stop and report instead of
  deciding.
- Both tags are stripped from what `read()` returns, so they never reach the
  diff engine.
- A user-supplied `aiform-managed` or `aiform:...` tag raises `ValueError`
  naming it. A tag update never removes either.
- Tests first (red before green). The live suite asserts that a created
  resource carries the tags, and that the TXT marker does not show as drift.
- Specs and prompts updated: `specs/resource_tagging.md`, the per-driver
  specs, `specs/driver.md`, `PLAN.md` §4 and §10,
  `prompts/review_driver.md`.

## Not built

- The command that deletes orphans by tag.
- Any backfill of existing resources.
