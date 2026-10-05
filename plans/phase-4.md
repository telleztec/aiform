# Plan: Phase 4 — orphan safety and restartable applies, as parallel PRs 4a, 4b, 4c plus the #265 probe

Point-in-time record, per `plans/README.md`. Committed on PR 4a's branch; PRs 4b, 4c and the probe cite this file in their `## Plan` sections.

## Approval

Approved by the repo owner on 2026-10-03 through a decision page (artifact `HraNBFaE5uYAGFLvjadNX4`, doc `decisions/phase-4-decisions`, submitted 2026-10-03T14:54:11Z, `page_version` 2026-10-03T00:48:57Z, read back from the page database). The owner then said in chat that they had posted, and asked for the plan to be made durable inside one of the PRs, the three PRs to be parallelized where they do not depend on each other, and agents to be used. The page's own note ends "Approved, create plans and spawn agents to write code, going in parallel where possible."

Recorded choices:

| Decision | Choice |
|----------|--------|
| Cut | `split`: three PRs (4a, 4b, 4c) and amend the PRD's delivery rules |
| #226 / #227 | `refuse` = **repair**: build the repair (#227) and relax #225's refusal |
| #235 | `report`: report the executed prefix on failure; no operational-direction model |
| #232 (P0) | `probe-first`: run the #265 probe, then choose the fix direction |
| #253 | `restart` = in: restartability belongs to Phase 4, in 4b |

The owner's notes, verbatim:

> Question #2: for #227 interactive consider a (y/n) prompt, if --yes is specified, just repair. Since we know the schema in the driver and we have the id, the repair code is not difficult.
>
> Question #3: ok to warn, but a --all --yes should clean up as requested, with longer timeouts, or more persistent retries. A script may learn to look for the partial delete failure and retry with --force, that's ok, then we should cleanup correctly, and if possible also remove the orphans, using aiform and deployment tags to search. The serial searches are expensive but the number of projected resources on these deployments will not be large, so it is ok to serially check the whole thing for orphans.

The owner also said, in chat, that Phase 3 is returned to after Phase 4.

Pairing: Sonnet 5 authors every PR; Opus 5 reviews every PR. The coordinating session never reviews. No change to any model-tiering default.

## What the user relies on

- A destroy never leaves a live resource pointing at something that is gone without saying so, and where the repair is mechanical aiform does it.
- A failed or interrupted `apply` or `destroy` tells the user what already happened, and a re-run converges instead of duplicating.
- Destroy and apply work on a deployment whose state or files are in a bad shape.

## PRD amendment (in PR 4a)

`specs/MULTI_RESOURCE_PRD.md` forbids parallel branches and concurrent implementation agents. For Phase 4 only, the rule becomes: PRs 4a, 4b, 4c may be developed in parallel because they touch different concerns, each is rebased onto `main` after another merges, merges are still serial, each PR needs its own green live `system-test` on its own head, and the header's "Next" line reads "Phase 4 (4a–4c), then reassess Phase 3". The one-phase-per-PR rule is replaced by the 4a/4b/4c cut below. The PRD's Phase 4 section records what each PR delivers as it lands.

## The cut

| PR | Closes | Concern | Branch |
|----|--------|---------|--------|
| 4a | #226, #227 | Destroying a droplet that a firewall lists repairs the firewall instead of orphaning it, on both destroy routes | `phase-4a-repair-dependents` |
| 4b | #253, #229; refs #235 | A failed or interrupted apply or destroy is reported and restartable | `phase-4b-restartable-apply` |
| 4c | #206, #183, #224, #234 | Destroy and apply survive bad state | `phase-4c-bad-state` |
| probe | #265 | Measure how long a firewall keeps a deleted droplet's id; findings only, no `aiform/` change | `probe-265-firewall-dead-id` |

Each PR is independently valuable and leaves `aiform` working end to end.

### 4a — repair, not refuse (#226, #227)

- When a destroy would remove a droplet (or any resource) that a tracked firewall references, the plan gains an **update** of that firewall: its live `droplet_ids` minus the destroyed id, written through the firewall driver's existing `update()`. State (`params`, `depends_on`) follows the live result.
- Interactive: a `(y/n)` prompt names the firewall and the id being removed. `--yes`: repair without asking. Declined: nothing is destroyed or changed.
- Both routes behave the same: `plan destroy <file>` (replaces #225's refusal) and the `AIFORM-DELETE-` marker route via `plan apply` (#226).
- Order: repair the firewall first, then delete the droplet, so a failure mid-way never leaves a firewall holding a dead id (the delete failing leaves the droplet running with a repaired firewall, which is reported per 4b).
- The user's `.aiform.md` is not edited. The notice says the file still names the droplet and that the next plan will flag it. `--force` keeps its meaning for dangling targets (`_resolve_dangling_targets`).
- Tests: planner/orchestrator unit tests for both routes (1→0, 2→1, no dependents, declined, `--yes`); a live system test for each route against a real droplet and firewall.
- Specs: `specs/orchestrator.md`, `specs/resource_dependencies.md`, `specs/cli.md`, `specs/MULTI_RESOURCE_PRD.md`.

### 4b — restartable and reported (#253, #229, #235 part)

- **#229 and #235 option 1.** On a failure in `apply_plan()`, the user is told which resources were applied before it. The report is a stable, greppable block (what executed, what failed, what did not run) so a script can detect a partial failure. Exit code stays 2.
- **#235 plan-time warning.** A destroy plan that removes a resource whose only role is to protect another resource in the same plan names the exposure window.
- **`plan destroy --all --yes`.** A failed delete is retried with bounded, persistent backoff before the run gives up. The owner expects scripts to retry with `--force`, so a forced re-run after a partial failure converges and reports correctly. Orphan removal inside `--all` is **not** in 4b; see "Deferred".
- **#253.** The retry after an accepted-but-unreported create must not make a second resource. Mechanism (the one design point beyond the owner's recorded choices; it is stated here so the reviewer and the owner see it): the DigitalOcean compute driver's `create()` tags the droplet at creation with a per-resource marker tag in addition to the deployment tag, and looks for a droplet carrying that marker before it posts a create; exactly one found is adopted (polled to active and returned), more than one is an error that names them. Because DigitalOcean applies the tags atomically with the create, this also covers the case where the response is lost before any id is seen, which an id-in-the-error approach cannot. No change to the `ResourceDriver` contract: the lookup is private to the driver.
- Live tests: remove the strict xfails that cite #253 (`RetryDuplicatesResource` cells T1, T3, the create reset/500/503 cells, C1, C2) once they pass live. Point the `ErrorOmitsResourceId` cells (T2 and the four `poll-*` cells) at #264 instead of no issue, and keep them strict xfails.
- Specs: `specs/orchestrator.md`, `specs/cli.md`, `specs/digitalocean_compute.md`, `specs/system_test_interrupt.md`, `specs/MULTI_RESOURCE_PRD.md`.

### 4c — bad state (#206, #183, #224, #234)

- **#206.** A dependency cycle recorded in state no longer blocks `plan destroy` for the whole deployment; the cycle is named and a way out is offered that does not need hand-editing `state.json`.
- **#183.** `plan destroy` with a tracked file missing from its recorded path no longer raises `FileNotFoundError`; it names the file and proceeds from state.
- **#224.** A firewall rule admitting two droplets by reference no longer fails partway through apply.
- **#234.** The edge type is kept instead of discarded, with no behavior change.
- Tests and specs as each issue states; a live system test for #224.

### Probe (#265)

A throwaway script under the repo's probe convention (`specs/driver_creation.md`): create one smallest droplet and one firewall listing it, delete the droplet, read the firewall back at intervals, and record whether and when the dead id leaves `droplet_ids`. Output is `knowledge/drivers/digitalocean_vpc/FINDINGS.md` (or a sibling probe directory) with the transcript. It sets #232's direction; #232 gets its own plan afterwards.

## Parallelism and its limits

- Four worktrees under `.claude/worktrees/`, one author agent each, each told its own path and branch and forbidden from touching another.
- Overlap: all three code PRs touch `aiform/orchestrator.py` and `aiform/cli.py`, in different functions (4a the destroy producers and plan entries, 4b `apply_plan` and the failure path, 4c state-driven destroy and parameter resolution). They will conflict textually where they meet; the later merge is rebased and re-tested.
- **Live tests are serialized.** The system suite shares the deployment name `default` and tag-keyed sweeps (#261), so two live runs at once can delete each other's resources. Author agents run the offline suite only; the coordinating session runs each head's live suite one at a time, and checks droplets, firewalls and domains for leaks after each.
- Merge order is decided at merge time, by what each PR's diff touches; each merge needs the owner's `/claude-merge-approved` for that PR and head.

## Deferred, not forgotten

- **4d: orphan detection and cleanup (#264).** The owner wants `--all --yes` to also remove orphans found by the `aiform` and `aiform:<deployment>` tags. That needs a way for aiform to list a deployment's provider resources, which is a `ResourceDriver` contract addition and so its own plan and approval; it is destructive and must never touch a resource that lacks both tags (the production WordPress droplet in particular). It follows 4b.
- #232 (P0), after the probe.
- #235 option 3 (model the operational direction) and tag-targeted firewalls (#235's create-side window).
- #261 (per-run deployment names), which would lift the live-test serialization.

## Safety

Live runs create droplets, firewalls and domains on the development account, named by `unique_droplet_name()`, tagged `aiform-system-test`, torn down by ledger id. The production droplet is never listed for deletion or touched.
