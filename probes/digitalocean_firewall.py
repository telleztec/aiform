# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

"""Probe session for DigitalOcean firewalls.

Every firewall created here is UNATTACHED (droplet_ids: []). Unattached
firewalls are free and carry no traffic, so this whole session has zero
cost and zero blast radius -- which is why firewall is a good resource to
prove the probing loop on. Nothing here ever attaches to a real droplet.

Run:  python probes/digitalocean_firewall.py --dry-run   # sends nothing
      python probes/digitalocean_firewall.py --mutate    # against your account
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _probe import Probe, ProbeError, base_arg_parser, unique_name  # noqa: E402

SESSION = "digitalocean_firewall"
NAME_PREFIX = "aiform-system-test-fw"
MINIMAL_RULE = {"protocol": "tcp", "ports": "22", "sources": {"addresses": ["0.0.0.0/0"]}}


def _body(name, **overrides):
    body = {
        "name": name,
        "inbound_rules": [dict(MINIMAL_RULE)],
        "outbound_rules": [],
        "droplet_ids": [],
        "tags": [],
    }
    body.update(overrides)
    return body


def _create(probe, slug, *, note, predict, **overrides):
    name = unique_name(f"{NAME_PREFIX}-{slug}")
    result = probe.call("POST", "/firewalls", _body(name, **overrides), note=note, predict=predict)
    if result.status < 400 and isinstance(result.body, dict):
        probe.cleanup("DELETE", f"/firewalls/{result.body['firewall']['id']}")
    return result


def run(probe: Probe) -> None:
    # --- Increment 0: does this token carry firewall read AND write? ---
    probe.call(
        "GET",
        "/firewalls?per_page=1",
        note="scope check: can this token read firewalls",
        predict={"status": 200, "notes": "200 proves read scope; a 401/403 means reissue"},
    )

    # --- Increment 1: create, unattached ---
    created = _create(
        probe,
        "minimal",
        note="create unattached firewall with one inbound tcp/22 rule",
        predict={"status": 202, "notes": "status 'waiting' until it settles"},
    )
    if probe.dry_run:
        # No response to key off, but the remaining calls are still worth
        # printing -- that is what --dry-run is for.
        firewall, fid = {"name": "<dry-run>"}, "<dry-run-id>"
    elif created.status >= 400:
        raise ProbeError(f"baseline create failed ({created.status}); stopping")
    else:
        firewall = created.body["firewall"]
        fid = firewall["id"]

    # --- Increment 2: read-back and the zero-diff invariant ---
    probe.call(
        "GET",
        f"/firewalls/{fid}",
        note="read back the created firewall",
        predict={"status": 200, "notes": "rules echo exactly what was posted"},
    )
    _create(
        probe,
        "intport",
        note="is an int port coerced or rejected",
        predict={
            "status": 422,
            "notes": "a typed API should reject an int where a string is documented",
        },
        inbound_rules=[{"protocol": "tcp", "ports": 22, "sources": {"addresses": ["0.0.0.0/0"]}}],
    )
    _create(
        probe,
        "upper",
        note="is an uppercase protocol rejected or case-folded",
        predict={"status": 422},
        inbound_rules=[{"protocol": "TCP", "ports": "80", "sources": {"addresses": ["1.2.3.4"]}}],
    )
    _create(
        probe,
        "bareip",
        note="is a bare IP expanded to a 32 CIDR",
        predict={"status": 202, "notes": "expanded to 1.2.3.4/32"},
        inbound_rules=[{"protocol": "tcp", "ports": "80", "sources": {"addresses": ["1.2.3.4"]}}],
    )
    _create(
        probe,
        "ipv6",
        note="is an ipv6 range renormalized on store",
        predict={"status": 202, "notes": "returned verbatim"},
        inbound_rules=[
            {"protocol": "tcp", "ports": "80", "sources": {"addresses": ["2001:db8::/32"]}}
        ],
    )
    _create(
        probe,
        "norules",
        note="is a firewall with zero rules legal",
        predict={"status": 202},
        inbound_rules=[],
        outbound_rules=[],
    )
    _create(
        probe,
        "icmp",
        note="is ports synthesized for an icmp rule with none given",
        predict={"status": 202, "notes": "ports comes back as '0'"},
        inbound_rules=[{"protocol": "icmp", "sources": {"addresses": ["0.0.0.0/0"]}}],
    )
    _create(
        probe,
        "portsall",
        note="how is the spelling all stored for ports",
        predict={"status": 202, "notes": "stored as '0'"},
        inbound_rules=[
            {"protocol": "tcp", "ports": "all", "sources": {"addresses": ["0.0.0.0/0"]}}
        ],
    )
    _create(
        probe,
        "actionsent",
        note="is a server-set action field accepted when sent back",
        predict={"status": 202, "notes": "accepted and echoed"},
        inbound_rules=[{**MINIMAL_RULE, "action": "allow"}],
    )
    _create(
        probe,
        "actiondeny",
        note="is action deny accepted",
        predict={"status": 422, "notes": "DO firewalls are allow-only; deny is implicit"},
        inbound_rules=[{**MINIMAL_RULE, "action": "deny"}],
    )

    # --- Increment 4: update semantics ---
    probe.call(
        "PUT",
        f"/firewalls/{fid}",
        {"name": firewall["name"], "inbound_rules": [dict(MINIMAL_RULE)], "outbound_rules": []},
        note="does PUT omitting tags and droplet_ids reset them",
        predict={"status": 200, "notes": "documented as a full replace"},
    )
    probe.call(
        "PUT",
        f"/firewalls/{fid}",
        _body(unique_name(f"{NAME_PREFIX}-renamed")),
        note="can a firewall be renamed through PUT",
        predict={"status": 200, "notes": "accepted; aiform still will not expose it"},
    )

    # --- Increment 4: the rejection-status battery ---
    for slug, note, rule in [
        (
            "invrange",
            "how is an inverted port range rejected",
            {"protocol": "tcp", "ports": "9000-8000", "sources": {"addresses": ["0.0.0.0/0"]}},
        ),
        (
            "badcidr",
            "how is a malformed CIDR rejected",
            {"protocol": "tcp", "ports": "80", "sources": {"addresses": ["999.1.1.1/0"]}},
        ),
        (
            "sctp",
            "how is an unsupported protocol rejected",
            {"protocol": "sctp", "ports": "80", "sources": {"addresses": ["0.0.0.0/0"]}},
        ),
        (
            "emptysrc",
            "how is a rule with an empty sources object rejected",
            {"protocol": "tcp", "ports": "80", "sources": {}},
        ),
    ]:
        _create(probe, slug, note=note, predict={"status": 422}, inbound_rules=[rule])

    # --- Increment 5: relationship edges (all free) ---
    fresh_tag = unique_name("aiform-probe-tag")
    _create(
        probe,
        "newtag",
        note="does creating a firewall auto-create an unknown tag",
        predict={"status": 202, "notes": "auto-created, as droplet create does"},
        tags=[fresh_tag],
    )
    _create(
        probe,
        "srctag",
        note="is an unknown tag accepted inside a rule sources block",
        predict={"status": 202},
        inbound_rules=[
            {
                "protocol": "tcp",
                "ports": "80",
                "sources": {"tags": [unique_name("aiform-probe-srctag")]},
            }
        ],
    )
    _create(
        probe,
        "nodroplet",
        note="how is a nonexistent droplet id rejected",
        predict={"status": 422},
        droplet_ids=[1],
    )
    _create(
        probe,
        "nolb",
        note="how is a nonexistent load balancer uid rejected",
        predict={"status": 422},
        inbound_rules=[
            {
                "protocol": "tcp",
                "ports": "80",
                "sources": {"load_balancer_uids": ["00000000-0000-0000-0000-000000000000"]},
            }
        ],
    )

    # --- Increment 3: not-found semantics and delete idempotency ---
    probe.call(
        "GET",
        "/firewalls/00000000-0000-0000-0000-000000000000",
        note="a well formed but absent uuid",
        predict={"status": 404},
    )
    probe.call(
        "GET",
        "/firewalls/not-a-uuid",
        note="a malformed id: 404 like compute, or 422",
        predict={"status": 404, "notes": "compute 404s a malformed droplet id"},
    )
    doomed = _create(
        probe,
        "delete",
        note="create a firewall to delete twice",
        predict={"status": 202},
    )
    if doomed.status < 400:
        did = "<dry-run-id>" if probe.dry_run else doomed.body["firewall"]["id"]
        probe.call(
            "DELETE",
            f"/firewalls/{did}",
            note="delete an existing firewall",
            predict={"status": 204},
        )
        probe.call(
            "DELETE",
            f"/firewalls/{did}",
            note="delete the same firewall again",
            predict={"status": 404, "notes": "404 must read as success in delete()"},
        )


def sweep(probe: Probe) -> int:
    """Delete leftover probe firewalls. A non-empty sweep is a bug report."""
    result = probe.call("GET", "/firewalls?per_page=200", note="sweep: list firewalls")
    leaked = [
        f for f in (result.body or {}).get("firewalls", []) if f["name"].startswith(NAME_PREFIX)
    ]
    for f in leaked:
        print(f"  LEAKED {f['id']} {f['name']}")
        if probe.mutate:
            probe._send("DELETE", f"/firewalls/{f['id']}", None)
    return len(leaked)


def main(argv=None) -> int:
    args = base_arg_parser(__doc__).parse_args(argv)
    with Probe(SESSION, mutate=args.mutate, dry_run=args.dry_run) as probe:
        if args.sweep:
            n = sweep(probe)
            print(f"\n{n} leftover firewall(s)" + (" -- investigate" if n else ""))
            return 1 if n else 0
        run(probe)
        print(f"\n{probe._seq} probes; {len(probe.contradictions)} contradicted a prediction:")
        for line in probe.contradictions:
            print(f"  - {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
