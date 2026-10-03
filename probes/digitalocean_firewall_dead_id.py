# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

"""Probe session for #265: how long does a firewall keep a deleted droplet's id?

#232's severity depends on it. The only prior evidence
(knowledge/drivers/digitalocean_vpc/FINDINGS.md, transcript 13) is one firewall
read 12 seconds after a droplet DELETE, on a droplet that never reached
`active` and a firewall whose attach was still `waiting`. This session repeats
the experiment from a converged baseline and watches for much longer.

Sequence: create one droplet and wait for `active`; create one firewall naming
it and wait for `succeeded` with `pending_changes: []` and the droplet listed;
DELETE the droplet and poll it to 404 (every poll recorded); then read the
firewall by id at 0s, every 30s to 10 minutes, and at 15 and 20 minutes, with
the firewall list read alongside once a minute. Two consecutive reads without
the dead id end the observation early, which is what happened in the one run
recorded. If the dead id is still there at the end, try to remove it with a PUT
that omits it.

What it can settle: whether the id survives the window, and for how long if it
does not. What it cannot: "permanent". A reaper on a longer sweep passes every
read here.

Teardown is explicit, in a `finally`, by id; the harness's registered cleanups
are a backstop. A tag alone ever ties a resource to this session, and it is
`aiform-probe-265`, never `aiform-system-test` or `aiform-managed`, so the
system suite's sweeps cannot collide with it.

Run:  python probes/digitalocean_firewall_dead_id.py --dry-run
      python probes/digitalocean_firewall_dead_id.py --mutate
"""

import datetime
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _probe import (  # noqa: E402
    BASE_URL,
    REDACTED,
    TOKEN_ENV_VAR,
    Probe,
    base_arg_parser,
    unique_name,
)

SESSION = "digitalocean_firewall_dead_id"
REGION = "sfo3"
PROBE_TAG = "aiform-probe-265"
NAME_PREFIX = "aiform-probe-265-"

DROPLET_BODY = {
    "region": REGION,
    "size": "s-1vcpu-512mb-10gb",
    "image": "ubuntu-24-04-x64",
    "backups": False,
    "monitoring": False,
    "tags": [PROBE_TAG],
}

RULES = {
    "inbound_rules": [
        {
            "protocol": "tcp",
            "ports": "22",
            "sources": {"addresses": ["0.0.0.0/0"]},
        }
    ],
    "outbound_rules": [],
}

POLL_SECONDS = 15
DROPLET_ACTIVE_ATTEMPTS = 20
FIREWALL_CONVERGE_SECONDS = 10
FIREWALL_CONVERGE_ATTEMPTS = 30
GONE_POLL_SECONDS = 5
GONE_POLL_ATTEMPTS = 60

OBSERVE_OFFSETS = [*range(30, 601, 30), 900, 1200]
LIST_EVERY_SECONDS = 60

DRY_ID = "<dry-run-id>"

_SCRUB_KEYS = frozenset({"ip_address", "gateway", "netmask", "vpc_uuid", "subnet_uuid"})


def _scrub(value):
    if isinstance(value, dict):
        return {k: ("<scrubbed>" if k in _SCRUB_KEYS else _scrub(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


class ScrubbingProbe(Probe):
    """Droplet bodies carry public IPs and the team's VPC uuid; neither is a finding."""

    def _write(self, seq, note, payload):
        response = payload.get("response") or {}
        response["body"] = _scrub(response.get("body"))
        super()._write(seq, note, payload)


def _created_id(result, key, name):
    if result.status >= 400:
        raise SystemExit(f"setup call failed with {result.status}; aborting session")
    if not isinstance(result.body, dict):
        raise SystemExit(
            f"{key} {name!r} was created ({result.status}) but its body did not parse, so no "
            f"cleanup could be registered -- it is LIVE and untracked. Destroy it by hand NOW."
        )
    created = result.body.get(key)
    if not isinstance(created, dict) or "id" not in created:
        raise SystemExit(
            f"{key} {name!r} was created ({result.status}) but its body carried no id, so no "
            f"cleanup could be registered -- it is LIVE and untracked. Destroy it by hand NOW."
        )
    return created["id"]


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _firewall_view(body) -> dict:
    firewall = (body or {}).get("firewall") or {}
    return {
        "droplet_ids": firewall.get("droplet_ids"),
        "status": firewall.get("status"),
        "pending_changes": firewall.get("pending_changes"),
    }


def _write_custom(probe: Probe, note: str, request: dict, response: dict, elapsed_ms: int) -> None:
    probe._seq += 1
    probe._write(
        probe._seq,
        note,
        {
            "seq": probe._seq,
            "note": note,
            "credentials_key": TOKEN_ENV_VAR,
            "prediction": None,
            "prediction_matched": None,
            "request": {**request, "headers": {"Authorization": REDACTED}},
            "response": response,
            "elapsed_ms": elapsed_ms,
            "probed_at": _now().isoformat(),
        },
    )


def _read_firewall_list(probe: Probe, fw_id: str, note: str) -> dict:
    """The list is read unrecorded and written back filtered to this probe's firewall.

    The account holds other firewalls; their bodies are not this probe's to
    commit.
    """
    started = _now()
    listed = probe.call("GET", "/firewalls?per_page=200", note=note, record=False)
    elapsed_ms = int((_now() - started).total_seconds() * 1000)
    firewalls = (listed.body or {}).get("firewalls") or []
    ours = [fw for fw in firewalls if fw.get("id") == fw_id]
    _write_custom(
        probe,
        note,
        {"method": "GET", "url": BASE_URL + "/firewalls?per_page=200", "body": None},
        {
            "status": listed.status,
            "body": {
                "firewalls": ours,
                "filtered_to": "this probe's firewall only",
                "total_firewalls_on_account": len(firewalls),
            },
        },
        elapsed_ms,
    )
    return _firewall_view({"firewall": ours[0]}) if ours else {"missing_from_list": True}


def _wait_for_droplet_active(probe: Probe, droplet_id: int) -> bool:
    for attempt in range(DROPLET_ACTIVE_ATTEMPTS):
        seen = probe.call(
            "GET", f"/droplets/{droplet_id}", note=f"wait {attempt + 1}: active", record=False
        )
        status = ((seen.body or {}).get("droplet") or {}).get("status")
        print(f"     droplet status={status!r}")
        if status == "active":
            return True
        time.sleep(POLL_SECONDS)
    return False


def _wait_for_firewall_converged(probe: Probe, fw_id: str, droplet_id: int) -> bool:
    for attempt in range(FIREWALL_CONVERGE_ATTEMPTS):
        seen = probe.call(
            "GET", f"/firewalls/{fw_id}", note=f"wait {attempt + 1}: converged", record=False
        )
        view = _firewall_view(seen.body)
        print(f"     firewall {view}")
        if (
            view["status"] == "succeeded"
            and view["pending_changes"] == []
            and droplet_id in (view["droplet_ids"] or [])
        ):
            return True
        time.sleep(FIREWALL_CONVERGE_SECONDS)
    return False


def _poll_droplet_gone(probe: Probe, droplet_id: int, *, record: bool) -> bool:
    for attempt in range(GONE_POLL_ATTEMPTS):
        seen = probe.call(
            "GET",
            f"/droplets/{droplet_id}",
            note=f"poll {attempt + 1}: droplet gone yet",
            record=record,
        )
        if seen.status == 404:
            return True
        time.sleep(GONE_POLL_SECONDS)
    return False


def _dry_run(probe: Probe) -> None:
    probe.call("POST", "/droplets", DROPLET_BODY, note="create the droplet")
    probe.call("POST", "/firewalls", RULES, note="create the firewall naming it")
    probe.call("DELETE", f"/droplets/{DRY_ID}", note="delete the droplet")
    probe.call("GET", f"/firewalls/{DRY_ID}", note="read the firewall at intervals")
    probe.call("GET", "/firewalls?per_page=200", note="list the firewalls alongside")
    probe.call("PUT", f"/firewalls/{DRY_ID}", RULES, note="remove the dead id if still present")
    probe.call("DELETE", f"/firewalls/{DRY_ID}", note="teardown the firewall")
    probe.call("DELETE", f"/tags/{PROBE_TAG}", note="teardown the tag")


def run(probe: Probe, state: dict) -> None:
    droplet_name = unique_name(f"{NAME_PREFIX}droplet")
    created = probe.call(
        "POST",
        "/droplets",
        {"name": droplet_name, **DROPLET_BODY},
        note="create the one probe droplet",
        predict={"status": 202, "notes": "billable; status new"},
    )
    droplet_id = _created_id(created, "droplet", droplet_name)
    state["droplet_id"] = droplet_id
    state["tag_created"] = True
    probe.cleanup("DELETE", f"/droplets/{droplet_id}")

    if not _wait_for_droplet_active(probe, droplet_id):
        raise SystemExit("droplet never reached active; nothing to measure -- tearing down")
    probe.call(
        "GET",
        f"/droplets/{droplet_id}",
        note="the droplet is active",
        predict={"status": 200, "notes": "status active"},
    )

    fw_name = unique_name(f"{NAME_PREFIX}fw")
    created_fw = probe.call(
        "POST",
        "/firewalls",
        {"name": fw_name, "droplet_ids": [droplet_id], **RULES},
        note="create the one probe firewall naming the active droplet",
        predict={"status": 202, "notes": "status waiting, pending change adds the droplet"},
    )
    fw_id = _created_id(created_fw, "firewall", fw_name)
    state["firewall_id"] = fw_id
    probe.cleanup("DELETE", f"/firewalls/{fw_id}")

    if not _wait_for_firewall_converged(probe, fw_id, droplet_id):
        raise SystemExit(
            "firewall never converged with the droplet as a live member -- tearing down"
        )
    baseline = probe.call(
        "GET",
        f"/firewalls/{fw_id}",
        note="baseline: converged firewall with a live member",
        predict={
            "status": 200,
            "notes": "status succeeded, pending_changes [], droplet_ids [the droplet]",
        },
    )
    print(f"  baseline {_firewall_view(baseline.body)}")

    delete_started = _now()
    deleted = probe.call(
        "DELETE",
        f"/droplets/{droplet_id}",
        note="delete the droplet the firewall lists",
        predict={"status": 204, "notes": "asynchronous"},
    )
    if deleted.status != 204:
        raise SystemExit(f"droplet DELETE returned {deleted.status}; stopping")
    state["droplet_deleted"] = True
    if not _poll_droplet_gone(probe, droplet_id, record=True):
        raise SystemExit("droplet never reached 404 within the poll budget; stopping")
    state["droplet_gone"] = True
    gone_at = _now()
    print(f"  droplet 404 after {(gone_at - delete_started).total_seconds():.0f}s")

    observations: list[dict] = []
    offsets = [0, *OBSERVE_OFFSETS]
    next_list_at = 0
    for offset in offsets:
        wait = offset - (_now() - gone_at).total_seconds()
        if wait > 0:
            time.sleep(wait)
        elapsed = (_now() - gone_at).total_seconds()
        seen = probe.call(
            "GET",
            f"/firewalls/{fw_id}",
            note=f"firewall read {elapsed:.0f}s after the droplet 404'd",
            predict={
                "status": 200,
                "notes": "prediction: dead id still listed, status succeeded (#232's premise)",
            },
        )
        view = _firewall_view(seen.body)
        entry = {"after_404_s": round(elapsed), "by_id": view}
        if elapsed >= next_list_at - 1:
            entry["in_list"] = _read_firewall_list(
                probe, fw_id, f"firewall list {elapsed:.0f}s after the droplet 404'd"
            )
            next_list_at = elapsed + LIST_EVERY_SECONDS
        observations.append(entry)
        print(f"  {entry}")
        state["observations"] = observations
        if droplet_id not in (view["droplet_ids"] or []):
            print("  the dead id has LEFT droplet_ids")
            if len(observations) >= 2 and droplet_id not in (
                observations[-2]["by_id"]["droplet_ids"] or []
            ):
                break

    last = observations[-1]["by_id"]["droplet_ids"] or []
    if droplet_id in last:
        removed = probe.call(
            "PUT",
            f"/firewalls/{fw_id}",
            {"name": fw_name, "droplet_ids": [], "tags": [], **RULES},
            note="update the firewall to drop the dead id",
            predict={"status": 200, "notes": "unknown whether a dead id is rejected"},
        )
        state["put_status"] = removed.status
        after = probe.call(
            "GET",
            f"/firewalls/{fw_id}",
            note="firewall read after the PUT",
            predict={"status": 200, "notes": "droplet_ids empty if the PUT was accepted"},
        )
        print(f"  after PUT {_firewall_view(after.body)}")
        state["after_put"] = _firewall_view(after.body)


def teardown(probe: Probe, state: dict) -> None:
    droplet_id = state.get("droplet_id")
    if droplet_id and not state.get("droplet_gone"):
        if not state.get("droplet_deleted"):
            probe.call("DELETE", f"/droplets/{droplet_id}", note="teardown: delete the droplet")
        _poll_droplet_gone(probe, droplet_id, record=False)
    if state.get("firewall_id"):
        probe.call("DELETE", f"/firewalls/{state['firewall_id']}", note="teardown: delete firewall")
    if state.get("tag_created"):
        probe.call("DELETE", f"/tags/{PROBE_TAG}", note="teardown: delete the probe tag")


def verify_clean(probe: Probe) -> bool:
    clean = True
    summary = {}
    for collection in ("droplets", "firewalls", "domains"):
        listed = probe.call(
            "GET", f"/{collection}?per_page=200", note=f"verify: list {collection}", record=False
        )
        items = (listed.body or {}).get(collection) or []
        mine = [i for i in items if str(i.get("name", "")).startswith(NAME_PREFIX)]
        tagged = [i for i in items if PROBE_TAG in (i.get("tags") or [])]
        summary[collection] = {
            "status": listed.status,
            "total": len(items),
            "named_aiform_probe_265": len(mine),
            "tagged_aiform_probe_265": len(tagged),
        }
        if collection == "domains":
            summary[collection]["names"] = sorted(i.get("name") for i in items)
        if listed.status != 200 or mine or tagged:
            clean = False
    tag = probe.call("GET", f"/tags/{PROBE_TAG}", note="verify: the probe tag", record=False)
    summary["tag_lookup_status"] = tag.status
    if tag.status != 404:
        clean = False
    _write_custom(
        probe,
        "verify: nothing of this session remains",
        {"method": "GET", "url": BASE_URL + "/{droplets,firewalls,domains}?per_page=200"},
        {"status": 200 if clean else 0, "body": {"clean": clean, **summary}},
        0,
    )
    print(f"  verify: {json.dumps(summary)}")
    return clean


def main(argv=None) -> int:
    args = base_arg_parser(__doc__).parse_args(argv)
    if args.sweep:
        print("no sweep: this session cleans up by id in a finally; list by hand if it crashed")
        return 2
    state: dict = {}
    with ScrubbingProbe(SESSION, mutate=args.mutate, dry_run=args.dry_run) as probe:
        if args.dry_run:
            _dry_run(probe)
            return 0
        clean = False
        try:
            run(probe, state)
        finally:
            try:
                teardown(probe, state)
            finally:
                clean = verify_clean(probe)
                print(f"\n{probe._seq} calls; cleanup verified clean: {clean}")
                print(
                    json.dumps({k: v for k, v in state.items() if k != "observations"}, default=str)
                )
        return 0 if clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
