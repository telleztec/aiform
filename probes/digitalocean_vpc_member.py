# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

"""Probe session for the DigitalOcean VPC "empty requirement", done properly.

Separate from digitalocean_vpc.py rather than appended to it, following the
precedent digitalocean_firewall_attach.py sets: a re-run restarts the sequence
at 01 and would overwrite that session's transcripts, and those transcripts
carry a finding worth keeping -- that a droplet's record has no `vpc_uuid` key
at all immediately after creation.

That finding is exactly why this session exists. digitalocean_vpc.py's step 07
deleted a VPC while a droplet it had just created was notionally inside it, and
got 204. That looked like a refutation of DigitalOcean's documented rule ("a VPC
can only be deleted if it does not contain any member resources"). It was not:
step 06 showed `members: []` and step 05 showed no `vpc_uuid` key at all on a
droplet still in status "new". The VPC was empty as far as DigitalOcean was concerned, so the
delete tells us nothing about a VPC that is not.

This session waits for the droplet to reach "active" and for the VPC's member
list to actually show it, and only then attempts the delete. If the delete
still succeeds, the rule is genuinely wrong and the dependency model should not
be built on it. If it is refused, the model gets its first edge whose parent the
provider will not release -- note the narrower claim: that is the refusal, not
evidence a droplet breaks without its VPC, which the provider prevents anyone
from observing.

The rule itself is DigitalOcean's own documentation, not a guess: vpcs_delete
says a VPC "can only be deleted if it does not contain any member resources",
and promises a 403. Whether the documented *status* is right is the part a probe
can actually settle.

Costs: one droplet, the cheapest DigitalOcean sells, for roughly five minutes
-- longer than digitalocean_vpc.py because this one waits for convergence
rather than racing it. VPCs are free.

The registered teardown cannot fully honour that ordering, and this is a known
limit rather than an oversight. `Probe.__exit__` fires cleanups in reverse
registration order -- droplet, then VPC -- but sends each once with no wait, and
a droplet DELETE is accepted asynchronously. So on a crash path between the
droplet create and this session's own final VPC delete, the cleanup VPC DELETE
will 409 and the VPC is left behind. Fixing that properly means teaching the
shared harness to wait, which would change every existing session's teardown, so
it is out of scope here. `--sweep` is the backstop and does wait; a VPC costs
nothing while it waits to be swept.

Step comments below name the TRANSCRIPT number, not the call ordinal: the
convergence and teardown polls are `record=False` and consume sequence numbers
without writing a file, so the recorded set is 01, 02, 09, 10, 11, 14.

Run:  python probes/digitalocean_vpc_member.py --dry-run
      python probes/digitalocean_vpc_member.py --mutate
      python probes/digitalocean_vpc_member.py --sweep --mutate
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

SESSION = "digitalocean_vpc_member"
REGION = "sfo3"
VPC_PREFIX = "aiform-system-test-vpc-member"
DROPLET_PREFIX = "aiform-system-test-vpcmem-probe"

PROBE_IP_RANGE = "10.201.0.0/24"

DROPLET_BODY = {
    "region": REGION,
    "size": "s-1vcpu-512mb-10gb",
    "image": "ubuntu-24-04-x64",
    "backups": False,
    "monitoring": False,
    "tags": ["aiform-system-test"],
}

POLL_SECONDS = 15
POLL_ATTEMPTS = 20

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


def _wait_for_membership(probe: Probe, vpc_id: str, droplet_id: str) -> str:
    """Poll until the droplet is active AND the VPC lists it.

    Returns a short verdict string. Both conditions matter: digitalocean_vpc.py
    proved the droplet's own vpc_uuid lags its creation, and a member list that
    has not caught up makes the delete attempt meaningless.
    """
    if droplet_id == DRY_ID:
        return "dry-run"
    for attempt in range(POLL_ATTEMPTS):
        seen = probe.call(
            "GET",
            f"/droplets/{droplet_id}",
            note=f"poll {attempt + 1}: droplet status and vpc_uuid",
            record=False,
        )
        droplet = (seen.body or {}).get("droplet") or {}
        status = droplet.get("status")
        vpc_uuid = droplet.get("vpc_uuid")

        listed = probe.call(
            "GET",
            f"/vpcs/{vpc_id}/members",
            note=f"poll {attempt + 1}: VPC member list",
            record=False,
        )
        members = (listed.body or {}).get("members") or []
        print(f"     status={status!r} vpc_uuid={vpc_uuid!r} members={len(members)}")

        if status == "active" and vpc_uuid == vpc_id and members:
            return "converged"
        time.sleep(POLL_SECONDS)
    return "timeout"


def run(probe: Probe) -> None:
    # --- 01: the VPC ---------------------------------------------------
    vpc_name = unique_name(VPC_PREFIX)
    created_vpc = probe.call(
        "POST",
        "/vpcs",
        {"name": vpc_name, "region": REGION, "ip_range": PROBE_IP_RANGE},
        note="create a non-default VPC",
        predict={"status": 201, "notes": "free; default=false"},
    )
    vpc_id = _created_id(created_vpc, "vpc", vpc_name)
    if created_vpc.status < 400:
        probe.cleanup("DELETE", f"/vpcs/{vpc_id}")

    # --- 02: a droplet inside it ---------------------------------------
    droplet_name = unique_name(DROPLET_PREFIX)
    created_droplet = probe.call(
        "POST",
        "/droplets",
        {"name": droplet_name, "vpc_uuid": vpc_id, **DROPLET_BODY},
        note="create a droplet inside the VPC",
        predict={"status": 202, "notes": "billable; status 'new', vpc_uuid not yet reported"},
    )
    droplet_id = _created_id(created_droplet, "droplet", droplet_name)
    if created_droplet.status < 400:
        probe.cleanup("DELETE", f"/droplets/{droplet_id}")

    # --- wait for the edge to become real ------------------------------
    verdict = _wait_for_membership(probe, vpc_id, droplet_id)
    print(f"\n  convergence: {verdict}")
    if verdict == "timeout":
        print(
            "  !! never saw droplet active AND vpc_uuid set AND a non-empty member list. "
            "The delete below is inconclusive for the same reason digitalocean_vpc.py's was; "
            "record it as such rather than as a refutation."
        )

    # --- 09: membership, now that it has settled -----------------------
    probe.call(
        "GET",
        f"/vpcs/{vpc_id}/members",
        note="the VPC's member list once the droplet is active",
        predict={
            "status": 200,
            "notes": "one member, the droplet -- this is the reverse-edge query a "
            "blast-radius report would use, answered natively by the provider",
        },
    )

    # --- 10: THE EMPTY REQUIREMENT, for real ---------------------------
    refused = probe.call(
        "DELETE",
        f"/vpcs/{vpc_id}",
        note="delete the VPC while a CONVERGED member is alive",
        predict={
            "status": 403,
            "notes": "403 is what DigitalOcean's own vpcs_delete description promises. "
            "The first run observed 409 instead, so this prediction is kept as the "
            "documented value deliberately: a re-run that sees 409 again records the "
            "documented-vs-observed gap rather than hiding it behind a corrected guess",
        },
    )
    if 0 < refused.status < 400:
        print(
            "\n  *** THE EMPTY REQUIREMENT DOES NOT HOLD: the VPC was deleted with a live, "
            "converged member. Whatever happened to that droplet's networking is the next "
            "question, and the dependency model cannot treat containment as a refusal."
        )
        # The VPC is gone; the droplet is not. Ask what it thinks it is in now.
        probe.call(
            "GET",
            f"/droplets/{droplet_id}",
            note="what does the droplet report as its VPC after that VPC was deleted",
            predict={
                "status": 200,
                "notes": "unknown -- a dangling vpc_uuid, a silent move to the region "
                "default, or something else. Whichever it is, it is a finding",
            },
        )

    # --- 11: tear the member down --------------------------------------
    probe.call(
        "DELETE",
        f"/droplets/{droplet_id}",
        note="destroy the droplet",
        predict={"status": 204, "notes": "asynchronous"},
    )
    # Guarded on DRY_ID: a --dry-run call returns status 0, which never
    # equals 404, so an unguarded loop sleeps the full budget for nothing.
    if droplet_id != DRY_ID:
        for attempt in range(POLL_ATTEMPTS):
            seen = probe.call(
                "GET", f"/droplets/{droplet_id}", note=f"poll {attempt + 1}: gone yet", record=False
            )
            if seen.status == 404:
                break
            time.sleep(POLL_SECONDS)

    # --- 14: and now the VPC should go ---------------------------------
    probe.call(
        "DELETE",
        f"/vpcs/{vpc_id}",
        note="delete the VPC now that it is genuinely empty",
        predict={
            "status": 204,
            "notes": "if step 10 was refused, this is the pair that shows the refusal was "
            "about membership. If step 04 succeeded, this will 404 and says nothing",
        },
    )


def _wait_for_droplets_gone(probe: Probe, ids: list[str]) -> None:
    """Block until every id 404s, so the VPC sweep below is not refused.

    A droplet DELETE is accepted asynchronously, and this session's own finding
    is that a VPC cannot be deleted while a member is alive. Sweeping vpcs
    immediately after droplets would therefore 409 on exactly the leak it was
    called to clean, and only succeed on some later run.
    """
    for droplet_id in ids:
        for _ in range(POLL_ATTEMPTS):
            seen = probe.call(
                "GET", f"/droplets/{droplet_id}", note="sweep: wait for gone", record=False
            )
            if seen.status == 404:
                break
            time.sleep(POLL_SECONDS)


def _age_minutes(created_at: str) -> float:
    created = datetime.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    return (datetime.datetime.now(datetime.UTC) - created).total_seconds() / 60


def sweep(probe: Probe) -> int:
    """Droplets first: they bill, and a VPC cannot go while one is inside it."""
    leaked = 0
    deleted_droplets: list[str] = []
    for collection, prefix, label in (
        ("droplets", DROPLET_PREFIX, "DROPLET (billing)"),
        ("vpcs", VPC_PREFIX, "vpc"),
    ):
        if collection == "vpcs" and deleted_droplets and probe.mutate:
            _wait_for_droplets_gone(probe, deleted_droplets)
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
                if collection == "droplets":
                    deleted_droplets.append(item["id"])
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
