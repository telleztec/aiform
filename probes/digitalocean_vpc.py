# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

"""Probe session for DigitalOcean VPCs, run to settle dependency edges.

No VPC driver exists and none is being written. This session exists because
`specs/resource_dependencies.md` is being given a real dependency model, and
every edge in the current driver set is the same kind -- firewall to droplet,
which survives its target and whose operational impact runs *opposite* to the
declared edge. A model validated against that one example is validated against
the example that misleads.

A VPC is the aligned case: a droplet needs its VPC's id AND cannot exist
without it. Probing it is what makes "the dependent cannot survive its target"
a verified property rather than an owner-reported one.

It answers four questions the documentation does not settle:

  1. Must a VPC pre-exist before a droplet can name it? (an implied edge)
  2. Does DigitalOcean refuse to delete a VPC that still has members, and
     with what status and message? The "empty requirement" is owner-reported
     and drives whether aiform's own refusal is worth anything.
  3. Does VPC membership clear by itself when a member is destroyed?
  4. And the same question for a firewall's droplet_ids, which
     specs/digitalocean_firewall.md:323 names as "not yet probed" and which
     decides whether #227 has anything to repair.

Question 4 rides along deliberately: it needs a droplet, this session already
pays for one, and asking it separately would pay twice.

DELIBERATELY NOT PROBED: whether a *default* VPC can be deleted. A passing
result there is a destroyed region default, which is not a finding worth the
blast radius. Recorded as unprobed.

Costs: one droplet, the cheapest DigitalOcean sells, for roughly four
minutes. VPCs and firewalls are free. The droplet is never logged into and
generates no traffic; it exists to be a VPC member and a firewall target.

Run:  python probes/digitalocean_vpc.py --dry-run
      python probes/digitalocean_vpc.py --mutate
      python probes/digitalocean_vpc.py --sweep --mutate
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

SESSION = "digitalocean_vpc"
REGION = "sfo3"
VPC_PREFIX = "aiform-system-test-vpc-probe"
FW_PREFIX = "aiform-system-test-fw-vpcprobe"
# Shares tests/system/conftest.py's SYSTEM_TEST_DROPLET_PREFIX stem
# deliberately: that sweep is the backstop for a droplet this session leaks,
# and it matches on the prefix.
DROPLET_PREFIX = "aiform-system-test-vpcdrop-probe"

# Outside both region defaults observed at recon time (10.108.0.0/20 nyc3,
# 10.124.0.0/20 sfo3), so the create cannot fail for overlap and leave the
# session unable to distinguish "overlap" from "rejected".
PROBE_IP_RANGE = "10.200.0.0/24"

NONEXISTENT_VPC = "00000000-0000-0000-0000-000000000000"

DROPLET_BODY = {
    "region": REGION,
    "size": "s-1vcpu-512mb-10gb",
    "image": "ubuntu-24-04-x64",
    "backups": False,
    "monitoring": False,
    "tags": ["aiform-system-test"],
}

RULE = {"protocol": "tcp", "ports": "22", "sources": {"addresses": ["0.0.0.0/0"]}}

# A droplet delete is asynchronous. Poll rather than sleep a fixed span, so a
# slow delete is visible as a timeout finding instead of silently making the
# two "does membership clear" answers meaningless.
DELETE_POLL_SECONDS = 10
DELETE_POLL_ATTEMPTS = 18

DRY_ID = "<dry-run-id>"


def _created_id(result, key, name=None):
    """The id a create returned, or a placeholder under --dry-run."""
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


def _members_of(result):
    if not isinstance(result.body, dict):
        return "<dry-run>"
    return result.body.get("members", [])


def _wait_until_droplet_gone(probe: Probe, droplet_id: str) -> bool:
    """Poll until the droplet 404s. Returns whether it actually went."""
    if droplet_id == DRY_ID:
        return True
    for attempt in range(DELETE_POLL_ATTEMPTS):
        seen = probe.call(
            "GET",
            f"/droplets/{droplet_id}",
            note=f"poll {attempt + 1}: is the droplet gone yet",
            record=False,
        )
        if seen.status == 404:
            return True
        time.sleep(DELETE_POLL_SECONDS)
    return False


def run(probe: Probe) -> None:
    # --- 01: does a droplet's vpc_uuid have to pre-exist? --------------
    # The cheapest possible form of the question: a create that should be
    # refused costs nothing if it is refused, and the session aborts loudly
    # if it is not, because an accepted droplet here is a live untracked one.
    refused = probe.call(
        "POST",
        "/droplets",
        {"name": unique_name(DROPLET_PREFIX), "vpc_uuid": NONEXISTENT_VPC, **DROPLET_BODY},
        note="create a droplet naming a VPC that does not exist",
        predict={
            "status": 404,
            "notes": "a droplet's vpc_uuid must pre-exist, so this is an implied edge "
            "of the same shape as firewall.droplet_ids -- refused, not auto-created",
        },
    )
    # status 0 is --dry-run, which sent nothing. Only a real 2xx is the
    # alarming case: a droplet that was actually accepted here is live with no
    # cleanup registered, because the call was written expecting a refusal.
    if 0 < refused.status < 400:
        raise SystemExit(
            "a droplet naming a nonexistent VPC was ACCEPTED -- it is live and untracked, "
            "and this session registered no cleanup for it. Destroy it by hand NOW."
        )

    # --- 02: create the probe VPC -------------------------------------
    vpc_name = unique_name(VPC_PREFIX)
    created_vpc = probe.call(
        "POST",
        "/vpcs",
        {"name": vpc_name, "region": REGION, "ip_range": PROBE_IP_RANGE},
        note="create a non-default VPC to put a droplet in",
        predict={"status": 201, "notes": "free; default=false since the region already has one"},
    )
    vpc_id = _created_id(created_vpc, "vpc", vpc_name)
    if created_vpc.status < 400:
        probe.cleanup("DELETE", f"/vpcs/{vpc_id}")

    # --- 03: it starts empty ------------------------------------------
    probe.call(
        "GET",
        f"/vpcs/{vpc_id}/members",
        note="members of a brand-new VPC",
        predict={"status": 200, "notes": "empty list"},
    )

    # --- 04: a droplet inside it --------------------------------------
    droplet_name = unique_name(DROPLET_PREFIX)
    created_droplet = probe.call(
        "POST",
        "/droplets",
        {"name": droplet_name, "vpc_uuid": vpc_id, **DROPLET_BODY},
        note="create the droplet this session puts inside the probe VPC",
        predict={"status": 202, "notes": "billable from this moment"},
    )
    droplet_id = _created_id(created_droplet, "droplet", droplet_name)
    if created_droplet.status < 400:
        probe.cleanup("DELETE", f"/droplets/{droplet_id}")

    # --- 05: the droplet reports its own VPC --------------------------
    probe.call(
        "GET",
        f"/droplets/{droplet_id}",
        note="does the droplet record name the VPC it is in",
        predict={
            "status": 200,
            "notes": "vpc_uuid should equal the probe VPC -- if a driver is to declare "
            "this edge, the attribute has to be readable from the dependent's own record",
        },
    )

    # --- 06: membership is visible from the VPC side -------------------
    # This is the shape a blast-radius query would use, and it is the
    # opposite arrangement from a firewall, where membership lives on the
    # firewall rather than on the droplet.
    probe.call(
        "GET",
        f"/vpcs/{vpc_id}/members",
        note="does the VPC list the droplet as a member",
        predict={
            "status": 200,
            "notes": "the droplet appears -- the provider answers 'what depends on me' "
            "natively here, unlike a firewall",
        },
    )

    # --- 07: THE EMPTY REQUIREMENT ------------------------------------
    probe.call(
        "DELETE",
        f"/vpcs/{vpc_id}",
        note="delete the VPC while its member is alive",
        predict={
            "status": 403,
            "notes": "owner-reported as refused ('a VPC can only be deleted if it has no "
            "member resources attached'). The status and message are what decide whether "
            "aiform can pre-empt this usefully at plan time",
        },
    )

    # --- 08: a firewall pointing at the same droplet -------------------
    fw_name = unique_name(FW_PREFIX)
    created_fw = probe.call(
        "POST",
        "/firewalls",
        {"name": fw_name, "inbound_rules": [RULE], "droplet_ids": [droplet_id]},
        note="attach a firewall, so one droplet answers the droplet_ids question too",
        predict={"status": 202, "notes": "free"},
    )
    fw_id = _created_id(created_fw, "firewall", fw_name)
    if created_fw.status < 400:
        probe.cleanup("DELETE", f"/firewalls/{fw_id}")

    # --- 09: destroy the member ---------------------------------------
    probe.call(
        "DELETE",
        f"/droplets/{droplet_id}",
        note="destroy the droplet that both the VPC and the firewall point at",
        predict={"status": 204, "notes": "asynchronous; the next calls poll for the far side"},
    )
    gone = _wait_until_droplet_gone(probe, droplet_id)
    if not gone:
        print(
            "  !! droplet still present after "
            f"{DELETE_POLL_ATTEMPTS * DELETE_POLL_SECONDS}s -- the two 'does membership "
            "clear' answers below are inconclusive, not negative"
        )

    # --- 10: does VPC membership clear itself? ------------------------
    probe.call(
        "GET",
        f"/vpcs/{vpc_id}/members",
        note="after the member is destroyed, does the VPC still list it",
        predict={
            "status": 200,
            "notes": "expected empty -- membership is derived from live resources rather "
            "than stored on the VPC, so there should be nothing to go stale",
        },
    )

    # --- 11: does a firewall's droplet_ids clear itself? ---------------
    # specs/digitalocean_firewall.md:323 names this as not yet probed, and
    # #227 turns on the answer: if DigitalOcean drops the dead id itself,
    # there is nothing for aiform to repair.
    probe.call(
        "GET",
        f"/firewalls/{fw_id}",
        note="after the droplet is destroyed, does droplet_ids still carry its id",
        predict={
            "status": 200,
            "notes": "unknown -- this is the unprobed question. Either droplet_ids is now "
            "empty (DigitalOcean self-heals, #227 has nothing to repair) or it still "
            "carries a dead id (aiform must repair it)",
        },
    )

    # --- 12: and now the VPC deletes ----------------------------------
    probe.call(
        "DELETE",
        f"/vpcs/{vpc_id}",
        note="delete the VPC now that it is empty",
        predict={
            "status": 204,
            "notes": "the same call that was refused at step 07, to show the refusal was "
            "about membership and not about the VPC itself",
        },
    )


def _age_minutes(created_at: str) -> float:
    created = datetime.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    return (datetime.datetime.now(datetime.UTC) - created).total_seconds() / 60


def sweep(probe: Probe) -> int:
    """Leftovers from a crashed run. Droplets first: they bill.

    VPCs last, because a VPC whose member leaked cannot be deleted until the
    droplet is -- that is the very property this session probes, and a sweep
    that tried the VPC first would report a false leak for it.
    """
    leaked = 0
    for collection, prefix, label in (
        ("droplets", DROPLET_PREFIX, "DROPLET (billing)"),
        ("firewalls", FW_PREFIX, "firewall"),
        ("vpcs", VPC_PREFIX, "vpc"),
    ):
        listed = probe.call(
            "GET", f"/{collection}?per_page=200", note=f"sweep: list {collection}", record=False
        )
        for item in (listed.body or {}).get(collection, []):
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
