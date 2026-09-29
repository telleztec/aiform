# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

"""Where does a droplet go when nobody says which VPC?

One question, and it is the one the other two VPC sessions cannot answer.
`digitalocean_vpc.py` and `digitalocean_vpc_member.py` both created their
droplets with an explicit `vpc_uuid`, because they were probing containment --
so neither shows what happens when the field is omitted. And every droplet in
the whole transcript archive was read at `status: "new"`, where `vpc_uuid` is
absent, except one on an account this token can no longer see.

That matters because `drivers/digitalocean/compute.py` never sends `vpc_uuid`
at all: it is not in PARAM_SCHEMA and not in the create body. So **every
droplet aiform has ever created** took whatever default DigitalOcean applies,
and specs/resource_dependencies.md calls that an edge to an INTRINSIC resource:
one the provider creates on the user's behalf, that no configuration requested
and that cannot be deleted. A dependency neither declared nor referenced.

Before this session ran, the spec graded "aiform's droplets sit in the region
default" as INFERRED -- from DigitalOcean's "all applicable resources are placed
into the default VPC network unless otherwise specified" plus the absence of the
field in our code. This session is what makes it verified.

This settles it: create a droplet the way aiform does -- no `vpc_uuid` --
wait for it to converge, and read where it went.

It also narrows a second open question. `knowledge/drivers/digitalocean_vpc/`
records the ORIGIN of the account's two `default: true` VPCs as unknown, after
an earlier guess was refuted. If a droplet created with no VPC lands in
`default-sfo3`, that confirms the default is the sink for unspecified
resources, which is the half of the origin story that is testable without
creating a resource in a region that has no VPC at all.

Costs: one droplet, the cheapest DigitalOcean sells, about three minutes. No
VPC is created -- this session only reads them.

Run:  python probes/digitalocean_vpc_default.py --dry-run
      python probes/digitalocean_vpc_default.py --mutate
      python probes/digitalocean_vpc_default.py --sweep --mutate
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

SESSION = "digitalocean_vpc_default"
REGION = "sfo3"
DROPLET_PREFIX = "aiform-system-test-vpcdef-probe"

# Exactly what drivers/digitalocean/compute.py's create() sends: name, region,
# size, image, plus the optional flags. No vpc_uuid, deliberately -- the point
# is to reproduce aiform's own request rather than a hand-tuned one.
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


def run(probe: Probe) -> None:
    # --- 01: which VPCs exist, and which is this region's default? -----
    listed = probe.call(
        "GET",
        "/vpcs?per_page=50",
        note="the account's VPCs before anything is created",
        predict={
            "status": 200,
            "notes": "expect the two default:true VPCs whose origin knowledge/ records "
            "as unknown; this run does not create or delete any of them",
        },
    )
    default_id = None
    if isinstance(listed.body, dict):
        for vpc in listed.body.get("vpcs", []):
            if vpc.get("region") == REGION and vpc.get("default"):
                default_id = vpc["id"]
                print(f"     region default for {REGION}: {default_id} ({vpc.get('name')!r})")

    # --- 02: a droplet the way aiform makes one, with no vpc_uuid ------
    droplet_name = unique_name(DROPLET_PREFIX)
    created = probe.call(
        "POST",
        "/droplets",
        {"name": droplet_name, **DROPLET_BODY},
        note="create a droplet with NO vpc_uuid, exactly as compute.py does",
        predict={
            "status": 202,
            "notes": "accepted; vpc_uuid absent from the response while status is new",
        },
    )
    droplet_id = _created_id(created, "droplet", droplet_name)
    if created.status < 400:
        probe.cleanup("DELETE", f"/droplets/{droplet_id}")

    # --- wait for convergence, since vpc_uuid is absent until then -----
    landed = None
    if droplet_id != DRY_ID:
        for attempt in range(POLL_ATTEMPTS):
            seen = probe.call(
                "GET",
                f"/droplets/{droplet_id}",
                note=f"poll {attempt + 1}: status and vpc_uuid",
                record=False,
            )
            droplet = (seen.body or {}).get("droplet") or {}
            status = droplet.get("status")
            landed = droplet.get("vpc_uuid")
            print(f"     status={status!r} vpc_uuid={landed!r}")
            if status == "active" and landed:
                break
            time.sleep(POLL_SECONDS)

    # --- 03: the answer, recorded ------------------------------------
    probe.call(
        "GET",
        f"/droplets/{droplet_id}",
        note="where did a droplet with no vpc_uuid actually land",
        predict={
            "status": 200,
            "notes": f"vpc_uuid expected to equal this region's default:true VPC "
            f"({default_id}), which would make 'aiform's droplets sit in the region "
            "default' verified rather than inferred from documentation",
        },
    )

    if default_id and landed:
        verdict = "MATCHES the region default" if landed == default_id else "does NOT match"
        print(f"\n  *** droplet landed in {landed} -- {verdict} ({default_id})")
    elif droplet_id != DRY_ID:
        print(
            "\n  !! never saw a vpc_uuid before the poll budget ran out; the question is "
            "unanswered rather than answered negatively"
        )

    # --- 04: membership from the other end ---------------------------
    if default_id:
        probe.call(
            "GET",
            f"/vpcs/{default_id}/members",
            note="does the region default list the droplet as a member",
            predict={
                "status": 200,
                "notes": "the droplet should appear -- the reverse-edge query for an edge "
                "nobody declared and nothing in aiform records",
            },
        )

    # --- 05: tear it down --------------------------------------------
    probe.call(
        "DELETE",
        f"/droplets/{droplet_id}",
        note="destroy the droplet",
        predict={"status": 204, "notes": "asynchronous"},
    )


def _age_minutes(created_at: str) -> float:
    created = datetime.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    return (datetime.datetime.now(datetime.UTC) - created).total_seconds() / 60


def sweep(probe: Probe) -> int:
    """Droplets only. This session creates no VPC, so it can leak none."""
    leaked = 0
    listed = probe.call("GET", "/droplets?per_page=200", note="sweep: list droplets", record=False)
    for item in (listed.body or {}).get("droplets", []):
        if not item["name"].startswith(DROPLET_PREFIX):
            continue
        age = _age_minutes(item["created_at"])
        if age < SWEEP_MIN_AGE_MINUTES:
            print(f"  skipping {item['name']} -- {age:.0f}m old, a live run may still own it")
            continue
        print(f"  LEAKED DROPLET (billing) {item['id']} {item['name']} ({age:.0f}m old)")
        leaked += 1
        if probe.mutate:
            probe.call("DELETE", f"/droplets/{item['id']}", note="sweep: delete", record=False)
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
