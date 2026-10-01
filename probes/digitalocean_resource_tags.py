# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

"""Probe session for DigitalOcean tags, run to settle #249.

#249 writes the deployment name and an aiform marker onto every provider
resource. A droplet can plainly carry a DigitalOcean tag; this session asks
whether the other two resource kinds aiform manages can, and how tags behave.

  1. Does a tag name containing ':' work, as a path segment and in a body?
  2. Is a tag name case-folded on store? (Decides whether a reserved-name check
     must be case-insensitive.)
  3. Can a firewall carry a tag, or is a firewall's own `tags` field only a
     selector for droplets? Can a domain carry one?
  4. If a firewall cannot, what does a firewall name accept, so the deployment
     name can ride in it instead?
  5. Does a second TXT record at a zone apex disturb the first one's TTL, and does
     a TXT value containing ':' round-trip verbatim?

DELIBERATELY NOT PROBED: tag behavior on droplet create and on droplet delete.
Both need a droplet, which bills, and the owner ruled billable resources out of
this session. Droplet tag auto-creation is therefore recorded as documented,
and what happens to a tag when its tagged resource is deleted is recorded as
NOT observed -- only that a tag with zero resources persists until deleted is
observed here.

Costs nothing: tags, firewalls and DNS zones are free. No droplet id is ever
real -- the attach calls use ids that cannot exist.

Run:  python probes/digitalocean_resource_tags.py --dry-run
      python probes/digitalocean_resource_tags.py --mutate
      python probes/digitalocean_resource_tags.py --sweep --mutate
"""

import datetime
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _probe import (  # noqa: E402
    SWEEP_MIN_AGE_MINUTES,
    Probe,
    base_arg_parser,
    unique_name,
)

SESSION = "digitalocean_resource_tags"
FW_PREFIX = "aiform-system-test-fw-tagprobe"
TAG_PREFIX = "aiform:tagprobe-"
# The system suite's own zone namespace, so a leaked zone is also caught by
# tests/system/conftest.py's sweep. The token cannot create zones under the
# telleztec.com parent (see SYSTEM_TEST_ZONE_PARENT there).
ZONE_PARENT = "cloudaiform.com"
ZONE_PREFIX = "systest-"
ZONE_TIMESTAMP_FORMAT = "%Y%m%dt%H%M%Sz"

RULE = {"protocol": "tcp", "ports": "22", "sources": {"addresses": ["0.0.0.0/0"]}}

# Neither can exist as a droplet: the attach calls must fail on the tag or the
# resource, never succeed.
NONEXISTENT_DROPLET = "1"

DRY_ID = "<dry-run-id>"


def _zone_name() -> str:
    now = datetime.datetime.now(datetime.UTC)
    return (
        f"{ZONE_PREFIX}{now:{ZONE_TIMESTAMP_FORMAT}}-{secrets.token_hex(3)}-tagprobe.{ZONE_PARENT}"
    )


def _firewall_id(result):
    if not isinstance(result.body, dict):
        return DRY_ID
    return result.body["firewall"]["id"]


def run(probe: Probe) -> None:
    tag = f"{TAG_PREFIX}{secrets.token_hex(3)}"

    # --- 01-02: a tag name with ':' ------------------------------------
    created = probe.call(
        "POST",
        "/tags",
        {"name": tag},
        note="create a tag whose name contains ':'",
        predict={
            "status": 201,
            "notes": "DigitalOcean's own k8s:<cluster-id> tags use ':', and "
            "compute.py's _tag_url documents the pattern allowing it",
        },
    )
    if 0 < created.status < 400:
        probe.cleanup("DELETE", f"/tags/{tag}")
    probe.call(
        "GET",
        f"/tags/{tag}",
        note="fetch it back with ':' unescaped in the path",
        predict={"status": 200, "notes": "resources.count is 0: a tag need not tag anything"},
    )

    # --- 03: case folding ----------------------------------------------
    mixed = f"Aiform:TagProbe-{secrets.token_hex(3)}-UPPER"
    mixed_created = probe.call(
        "POST",
        "/tags",
        {"name": mixed},
        note="create a tag with uppercase letters",
        predict={
            "status": 201,
            "notes": "unknown whether the stored name keeps its case; the response "
            "body's tag.name answers it",
        },
    )
    if 0 < mixed_created.status < 400:
        stored = (mixed_created.body or {}).get("tag", {}).get("name", mixed)
        probe.cleanup("DELETE", f"/tags/{stored}")
        probe.call(
            "GET",
            f"/tags/{mixed.lower()}",
            note="fetch the uppercase tag by its lowercased name",
            predict={"status": 200, "notes": "200 if DigitalOcean folds case on store"},
        )

    # --- 04-05: what a name may not contain ----------------------------
    probe.call(
        "POST",
        "/tags",
        {"name": "has space"},
        note="create a tag whose name contains a space",
        predict={"status": 422},
    )
    probe.call(
        "POST",
        "/tags",
        {"name": "aiform:a/b"},
        note="create a tag whose name contains '/'",
        predict={"status": 422},
    )

    # --- 06-07: the attach endpoint, against a droplet that cannot exist -
    probe.call(
        "POST",
        f"/tags/{tag}/resources",
        {"resources": [{"resource_id": NONEXISTENT_DROPLET, "resource_type": "droplet"}]},
        note="attach an existing tag to a droplet id that does not exist",
        predict={"status": 404, "notes": "the endpoint exists; the droplet is what is missing"},
    )
    probe.call(
        "POST",
        f"/tags/{tag}-never-created/resources",
        {"resources": [{"resource_id": NONEXISTENT_DROPLET, "resource_type": "droplet"}]},
        note="attach a tag that was never created",
        predict={
            "status": 404,
            "notes": "compute.py's _ensure_tag_exists documents that assignment does not "
            "auto-create a tag",
        },
    )

    # --- 08-09: a firewall carrying a tag ------------------------------
    fw_name = unique_name(FW_PREFIX)
    created_fw = probe.call(
        "POST",
        "/firewalls",
        {"name": fw_name, "inbound_rules": [RULE], "tags": [tag]},
        note="create a firewall whose own `tags` field names the throwaway tag",
        predict={"status": 202, "notes": "free; `tags` here is documented as a droplet selector"},
    )
    fw_id = _firewall_id(created_fw)
    if 0 < created_fw.status < 400:
        probe.cleanup("DELETE", f"/firewalls/{fw_id}")
    probe.call(
        "GET",
        f"/tags/{tag}",
        note="does the tag now count the firewall among its resources",
        predict={
            "status": 200,
            "notes": "resources.count stays 0 if `tags` on a firewall is only a selector",
        },
    )
    probe.call(
        "POST",
        f"/tags/{tag}/resources",
        {"resources": [{"resource_id": fw_id, "resource_type": "firewall"}]},
        note="attach the tag to the firewall as a resource",
        predict={"status": 422, "notes": "firewall is not a taggable resource_type"},
    )

    # --- 09b: the same attach against a firewall that starts with no tags -
    # Step 11 above returned 204 yet the tag's counts did not move, and the
    # firewall had been created with the tag in its own `tags` field, so
    # neither the 204 nor a GET of that firewall can say whether the attach
    # did anything. A clean firewall can.
    bare_fw = probe.call(
        "POST",
        "/firewalls",
        {"name": unique_name(FW_PREFIX), "inbound_rules": [RULE]},
        note="create a firewall with no tags at all",
        predict={"status": 202},
    )
    bare_fw_id = _firewall_id(bare_fw)
    if 0 < bare_fw.status < 400:
        probe.cleanup("DELETE", f"/firewalls/{bare_fw_id}")
    probe.call(
        "POST",
        f"/tags/{tag}/resources",
        {"resources": [{"resource_id": bare_fw_id, "resource_type": "firewall"}]},
        note="attach the tag to the tagless firewall as a resource",
        predict={"status": 422, "notes": "step 11 was the same call and returned 204"},
    )
    probe.call(
        "GET",
        f"/firewalls/{bare_fw_id}",
        note="does the tagless firewall now carry the tag, as a selector or otherwise",
        predict={"status": 200, "notes": "tags stays [] if the attach was a no-op"},
    )

    # --- 10-12: a domain carrying a tag, and a TXT marker --------------
    zone = _zone_name()
    created_zone = probe.call(
        "POST",
        "/domains",
        {"name": zone},
        note="create a throwaway zone",
        predict={"status": 201, "notes": "free"},
    )
    if 0 < created_zone.status < 400:
        probe.cleanup("DELETE", f"/domains/{zone}")
    probe.call(
        "POST",
        f"/tags/{tag}/resources",
        {"resources": [{"resource_id": zone, "resource_type": "domain"}]},
        note="attach the tag to the domain as a resource",
        predict={"status": 422, "notes": "domain is not a taggable resource_type"},
    )
    probe.call(
        "GET",
        f"/domains/{zone}",
        note="does a domain record have any tags key",
        predict={"status": 200, "notes": "the transcript's body keys answer it"},
    )
    probe.call(
        "POST",
        f"/domains/{zone}/records",
        {"type": "TXT", "name": "@", "data": "user-owned-text", "ttl": 1800},
        note="a user's own apex TXT record, ttl 1800",
        predict={"status": 201},
    )
    probe.call(
        "POST",
        f"/domains/{zone}/records",
        {"type": "TXT", "name": "@", "data": "aiform:prod", "ttl": 300},
        note="the marker TXT record at the apex, a different ttl",
        predict={"status": 201, "notes": "a second TXT value at the same name is legal"},
    )
    probe.call(
        "GET",
        f"/domains/{zone}/records?type=TXT",
        note="read both apex TXT records back: ttl of the user's record, and the marker value",
        predict={
            "status": 200,
            "notes": "domain.py records that DigitalOcean silently rectifies a mismatched "
            "ttl across an RRset; the first record's ttl is expected to read 300 now. "
            "The marker's data is expected verbatim, colon and all",
        },
    )

    # --- 13-16: what a firewall name accepts ---------------------------
    suffix = secrets.token_hex(3)
    for note, name, status in (
        ("a firewall name containing ':'", f"aiform:tagprobe-{suffix}", 422),
        (
            "a firewall name shaped aiform-<deployment>-<name>, with '_' and '.'",
            f"aiform-prod_1-tagprobe.{suffix}",
            202,
        ),
        ("a firewall name containing '_' only", f"aiform-prod_1-tagprobe-{suffix}", 202),
        ("a firewall name containing '.' only", f"aiform-prod.1-tagprobe-{suffix}", 202),
        ("a firewall name with uppercase letters", f"Aiform-Prod-tagprobe-{suffix}", 202),
        ("a firewall name starting with a digit", f"1-aiform-tagprobe-{suffix}", 202),
        ("a firewall name of 255 characters", "a" * 255, 202),
        ("a firewall name of 256 characters", "a" * 256, 422),
    ):
        made = probe.call(
            "POST",
            "/firewalls",
            {"name": name, "inbound_rules": [RULE]},
            note=note,
            predict={"status": status},
        )
        if 0 < made.status < 400 and isinstance(made.body, dict):
            probe.cleanup("DELETE", f"/firewalls/{made.body['firewall']['id']}")

    # --- 17-18: a tag with zero resources persists, then deletes --------
    probe.call(
        "GET",
        f"/tags/{tag}",
        note="the throwaway tag is still there with nothing tagged",
        predict={"status": 200, "notes": "a tag persists until deleted, empty or not"},
    )
    probe.call(
        "DELETE",
        f"/tags/{tag}",
        note="delete the empty tag",
        predict={"status": 204},
    )
    probe.call(
        "GET",
        f"/tags/{tag}",
        note="the deleted tag is gone",
        predict={"status": 404},
    )


def _age_minutes(created_at: str) -> float:
    created = datetime.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    return (datetime.datetime.now(datetime.UTC) - created).total_seconds() / 60


def sweep(probe: Probe) -> int:
    """Leftovers from a crashed run. Everything here is free."""
    leaked = 0
    for collection, key, prefix in (
        ("firewalls", "firewalls", FW_PREFIX),
        ("firewalls", "firewalls", "aiform-prod_1-tagprobe"),
        ("domains", "domains", f"{ZONE_PREFIX}"),
        ("tags", "tags", TAG_PREFIX),
        ("tags", "tags", "aiform:tagprobe-"),
    ):
        listed = probe.call(
            "GET", f"/{collection}?per_page=200", note=f"sweep: list {collection}", record=False
        )
        for item in (listed.body or {}).get(key, []):
            name = item["name"]
            if not name.startswith(prefix) or "tagprobe" not in name:
                continue
            created_at = item.get("created_at")
            if created_at:
                age = _age_minutes(created_at)
                if age < SWEEP_MIN_AGE_MINUTES:
                    print(f"  skipping {name} -- {age:.0f}m old, a live run may still own it")
                    continue
            print(f"  LEAKED {collection} {name}")
            leaked += 1
            if probe.mutate:
                ident = item.get("id", name)
                probe.call("DELETE", f"/{collection}/{ident}", note="sweep: delete", record=False)
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
