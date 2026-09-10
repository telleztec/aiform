# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

"""Probe session for an ATTACHED DigitalOcean firewall.

Separate from digitalocean_firewall.py, which is deliberately free: every
firewall there is unattached, and its 31 transcripts are cited by number
throughout specs/digitalocean_firewall.md, so appending to that session
would renumber evidence other documents point at.

This session is the one that costs money. It creates the cheapest droplet
DigitalOcean sells for about two minutes, because the question it exists
to answer cannot be asked without one: specs/digitalocean_firewall.md
carried `waiting -> succeeded` and `pending_changes` as *recalled, not
verified*, and tests/system/test_cli_firewall.py verifies only the
consequence -- that a re-plan converges whatever DO is doing mid-flight
-- not the transition itself.

It generates no traffic. The droplet is never logged into and nothing is
ever sent to it; it exists to be an integer DigitalOcean will accept in
droplet_ids. Whether a rule is *enforced* is a different question and
needs a different instrument -- see specs/digitalocean_firewall.md's
Open questions.

Run:  python probes/digitalocean_firewall_attach.py --dry-run
      python probes/digitalocean_firewall_attach.py --mutate
      python probes/digitalocean_firewall_attach.py --sweep --mutate
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

SESSION = "digitalocean_firewall_attach"
FW_PREFIX = "aiform-system-test-fw-attach"
DROPLET_PREFIX = "aiform-system-test-drop-probe"
RULE = {"protocol": "tcp", "ports": "22", "sources": {"addresses": ["0.0.0.0/0"]}}

# The cheapest thing DigitalOcean sells, billed hourly.
DROPLET_BODY = {
    "region": "sfo3",
    "size": "s-1vcpu-512mb-10gb",
    "image": "ubuntu-24-04-x64",
    "backups": False,
    "monitoring": False,
}

# DO applies a firewall asynchronously. Long enough to see the far side
# of the transition without turning a probe session into a poll loop; if
# it is still `waiting` after this, that is itself the finding.
SETTLE_SECONDS = 20

DRY_ID = "<dry-run-id>"


def _created_id(result, key):
    """The id a create returned, or a placeholder under --dry-run.

    One place rather than at each call site: --dry-run sends nothing, so
    every later call in the session has to name something, and three
    slightly different reconstructions of that fact is how one of them
    ends up guarding `< 400` differently from the others.
    """
    if result is None:
        return DRY_ID
    if result.status >= 400:
        raise SystemExit(f"setup call failed with {result.status}; aborting session")
    if not isinstance(result.body, dict):
        return DRY_ID
    return result.body[key]["id"]


def _status_of(result):
    if result is None or not isinstance(result.body, dict):
        return "<dry-run>", "<dry-run>"
    firewall = result.body.get("firewall") or {}
    return firewall.get("status"), firewall.get("pending_changes")


def run(probe: Probe) -> None:
    # --- 01: a droplet to attach to -----------------------------------
    droplet_name = unique_name(DROPLET_PREFIX)
    created = probe.call(
        "POST",
        "/droplets",
        {"name": droplet_name, "tags": ["aiform-system-test"], **DROPLET_BODY},
        note="create the throwaway droplet this session attaches to",
        predict={
            "status": 202,
            "notes": "202 with droplet.status='new'; it is billable from this moment",
        },
    )
    droplet_id = _created_id(created, "droplet")
    if created is not None and created.status < 400:
        probe.cleanup("DELETE", f"/droplets/{droplet_id}")

    # --- 02: the question this session exists for ---------------------
    fw_name = unique_name(FW_PREFIX)
    attached = probe.call(
        "POST",
        "/firewalls",
        {
            "name": fw_name,
            "inbound_rules": [dict(RULE)],
            "outbound_rules": [],
            "droplet_ids": [droplet_id],
            "tags": [],
        },
        note="create a firewall ALREADY attached to a real droplet",
        predict={
            "status": 202,
            # The recalled claim, written down before sending so a match
            # is worth as much as a miss.
            "notes": (
                "status='waiting' and pending_changes carrying one entry for the droplet -- "
                "this is what specs/digitalocean_firewall.md recorded as recalled, not verified. "
                "The unattached case (session digitalocean_firewall, probe 02) returned "
                "'succeeded' immediately"
            ),
        },
    )
    fw_id = _created_id(attached, "firewall")
    if attached is not None and attached.status < 400:
        probe.cleanup("DELETE", f"/firewalls/{fw_id}")
    status, pending = _status_of(attached)
    print(f"  [attach] status={status!r} pending_changes={pending!r}")

    # --- 03: is it still waiting a moment later? ----------------------
    probe.call(
        "GET",
        f"/firewalls/{fw_id}",
        note="read back immediately after attaching",
        predict={
            "status": 200,
            "notes": "still status='waiting' if the transition is observable at all",
        },
    )

    # --- 04: the far side of the transition ---------------------------
    if not probe.dry_run:
        time.sleep(SETTLE_SECONDS)
    settled = probe.call(
        "GET",
        f"/firewalls/{fw_id}",
        note=f"read back {SETTLE_SECONDS}s after attaching",
        predict={
            "status": 200,
            "notes": "status='succeeded' and pending_changes empty",
        },
    )
    status, pending = _status_of(settled)
    print(f"  [settled] status={status!r} pending_changes={pending!r}")

    # --- 05: detach through the whole-object PUT ----------------------
    detached = probe.call(
        "PUT",
        f"/firewalls/{fw_id}",
        {
            "name": fw_name,
            "inbound_rules": [dict(RULE)],
            "outbound_rules": [],
            "droplet_ids": [],
            "tags": [],
        },
        note="detach by omitting the droplet from droplet_ids",
        predict={
            "status": 200,
            "notes": "status='waiting' again while DO removes the rules from the droplet",
        },
    )
    status, pending = _status_of(detached)
    print(f"  [detach] status={status!r} pending_changes={pending!r}")

    # --- 06: attach an EXISTING firewall, the update() path ------------
    reattached = probe.call(
        "PUT",
        f"/firewalls/{fw_id}",
        {
            "name": fw_name,
            "inbound_rules": [dict(RULE)],
            "outbound_rules": [],
            "droplet_ids": [droplet_id],
            "tags": [],
        },
        note="re-attach an existing firewall, which is what update() does",
        predict={
            "status": 200,
            "notes": "status='waiting'; the create path and the update path should agree",
        },
    )
    status, pending = _status_of(reattached)
    print(f"  [reattach] status={status!r} pending_changes={pending!r}")

    if not probe.dry_run:
        time.sleep(SETTLE_SECONDS)
    final = probe.call(
        "GET",
        f"/firewalls/{fw_id}",
        note=f"read back {SETTLE_SECONDS}s after re-attaching",
        predict={"status": 200, "notes": "status='succeeded', pending_changes empty"},
    )
    status, pending = _status_of(final)
    print(f"  [reattach settled] status={status!r} pending_changes={pending!r}")


def _age_minutes(created_at: str) -> float:
    created = datetime.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    return (datetime.datetime.now(datetime.UTC) - created).total_seconds() / 60


def sweep(probe: Probe) -> int:
    """Leftovers from a crashed run. Droplets first: they bill."""
    leaked = 0
    for collection, prefix, label in (
        ("droplets", DROPLET_PREFIX, "DROPLET (billing)"),
        ("firewalls", FW_PREFIX, "firewall"),
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
