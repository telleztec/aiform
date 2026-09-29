# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

"""Probe session for DigitalOcean VPC peering, run to test a shape the
dependency model has no example of.

No VPC or peering driver exists and none is being written. This session exists
because `specs/resource_dependencies.md`'s model is built on resources that
*reference* other resources, and a peering is not that. Its entire purpose is to
BE a relationship: a node in the graph whose semantics are an edge. Nothing in
the driver set has that shape, so nothing tests whether the model can express it.

Four questions, in the order the model needs them:

  1. **Must both VPCs pre-exist?** Predicted yes, like droplet.vpc_uuid and
     firewall.droplet_ids -- every probed DigitalOcean edge so far refuses a
     referent that is not there.
  2. **What does DigitalOcean do when a peered VPC is deleted?** The interesting
     one. `digitalocean_vpc_member` established 409 "Can not delete VPC with
     members" for a droplet inside. A peering is not inside, so this could be a
     refusal, a cascade, or permitted-and-broken. Each implies a different
     relationship kind.
  3. **Is the relationship readable from the VPC side?** `/v2/vpcs/{id}/members`
     answers "what is inside me". If a VPC can also be asked what it is peered
     with, the provider supplies both directions here -- unlike a firewall, where
     a droplet cannot be asked what protects it.
  4. **Is `vpc_ids` ordered?** Peering A-B means the same as B-A, so if the API
     returns the pair as given rather than canonicalised, a future driver needs
     `UNORDERED_FIELDS` for it or every re-plan shows a phantom diff.

ANSWERED, first run: both VPCs must pre-exist (404); a peered VPC cannot be
deleted, with **412 "Can not delete VPC with peerings"** where a member gives 409
-- so a referential refusal cannot be recognised by status code; `vpc_ids` comes
back **reordered**, so it needs UNORDERED_FIELDS; and the relationship IS readable
from the VPC side. See knowledge/drivers/digitalocean_vpc_peering/FINDINGS.md.

What makes this worth probing beyond a resource kind: a peering is the first
**symmetric** relationship available. The two `vpc_ids` are indistinguishable in
role -- there is no parent and no child -- while every edge the model currently
describes has a direction. `specs/resource_dependencies.md`'s "Out of scope"
raises whether one acyclic graph can serve both provisioning and operations; a
symmetric relationship is the concrete case behind that question.

DELIBERATELY NOT PROBED: cross-account peering. DigitalOcean allows a peering
between VPCs in two different accounts, which breaks the assumption that every
edge endpoint is in one state file -- see #201. The repo owner scoped it out, and
this session has one account.

Also not probed: peering two VPCs whose ip_ranges overlap. That is a
*compatibility constraint between two targets* rather than a dependency, and
provoking it needs a VPC created with a deliberately colliding range, which
DigitalOcean may refuse at VPC-create time instead -- a different finding that
would need its own session to attribute correctly.

Costs: **nothing billable.** VPCs and peerings are both free, and no droplet is
created. The session is free enough to re-run.

Run:  python probes/digitalocean_vpc_peering.py --dry-run
      python probes/digitalocean_vpc_peering.py --mutate
      python probes/digitalocean_vpc_peering.py --sweep --mutate
"""

import datetime
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _probe import (  # noqa: E402
    SWEEP_MIN_AGE_MINUTES,
    Probe,
    base_arg_parser,
    unique_name,
)

SESSION = "digitalocean_vpc_peering"
REGION = "sfo3"
VPC_PREFIX = "aiform-system-test-vpc-peer"
PEERING_PREFIX = "aiform-system-test-peering"

# Distinct, non-overlapping, and clear of both region defaults observed at recon
# time (10.108.0.0/20 nyc3, 10.124.0.0/20 sfo3) and the other probe sessions'
# ranges (10.200-10.201), so a failure here is never about address collision.
RANGE_A = "10.202.0.0/24"
RANGE_B = "10.203.0.0/24"

NONEXISTENT_VPC = "00000000-0000-0000-0000-000000000000"

POLL_SECONDS = 10
POLL_ATTEMPTS = 24

DRY_ID = "<dry-run-id>"


def _created_id(result, key, name=None):
    if result.status >= 400:
        raise SystemExit(f"setup call failed with {result.status}; aborting session")
    if not isinstance(result.body, dict):
        if not result.status:
            return DRY_ID
        raise SystemExit(
            f"{key} {name!r} was created ({result.status}) but its body did not parse, so no "
            f"cleanup could be registered -- it is LIVE and untracked. Destroy it by hand NOW: "
            f"--sweep will not touch it until it is over {SWEEP_MIN_AGE_MINUTES} minutes old."
        )
    return result.body[key]["id"]


def _wait_for_status(probe: Probe, peering_id: str, target: str) -> str | None:
    """Poll a peering until it reaches `target`. Returns the last status seen."""
    if peering_id == DRY_ID:
        return "<dry-run>"
    last = None
    for attempt in range(POLL_ATTEMPTS):
        seen = probe.call(
            "GET",
            f"/vpc_peerings/{peering_id}",
            note=f"poll {attempt + 1}: peering status",
            record=False,
        )
        body = (seen.body or {}).get("vpc_peering") or {}
        last = body.get("status")
        print(f"     status={last!r} vpc_ids={body.get('vpc_ids')}")
        if last == target:
            return last
        if last in ("ERROR", "DELETED"):
            return last
        time.sleep(POLL_SECONDS)
    return last


def _wait_until_gone(probe: Probe, peering_id: str) -> bool:
    """Poll until the peering 404s. It has no terminal status -- see `11`."""
    if peering_id == DRY_ID:
        return True
    for attempt in range(POLL_ATTEMPTS):
        seen = probe.call(
            "GET",
            f"/vpc_peerings/{peering_id}",
            note=f"poll {attempt + 1}: is the peering gone",
            record=False,
        )
        if seen.status == 404:
            return True
        body = (seen.body or {}).get("vpc_peering") or {}
        print(f"     status={body.get('status')!r}")
        time.sleep(POLL_SECONDS)
    return False


def run(probe: Probe) -> None:
    # --- 01: what peerings exist before anything is done ---------------
    probe.call(
        "GET",
        "/vpc_peerings",
        note="peerings on the account before this session",
        predict={"status": 200, "notes": "expect an empty list on the dev team"},
    )

    # --- 02, 03: two VPCs to peer -------------------------------------
    name_a = unique_name(VPC_PREFIX + "-a")
    created_a = probe.call(
        "POST",
        "/vpcs",
        {"name": name_a, "region": REGION, "ip_range": RANGE_A},
        note="create VPC A",
        predict={"status": 201, "notes": "free; default=false"},
    )
    vpc_a = _created_id(created_a, "vpc", name_a)
    if created_a.status < 400:
        probe.cleanup("DELETE", f"/vpcs/{vpc_a}")

    name_b = unique_name(VPC_PREFIX + "-b")
    created_b = probe.call(
        "POST",
        "/vpcs",
        {"name": name_b, "region": REGION, "ip_range": RANGE_B},
        note="create VPC B",
        predict={"status": 201, "notes": "free; non-overlapping range with A"},
    )
    vpc_b = _created_id(created_b, "vpc", name_b)
    if created_b.status < 400:
        probe.cleanup("DELETE", f"/vpcs/{vpc_b}")

    # --- 04: must both targets pre-exist? -----------------------------
    refused = probe.call(
        "POST",
        "/vpc_peerings",
        {"name": unique_name(PEERING_PREFIX + "-ghost"), "vpc_ids": [vpc_a, NONEXISTENT_VPC]},
        note="peer VPC A with a VPC that does not exist",
        predict={
            "status": 404,
            "notes": "every probed DigitalOcean edge refuses an absent referent -- "
            "droplet.vpc_uuid 404s, firewall.droplet_ids 422s. A peering should too, and "
            "if it does not, a peering can be created half-dangling, which is a finding",
        },
    )
    if 0 < refused.status < 400:
        pid = (refused.body or {}).get("vpc_peering", {}).get("id")
        if pid:
            probe.cleanup("DELETE", f"/vpc_peerings/{pid}")
        print(
            "\n  *** a peering naming a nonexistent VPC was ACCEPTED. A peering can exist "
            "with a dangling endpoint, which no other edge in the driver set permits."
        )

    # --- 05: the peering ----------------------------------------------
    peer_name = unique_name(PEERING_PREFIX)
    created_peering = probe.call(
        "POST",
        "/vpc_peerings",
        {"name": peer_name, "vpc_ids": [vpc_a, vpc_b]},
        note="peer A with B",
        predict={
            "status": 202,
            "notes": "202 accepted, status PROVISIONING; 202 says nothing about success, "
            "so the poll below is what establishes it worked",
        },
    )
    peering_id = _created_id(created_peering, "vpc_peering", peer_name)
    if created_peering.status < 400:
        probe.cleanup("DELETE", f"/vpc_peerings/{peering_id}")

    final = _wait_for_status(probe, peering_id, "ACTIVE")
    print(f"\n  peering settled at: {final!r}")

    # --- 06: is vpc_ids returned in the order given? ------------------
    probe.call(
        "GET",
        f"/vpc_peerings/{peering_id}",
        note="the active peering, to compare vpc_ids order against what was sent",
        predict={
            "status": 200,
            "notes": f"sent [A, B] = [{vpc_a}, {vpc_b}]. If the response reorders them, a "
            "future driver needs vpc_ids in UNORDERED_FIELDS or every re-plan shows a "
            "phantom diff -- the same trap specs/unordered_fields.md exists for",
        },
    )

    # --- 07: can a VPC be asked what it is peered with? ---------------
    probe.call(
        "GET",
        f"/vpcs/{vpc_a}/peerings",
        note="is the relationship readable from the VPC side",
        predict={
            "status": 200,
            "notes": "if this answers, DigitalOcean supplies BOTH directions for this "
            "relationship -- unlike a firewall, where a droplet cannot be asked what "
            "protects it. That asymmetry is recorded in digitalocean_vpc's findings",
        },
    )

    # --- 08: THE ORDERING QUESTION ------------------------------------
    refused_delete = probe.call(
        "DELETE",
        f"/vpcs/{vpc_a}",
        note="delete a VPC that is currently peered",
        predict={
            "status": 409,
            "notes": "the question this session exists for. 409 would make a peering an "
            "existential dependent, like a member. A 2xx means the peering is left "
            "dangling or silently destroyed, and the model needs to say which",
        },
    )
    if 0 < refused_delete.status < 400:
        print(
            "\n  *** a PEERED VPC was deleted. What happened to the peering is the next "
            "question, and the call below answers it."
        )
        probe.call(
            "GET",
            f"/vpc_peerings/{peering_id}",
            note="after deleting one peer, does the peering survive",
            predict={
                "status": 200,
                "notes": "unknown -- cascade-deleted (404), left ACTIVE with a dead "
                "endpoint, or moved to an error status. Each is a different relationship",
            },
        )

    # --- 09: remove the peering, then the VPCs ------------------------
    probe.call(
        "DELETE",
        f"/vpc_peerings/{peering_id}",
        note="delete the peering",
        predict={"status": 202, "notes": "asynchronous, like the create"},
    )
    # Poll for 404, not for a terminal status. A deleted peering does not settle
    # into DELETED -- it disappears, and this session's first run spent 24 polls
    # learning that. A driver has to do the same.
    gone = _wait_until_gone(probe, peering_id)
    print(f"  peering gone: {gone}")

    # --- 10: and now the VPCs should go -------------------------------
    probe.call(
        "DELETE",
        f"/vpcs/{vpc_a}",
        note="delete VPC A now that nothing is peered with it",
        predict={
            "status": 204,
            "notes": "if step 08 was refused, this is the pair that shows the refusal was "
            "about the peering and not about the VPC",
        },
    )
    probe.call(
        "DELETE",
        f"/vpcs/{vpc_b}",
        note="delete VPC B",
        predict={"status": 204, "notes": "nothing references it"},
    )


def _age_minutes(created_at: str) -> float:
    created = datetime.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    return (datetime.datetime.now(datetime.UTC) - created).total_seconds() / 60


def sweep(probe: Probe) -> int:
    """Peerings first: this session's own finding may be that a VPC cannot go
    while one references it, so sweeping VPCs first would report a false leak."""
    leaked = 0
    for collection, key, prefix, label in (
        ("vpc_peerings", "vpc_peerings", PEERING_PREFIX, "peering"),
        ("vpcs", "vpcs", VPC_PREFIX, "vpc"),
    ):
        listed = probe.call(
            "GET", f"/{collection}?per_page=200", note=f"sweep: list {collection}", record=False
        )
        for item in (listed.body or {}).get(key, []):
            if not item["name"].startswith(prefix):
                continue
            age = _age_minutes(item["created_at"])
            if age < SWEEP_MIN_AGE_MINUTES:
                print(f"  skipping {item['name']} -- {age:.0f}m old, a live run may still own it")
                continue
            print(f"  LEAKED {label} {item['id']} {item['name']} ({age:.0f}m old)")
            leaked += 1
            if probe.mutate:
                probe.call(
                    "DELETE", f"/{collection}/{item['id']}", note="sweep: delete", record=False
                )
        if collection == "vpc_peerings" and leaked and probe.mutate:
            # A peering delete is asynchronous, and a VPC cannot go while one
            # references it. Give the deletes above time to land before the VPC
            # pass, or the sweep reports leaks it just caused.
            time.sleep(POLL_SECONDS * 3)
    return leaked


def main(argv=None) -> int:
    args = base_arg_parser(__doc__).parse_args(argv)
    with Probe(SESSION, mutate=args.mutate, dry_run=args.dry_run, audit=not args.sweep) as probe:
        if args.sweep:
            n = sweep(probe)
            print(f"\n{n} leftover(s)" + (" -- investigate" if n else ""))
            return 1 if n else 0
        run(probe)
        print(f"\n{probe._seq} probes; {len(probe.contradictions)} contradicted a prediction:")
        for line in probe.contradictions:
            print(f"  - {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
