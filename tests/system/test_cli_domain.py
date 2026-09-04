# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

"""Live end-to-end system test for drivers/digitalocean/domain.py against
the real DigitalOcean and Anthropic APIs, per specs/system_test_domain.md.
Excluded from the default `pytest` run (see pyproject.toml's `addopts`);
run explicitly with:

    pytest -m system tests/system/

Requires ANTHROPIC_API_KEY and DIGITALOCEAN_TOKEN in the environment, and
a token carrying `domain` scope -- see tests/system/conftest.py's
session-scoped skip fixture and case 1's own probe.

Unlike the droplet suite this creates nothing billable: DigitalOcean
hosts DNS zones for free. The real cost is Opus/Sonnet-priced gate calls,
so this still must never run on the default pull_request/push CI trigger.

The point of this suite is NOT to re-prove the CLI, orchestrator, gates
or state machinery -- test_cli_digitalocean.py already does that. It is
to settle the assumptions about DO's DNS API that domain.py hardcodes and
that a mock structurally cannot falsify, because the mock encodes the
same assumption the driver does.
"""

import urllib.error

import pytest

from aiform import cli, state
from drivers.digitalocean import domain as do_domain
from tests.system.conftest import (
    assert_cli_ok,
    count_driver_reads,
    create_domain_directly,
    delete_domain_directly,
    get_domain_or_none,
    list_domain_records,
    live_token,
    token_has_domain_scope,
    unique_zone_name,
    wait_until_domain_gone,
    write_domain_aiform_md,
)

pytestmark = pytest.mark.system

TTL = 1800

# Written DOTLESS throughout, for every _FQDN_TYPES record. DigitalOcean
# requires a trailing dot on the wire and returns the value without one
# (422 "Data needs to end with a dot (.)" on a dotless POST), so the
# dotless spelling is the only form read() can ever return and therefore
# the only one that round-trips to a stable no-op. domain.py's
# _to_wire_record() adds the dot at the wire boundary and nowhere else.
# If that stops being true, case 3 and case 4 are where it surfaces.
BASE_RECORDS = [
    {"type": "A", "name": "@", "data": "203.0.113.10", "ttl": TTL},
    {"type": "CNAME", "name": "www", "data": "target.example.com", "ttl": TTL},
    {"type": "MX", "name": "@", "data": "mail.example.com", "ttl": TTL, "priority": 10},
    {"type": "TXT", "name": "@", "data": "v=spf1 -all", "ttl": TTL},
]

# One record of every type domain.py supports, carrying exactly the
# fields _TYPE_EXTRA_FIELDS claims each requires. This is the case that
# settles specs/digitalocean_domain.md's last "recalled, not verified"
# item -- a field the table calls required but DO rejects, or one DO
# demands that the table omits, surfaces here as a 422.
#
# Three entries are chosen for what they can break, not for coverage:
#   - the TXT data carries embedded quotes, settling "stored verbatim,
#     quoted or unquoted";
#   - the two CAA records share a name AND an identical `data`, differing
#     only in `tag` -- the pair that broke _reconcile_set_path back when
#     it paired on `data` alone rather than the whole projected record;
#   - the NS is a delegated subdomain (name != "@"), which
#     _is_do_managed_ns must NOT filter out even though it points at a DO
#     nameserver, since only the apex is DO-managed.
ALL_TYPE_RECORDS = [
    {"type": "A", "name": "@", "data": "203.0.113.10", "ttl": TTL},
    {"type": "AAAA", "name": "@", "data": "2001:db8::1", "ttl": TTL},
    {"type": "CNAME", "name": "www", "data": "target.example.com", "ttl": TTL},
    {"type": "MX", "name": "@", "data": "mail.example.com", "ttl": TTL, "priority": 10},
    {"type": "NS", "name": "delegated", "data": "ns1.digitalocean.com", "ttl": TTL},
    {
        "type": "SRV",
        "name": "_sip._tcp",
        "data": "sipserver.example.com",
        "ttl": TTL,
        "priority": 10,
        "port": 5060,
        "weight": 5,
    },
    {"type": "TXT", "name": "@", "data": '"v=spf1 include:_spf.example.com -all"', "ttl": TTL},
    {"type": "CAA", "name": "@", "data": "letsencrypt.org", "ttl": TTL, "flags": 0, "tag": "issue"},
    {
        "type": "CAA",
        "name": "@",
        "data": "letsencrypt.org",
        "ttl": TTL,
        "flags": 0,
        "tag": "issuewild",
    },
]


# Guards the claim both specs now make -- that this suite verifies the
# per-type required-field table for EVERY type the driver supports. Add a
# ninth type to _RECORD_TYPES without adding it here and that claim
# silently becomes false while the suite stays green.
assert {r["type"] for r in ALL_TYPE_RECORDS} == set(do_domain._RECORD_TYPES), (
    "ALL_TYPE_RECORDS must cover every type in domain.py's _RECORD_TYPES"
)


def _resource_key(zone: str) -> str:
    return f"digitalocean.domain.{zone}"


def _verbose_call_count(captured) -> int:
    assert "[verbose]" in captured.err, f"no [verbose] line in stderr:\n{captured.err}"
    return int(captured.err.split("[verbose] ")[1].split(" Anthropic")[0])


def _find_live(records: list[dict], **match) -> dict:
    """The one raw DO record matching every key in `match`.

    Asserts uniqueness rather than taking the first hit: several of the
    assertions below turn on a specific record's DO id surviving an
    update, and silently picking the first of two would make a real
    reconciliation bug look like a pass.
    """
    hits = [r for r in records if all(r.get(k) == v for k, v in match.items())]
    assert len(hits) == 1, f"expected exactly one record matching {match}, got {hits}"
    return hits[0]


def _managed(records: list[dict]) -> list[dict]:
    """The records a user manages -- what read() should report, i.e.
    everything DO did not create for itself."""
    return [
        r
        for r in records
        if r["type"] != "SOA"
        and not (r["type"] == "NS" and r["name"] == "@" and "digitalocean.com" in r["data"])
    ]


def _sorted_records(records: list[dict]) -> list[dict]:
    """Order-insensitive comparison key for a records list. `records` is
    in UNORDERED_FIELDS, so any two orderings are the same value."""
    return sorted(records, key=lambda r: sorted(r.items()))


def _assert_converges(state_path, key: str, capsys, step: str) -> None:
    """Re-plan with the file untouched and assert a true no-op.

    Every mutation in the sequence is followed by this, rather than only
    by an assertion on its immediate effect: an apply that lands the
    right records but does not converge means a diff -- and an
    intent-orchestration-model call -- on every subsequent plan forever,
    which is a silent, permanent cost rather than a visible failure.
    Affordable after every single step only because DO bills nothing for
    DNS zones.
    """
    code = cli.main(["plan", "create", "--state-file", str(state_path), "--verbose"])
    captured = capsys.readouterr()
    assert_cli_ok(code, captured, f"{step}: convergence re-plan")
    assert f"= {key}: no-op" in captured.out, (
        f"{step} did not converge -- re-planning an untouched file still reports a diff:\n"
        f"{captured.out}"
    )
    assert "[verbose] 0 Anthropic API call(s) made" in captured.err


class TestDomainLifecycleSequence:
    """Cases 1-9 of specs/system_test_domain.md: one ordered sequence
    sharing a single tmp_path and a single tracked zone, because each
    step depends on state the previous one created. Splitting these into
    separate test functions would give each its own teardown instance and
    destroy the zone after the first."""

    def test_full_lifecycle(self, project_dir, teardown_tracked_resources, monkeypatch, capsys):
        token = live_token()

        # Case 1: domain-scope preflight, then `aiform init`.
        #
        # The probe is not redundant with init's [✓]: cli.py's
        # _check_droplet_scope probes GET /v2/droplets only, so a
        # droplet-scoped token earns a green check here and then fails at
        # the first domain apply.
        if not token_has_domain_scope(token):
            pytest.skip(
                "this DIGITALOCEAN_TOKEN cannot read /v2/domains -- the domain suite needs a "
                "token with `domain` scope; aiform init's preflight only checks droplet access"
            )

        state_path = project_dir / ".aiform" / "state.json"
        zone = unique_zone_name("lifecycle")
        key = _resource_key(zone)

        code = cli.main(["init"])
        out = capsys.readouterr().out
        assert code == 0
        assert "[✓] ANTHROPIC_API_KEY" in out
        # Not an exact-line match: the DO check carries a detail after the
        # variable name (the account email, or "authenticated (scoped token)").
        assert "[✓] DIGITALOCEAN_TOKEN" in out

        write_domain_aiform_md(project_dir, name=zone, records=BASE_RECORDS)

        # Case 2: first `plan create` -- gate #1 fires (trust-on-first-use;
        # nothing in state yet trusts domain.py's hash).
        code = cli.main(["plan", "create", "--state-file", str(state_path), "--verbose"])
        captured = capsys.readouterr()
        assert_cli_ok(code, captured, "case 2: first plan create")
        assert f"+ {key}: create" in captured.out
        assert _verbose_call_count(captured) >= 1

        # Case 3: `plan apply --yes`. A separate invocation, so gate #1
        # fires again -- plan create persists no driver-trust record, only
        # a resource-creating apply does.
        code = cli.main(["plan", "apply", "--yes", "--state-file", str(state_path), "--verbose"])
        captured = capsys.readouterr()
        assert_cli_ok(code, captured, "case 3: plan apply --yes")
        assert _verbose_call_count(captured) >= 1

        st = state.load(state_path)
        assert key in st.resources
        entry = st.resources[key]
        assert entry.id == zone
        assert entry.driver.sha256
        assert entry.driver.code_review is not None
        assert (project_dir / ".aiform" / "state.json.backup").exists()

        live_records = list_domain_records(token, zone)

        # The dot asymmetry. Every _FQDN_TYPES record above was written
        # dotless; DO requires the dot on the wire but must return the
        # value without one, or the dotless canonical form the user wrote
        # would diff forever. This is _to_wire_record()'s whole
        # justification, and the assumption the unit tests' mock shares
        # with the driver rather than checks.
        cname = _find_live(live_records, type="CNAME", name="www")
        assert cname["data"] == "target.example.com", (
            f"expected DO to store the CNAME target dotless, got {cname['data']!r} -- "
            "domain.py's dotless canonical form no longer round-trips"
        )
        mx = _find_live(live_records, type="MX", name="@")
        assert mx["data"] == "mail.example.com"

        # SOA and the apex NS records exist on DO's side and must be
        # absent from state: _filter_managed() owns that boundary.
        assert [r for r in live_records if r["type"] == "SOA"], (
            "expected DO to expose an SOA record in the listing"
        )
        assert [r for r in live_records if r["type"] == "NS" and r["name"] == "@"], (
            "expected DO's auto-created apex NS records in the listing"
        )

        tracked = st.resources[key].attributes["records"]
        assert not [r for r in tracked if r["type"] == "SOA"]
        assert not [r for r in tracked if r["type"] == "NS" and r["name"] == "@"]
        assert _sorted_records(tracked) == _sorted_records(BASE_RECORDS), (
            "state's records must match the .aiform.md verbatim after create"
        )

        # Case 4: second `plan create`, file unchanged. The zero-diff
        # invariant measured against reality -- the highest-value
        # assertion in this suite. It folds in TXT verbatim storage, the
        # dot round-trip, DO's own record ordering vs UNORDERED_FIELDS,
        # TTL rectification and CAA tag handling all at once.
        read_calls = count_driver_reads(monkeypatch)
        code = cli.main(["plan", "create", "--state-file", str(state_path), "--verbose"])
        captured = capsys.readouterr()
        assert_cli_ok(code, captured, "case 4: unchanged plan create")
        assert "[verbose] 0 Anthropic API call(s) made" in captured.err
        assert f"= {key}: no-op" in captured.out
        assert len(read_calls) == 1

        # Case 5: `plan refresh`. No --verbose line exists for this
        # command (refresh/show dispatch through _PLAIN_PLAN_DISPATCH,
        # which never builds a _CountingClient), so the zero-LLM-call
        # property here is structural. Assert against a live read instead.
        code = cli.main(["plan", "refresh", "--state-file", str(state_path)])
        assert code == 0
        st = state.load(state_path)
        live_managed = _managed(list_domain_records(token, zone))
        tracked = st.resources[key].attributes["records"]
        # Content, not just count: a refresh that wrote the right number
        # of records with wrong data or ttl would satisfy a length check.
        # Each tracked record must exist live with every one of its own
        # field values -- compared against the raw API payload rather than
        # through the driver's own _project_record(), which would be
        # circular here.
        for record in tracked:
            assert [r for r in live_managed if all(r.get(k) == v for k, v in record.items())], (
                f"refresh recorded {record} but no live record matches it"
            )
        assert len(tracked) == len(live_managed)

        # Case 6: one record of every supported type. Settles the per-type
        # required-field table.
        write_domain_aiform_md(project_dir, name=zone, records=ALL_TYPE_RECORDS)
        code = cli.main(["plan", "create", "--state-file", str(state_path)])
        assert code == 0
        assert f"~ {key}: update" in capsys.readouterr().out

        code = cli.main(["plan", "apply", "--yes", "--state-file", str(state_path)])
        captured = capsys.readouterr()
        assert_cli_ok(code, captured, "case 6: all-record-types apply")

        live_records = list_domain_records(token, zone)

        # The delegated-subdomain NS must survive: only the apex is
        # DO-managed, so _is_do_managed_ns must not filter this one out.
        delegated = _find_live(live_records, type="NS", name="delegated")
        assert delegated["data"] == "ns1.digitalocean.com"

        # TXT stored verbatim, quotes and all.
        txt = _find_live(live_records, type="TXT", name="@")
        assert txt["data"] == '"v=spf1 include:_spf.example.com -all"', (
            f"expected TXT data stored verbatim, got {txt['data']!r}"
        )

        # Both CAA records survive independently despite sharing a name
        # and an identical `data` -- the regression _reconcile_set_path's
        # whole-record pairing exists to prevent.
        caa = [r for r in live_records if r["type"] == "CAA"]
        assert sorted(r["tag"] for r in caa) == ["issue", "issuewild"]
        assert {r["data"] for r in caa} == {"letsencrypt.org"}

        _assert_converges(state_path, key, capsys, "case 6")

        # Case 7a: single-valued PUT. The apex A record's DO id must
        # survive -- that is what proves _reconcile_single_valued issued a
        # PUT rather than a delete/create pair. Asserting only the new
        # value would pass either way.
        apex_a_id = _find_live(list_domain_records(token, zone), type="A", name="@")["id"]

        records = [dict(r) for r in ALL_TYPE_RECORDS]
        records[0] = {**records[0], "data": "203.0.113.20"}
        write_domain_aiform_md(project_dir, name=zone, records=records)
        code = cli.main(["plan", "apply", "--yes", "--state-file", str(state_path)])
        captured = capsys.readouterr()
        assert_cli_ok(code, captured, "case 7a: single-valued PUT apply")
        assert "(likely replace)" not in captured.out

        apex_a = _find_live(list_domain_records(token, zone), type="A", name="@")
        assert apex_a["data"] == "203.0.113.20"
        assert apex_a["id"] == apex_a_id, (
            "the apex A record was recreated rather than PUT in place -- "
            "_reconcile_single_valued took the delete/create path"
        )
        _assert_converges(state_path, key, capsys, "case 7a")

        # Case 7b: ttl-only change on a set-path type. Pairs via
        # _key_without_ttl, so again the DO id must survive.
        mx_id = _find_live(list_domain_records(token, zone), type="MX", name="@")["id"]

        records = [{**r, "ttl": 3600} if r["type"] == "MX" else dict(r) for r in records]
        write_domain_aiform_md(project_dir, name=zone, records=records)
        code = cli.main(["plan", "apply", "--yes", "--state-file", str(state_path)])
        captured = capsys.readouterr()
        assert_cli_ok(code, captured, "case 7b: ttl-only PUT apply")
        assert "(likely replace)" not in captured.out

        mx = _find_live(list_domain_records(token, zone), type="MX", name="@")
        assert mx["ttl"] == 3600
        assert mx["id"] == mx_id, (
            "a ttl-only change recreated the MX record -- _key_without_ttl pairing did not fire"
        )
        _assert_converges(state_path, key, capsys, "case 7b")

        # Case 7c: add one MX, drop the TXT. The surviving MX's id must be
        # untouched (POST and DELETE touched only what changed), and DO's
        # silent TTL rectification must not have rewritten it -- the
        # behavior _validate_ttl_consistency exists to forestall.
        # ttl 3600 on the new MX matches the RRset, which that same
        # validation requires.
        records = [r for r in records if r["type"] != "TXT"]
        records.append(
            {"type": "MX", "name": "@", "data": "mail2.example.com", "ttl": 3600, "priority": 20}
        )
        write_domain_aiform_md(project_dir, name=zone, records=records)
        code = cli.main(["plan", "apply", "--yes", "--state-file", str(state_path)])
        captured = capsys.readouterr()
        assert_cli_ok(code, captured, "case 7c: add-and-remove apply")
        assert "(likely replace)" not in captured.out

        live_records = list_domain_records(token, zone)
        assert not [r for r in live_records if r["type"] == "TXT"]
        surviving_mx = _find_live(live_records, type="MX", data="mail.example.com")
        assert surviving_mx["id"] == mx_id, "the untouched MX record was recreated"
        assert surviving_mx["ttl"] == 3600
        assert _find_live(live_records, type="MX", data="mail2.example.com")["priority"] == 20
        _assert_converges(state_path, key, capsys, "case 7c")

        # Case 7d: pure reorder, no semantic change. End-to-end proof of
        # UNORDERED_FIELDS = ["records"] against real read() output: the
        # plan must be a no-op, so no apply is proposed and nothing is
        # rewritten on DO's side.
        #
        # Deliberately does NOT assert a zero Anthropic call count, and
        # that is not an oversight. planner.py's short-circuit requires
        # BOTH an empty diff and an unchanged .aiform.md sha256:
        #
        #     if not diff and state_aiform_md_sha256 == current_aiform_md_sha256 ...
        #
        # Reordering rewrites the file, so the hash changes and the Intent
        # prose is re-parsed and re-categorized -- correct, since prose
        # that the user edited in the same save could mean something new.
        # What UNORDERED_FIELDS buys is a no-op *plan*, not a free one; the
        # zero-call guarantee is keyed on the file, and case 4 and
        # _assert_converges are where that is asserted. An earlier draft of
        # this case asserted 0 here and failed on the first live run
        # against a plan that was, correctly, already a no-op.
        write_domain_aiform_md(project_dir, name=zone, records=list(reversed(records)))
        code = cli.main(["plan", "create", "--state-file", str(state_path), "--verbose"])
        captured = capsys.readouterr()
        assert_cli_ok(code, captured, "case 7d: reordered plan create")
        assert f"= {key}: no-op" in captured.out, (
            "reordering the records list produced a diff -- UNORDERED_FIELDS is not "
            "taking effect end to end"
        )

        # Case 7e: records: []. A zone with only its DO-managed SOA/NS
        # records is a stable no-op state, not a perpetual diff -- read()
        # must report [] after filtering.
        write_domain_aiform_md(project_dir, name=zone, records=[])
        code = cli.main(["plan", "apply", "--yes", "--state-file", str(state_path)])
        captured = capsys.readouterr()
        assert_cli_ok(code, captured, "case 7e: empty-records apply")
        assert "(likely replace)" not in captured.out

        assert _managed(list_domain_records(token, zone)) == []
        assert state.load(state_path).resources[key].attributes["records"] == []
        _assert_converges(state_path, key, capsys, "case 7e")

        # Case 8: `plan destroy --yes` -- gate #2 fires unconditionally.
        # The call count is the only direct evidence it ran: a regression
        # that silently skipped review_plan() for a destroy-only plan
        # would still leave the zone gone.
        code = cli.main(["plan", "destroy", "--yes", "--state-file", str(state_path), "--verbose"])
        captured = capsys.readouterr()
        assert_cli_ok(code, captured, "case 8: plan destroy --yes")
        assert _verbose_call_count(captured) >= 1

        leftover = wait_until_domain_gone(token, zone)
        assert leftover is None, f"destroyed zone {zone} still live: {leftover}"
        assert list((project_dir / ".aiform" / "trash").glob("*domain*"))
        assert key not in state.load(state_path).resources

        # Case 9: idempotent delete -- 404 treated as success, which
        # aiform/driver.py's contract requires of every driver.
        assert do_domain.Driver().delete(zone, {"DIGITALOCEAN_TOKEN": token}) is None


def test_bad_token_fails_cleanly_without_leaking_or_tracking(
    project_dir, teardown_tracked_resources, monkeypatch, capsys
):
    """Case 10: an obviously invalid DIGITALOCEAN_TOKEN must fail
    `plan apply` cleanly -- no crash, no state entry, no zone, and the
    bad token's literal value in neither stream. That last check is real
    rather than a formality: PLAN.md §5 step 3 requires a bad-credential
    failure to name enough of the CSP's error to diagnose it and never
    the credential's value, and this suite's output lands in
    .aiform/testlog/."""
    bad_token = "dop_v1_intentionally_invalid_domain_system_test_token"
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", bad_token)

    state_path = project_dir / ".aiform" / "state.json"
    zone = unique_zone_name("bad-token")
    write_domain_aiform_md(project_dir, name=zone, records=BASE_RECORDS)

    code = cli.main(["plan", "apply", "--yes", "--state-file", str(state_path)])
    captured = capsys.readouterr()

    assert code == 2
    assert "Error:" in captured.err
    assert bad_token not in captured.out
    assert bad_token not in captured.err
    assert _resource_key(zone) not in state.load(state_path).resources


def test_existing_zone_is_neither_adopted_nor_rolled_back(project_dir, capsys):
    """Case 11: a `POST /v2/domains` that fails because the name is taken
    must NOT trigger create()'s rollback -- otherwise aiform deletes a
    zone it did not create.

    specs/digitalocean_domain.md states this as a hard requirement
    ("never triggers rollback ... rather than having aiform adopt -- or
    delete -- a zone it did not create"). It is a destructive-behavior
    claim, and no mock can settle it: the mock decides for itself what
    the second POST returns.

    Cleanup goes direct to the API, not through aiform -- the code under
    test is exactly what is suspect here.
    """
    token = live_token()
    if not token_has_domain_scope(token):
        pytest.skip("DIGITALOCEAN_TOKEN lacks `domain` scope")

    zone = unique_zone_name("already-exists")
    create_domain_directly(token, zone)
    try:
        state_path = project_dir / ".aiform" / "state.json"
        write_domain_aiform_md(project_dir, name=zone, records=BASE_RECORDS)

        code = cli.main(["plan", "apply", "--yes", "--state-file", str(state_path)])
        captured = capsys.readouterr()

        assert code == 2, (
            f"apply against an existing zone should fail, exited {code}\n{captured.out}"
        )
        assert "Error:" in captured.err
        assert _resource_key(zone) not in state.load(state_path).resources

        # The claim under test is create()'s own behavior, so assert it
        # against create() directly. The CLI path above cannot carry that
        # weight on its own: exit 2 with an Error: line is equally what
        # local validation, a declined gate #2 or a credential failure
        # produce, and in each of those create() is never entered while
        # the zone survives for the trivial reason that nothing touched
        # it. Nor is the CLI path deterministic -- an earlier version of
        # case 12 failed with "categorization returned 'update' but no
        # state entry is tracked for it", never reaching create() at all,
        # having passed the run before. A test whose subject is a driver
        # method must not be able to pass or fail on an LLM's wording;
        # case 9 sets the same precedent for delete().
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            do_domain.Driver().create(
                zone, {"records": BASE_RECORDS}, {"DIGITALOCEAN_TOKEN": token}
            )
        assert excinfo.value.code == 422, (
            f"expected DO to reject a duplicate zone name with 422, got {excinfo.value.code}"
        )

        survivor = get_domain_or_none(token, zone)
        assert survivor is not None, (
            f"aiform deleted the pre-existing zone {zone} it did not create -- "
            "create()'s rollback fired on a name-already-taken 422"
        )
    finally:
        delete_domain_directly(token, zone)


def test_record_failure_rolls_the_zone_back_leaving_no_orphan():
    """Case 12: a record-level 422 after the zone exists must roll the
    zone back, leaving nothing live and untracked.

    Provoked with an A record whose data is not an IP address -- a str,
    so _validate_record passes it through, and DO rejects it. This is the
    one case whose failure mode IS a leaked zone, so it carries its own
    direct-API sweep on top of the assertion.
    """
    token = live_token()
    if not token_has_domain_scope(token):
        pytest.skip("DIGITALOCEAN_TOKEN lacks `domain` scope")

    zone = unique_zone_name("rollback")
    try:
        # Driven against create() directly, not through `plan apply`, for
        # the reason spelled out in case 11: the CLI path failed here once
        # with "categorization returned 'update' but no state entry is
        # tracked for it" -- blocked before create() ran, on a run whose
        # predecessor had passed. Every assertion below would have been
        # satisfied by that outcome too (the zone is equally "gone" if it
        # was never created), so routing this through the model would mean
        # a regression removing the rollback could go unnoticed for runs
        # at a time.
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            do_domain.Driver().create(
                zone,
                {"records": [{"type": "A", "name": "@", "data": "not-an-ip", "ttl": TTL}]},
                {"DIGITALOCEAN_TOKEN": token},
            )
        assert excinfo.value.code == 422, (
            f"expected DO to reject a non-IP A record with 422, got {excinfo.value.code}"
        )

        orphan = wait_until_domain_gone(token, zone)
        assert orphan is None, (
            f"zone {zone} survived a failed create -- create()'s rollback did not run, "
            "leaving a live, untracked zone"
        )
    finally:
        delete_domain_directly(token, zone)
