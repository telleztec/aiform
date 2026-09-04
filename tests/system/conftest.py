# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

import http.client
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import warnings
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aiform import cli, config, orchestrator
from drivers.digitalocean import compute as do_compute

_SECRET_ENV_VARS = ("DIGITALOCEAN_TOKEN", "ANTHROPIC_API_KEY")

# Snapshotted at import, before any test can monkeypatch these. Reading
# os.environ at report time instead would mean a test that patches a
# secret var (test_bad_token_... sets a bogus DIGITALOCEAN_TOKEN) leaves
# the *real* value unscrubbable for its duration -- silently disabling
# the backstop for exactly the tests most likely to touch credentials.
_SECRETS = {var: os.environ.get(var) for var in _SECRET_ENV_VARS}


class RedactedSecret(str):
    """A `str` that carries a live credential but never renders it in a repr.

    pytest writes two things into the log verbatim on a failure: the
    operands of a failing assert, and **the arguments of every frame in
    the traceback**. The second one is the dangerous one here -- it
    needs no assert at all. `get_droplet_or_none()` re-raises any
    non-404 HTTPError, so a single transient DO 500 or timeout anywhere
    in a 7-minute run prints `token = 'dop_v1_...'` into
    .aiform/testlog/*.log. That is exactly how the real token leaked
    once already.

    Subclassing `str` keeps it usable everywhere a credential is used
    (f-string interpolation into an Authorization header, dict values,
    equality) because those go through `__str__`/`__eq__`, while
    pytest's display path goes through `repr()` -- which this
    overrides.

    The env var name travels with the value rather than being baked into
    `__repr__`, so the marker always names the credential actually
    wrapped. A hardcoded name would mislabel every secret except one --
    and a log claiming `<DIGITALOCEAN_TOKEN redacted>` where an Anthropic
    key appeared sends whoever is triaging the leak after the wrong
    credential, which is worse than an unlabelled marker.

    No `__slots__`: `str` is a variable-length built-in, so a nonempty
    `__slots__` on a subclass is a `TypeError`.
    """

    def __new__(cls, value: str, var_name: str) -> "RedactedSecret":
        secret = super().__new__(cls, value)
        secret.var_name = var_name
        return secret

    def __repr__(self) -> str:
        return f"<{self.var_name} redacted>"


def _redacted_env(var: str) -> RedactedSecret:
    """Read `var` and wrap it labelled with that same name.

    Reading and labelling in one place is what keeps the two from
    drifting -- a caller cannot pass a name that doesn't match the value.
    """
    return RedactedSecret(os.environ[var], var)


def live_token() -> RedactedSecret:
    """The real DIGITALOCEAN_TOKEN, wrapped so it can't leak into a log.

    Always use this rather than reading os.environ directly in a test.
    Note this only covers credentials *this suite* holds -- the code
    under test resolves its own; see the scrubbing hook below.
    """
    return _redacted_env("DIGITALOCEAN_TOKEN")


# A secret rarely reaches a report intact. pytest renders frame
# arguments through saferepr, which truncates each repr at 240 chars by
# eliding the MIDDLE -- so a token inside a long `planned=[...]` repr
# arrives split into a head and a tail, and an exact-value replace()
# matches neither. Measured on the real thing: 58 of a 71-char token
# written to the log with the exact-match scrub already in place.
# Fragments are therefore matched too, longest-first. 12 is short enough
# that no realistic truncation leaves a usable remnant, and long enough
# that a false positive would require unrelated text to contain a
# 12-character run of the actual secret.
_MIN_LEAKED_FRAGMENT = 12


def _scrub(text: str) -> str:
    for var, value in _SECRETS.items():
        if not value:
            continue
        marker = f"<{var} redacted>"
        text = text.replace(value, marker)
        for length in range(len(value) - 1, _MIN_LEAKED_FRAGMENT - 1, -1):
            for start in range(len(value) - length + 1):
                fragment = value[start : start + length]
                if fragment in text:
                    text = text.replace(fragment, marker)
    return text


@pytest.fixture(autouse=True)
def _redact_resolved_credentials(monkeypatch):
    """Wrap the credentials the *code under test* resolves for itself.

    The scrubbing hook is a backstop on rendered text; this stops the
    plaintext from ever reaching a repr in the first place, which is the
    more reliable half. config.resolve_credentials() is the single point
    where aiform turns an env var into a live credential, and its return
    value is what ends up in PlannedResource.credentials and therefore
    in apply_plan()'s frame arguments.

    RedactedSecret is a str subclass, so the code under test is
    unaffected -- it interpolates into an Authorization header exactly
    as before. What changes is that the repr is a short marker, which
    also means saferepr truncation has nothing to split.
    """
    real_resolve = config.resolve_credentials

    # *args/**kwargs rather than (provider): the real signature also takes
    # credentials_path, and a wrapper that silently narrowed it would
    # TypeError on any call site that passes it.
    def resolve_redacted(*args, **kwargs) -> dict[str, str]:
        return {
            var: RedactedSecret(value, var) for var, value in real_resolve(*args, **kwargs).items()
        }

    monkeypatch.setattr(config, "resolve_credentials", resolve_redacted)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    """Strip live credentials out of any failure report before it is written.

    Last of three layers, and the only one that needs no cooperation
    from whoever wrote the code that leaked:

    1. `live_token()` wraps what this suite passes around.
    2. `_redact_resolved_credentials` wraps what the code under test
       resolves for itself -- the path that actually matters, since
       cli.main() only catches _HANDLED_EXCEPTIONS and anything else (an
       anthropic 529 on the gate #2 review call is the realistic one on
       a 7-minute live run) propagates out with apply_plan(planned, ...)
       on the stack, whose frame arguments pytest prints verbatim.
    3. This hook, covering anything the first two miss -- a credential
       from a source neither anticipated, or one that reaches the report
       through captured output rather than a frame argument.

    Layer 2 makes layer 3 mostly redundant for the known path, which is
    the point: the fix that failed twice was the one relying on a single
    layer being complete.

    Replacing longrepr with a plain string costs syntax highlighting and
    the reprcrash the "short test summary info" line is built from (that
    line falls back to showing the first source line rather than the
    exception message). The full traceback in the FAILURES section is
    unaffected, which is the part used to diagnose. That trade only
    applies to a report that genuinely carried a secret -- every other
    failure renders exactly as before.
    """
    report = yield

    if report.longrepr is not None:
        rendered = str(report.longrepr)
        scrubbed = _scrub(rendered)
        if scrubbed != rendered:
            report.longrepr = scrubbed

    if report.sections:
        report.sections = [(name, _scrub(content)) for name, content in report.sections]

    return report


SYSTEM_TEST_TAG = "aiform-system-test"
REGION = "sfo3"
ALTERNATE_REGION = "nyc3"
IMAGE = "ubuntu-24-04-x64"
SIZE = "s-1vcpu-512mb-10gb"
ALTERNATE_SIZE = "s-1vcpu-1gb"


@pytest.fixture(scope="session", autouse=True)
def _require_live_credentials():
    if not (os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("DIGITALOCEAN_TOKEN")):
        pytest.skip(
            "tests/system requires both ANTHROPIC_API_KEY and DIGITALOCEAN_TOKEN set in the "
            "environment -- see specs/system_test.md"
        )


@pytest.fixture
def project_dir(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


def unique_name(base: str) -> str:
    """Append a run-scoped suffix to a fixture's base droplet name.

    Two invocations of this suite can run in parallel (a local run
    overlapping a CI run, or two CI jobs) and, before this, raced on the
    literal same DO droplet name -- not a state-file collision (each run
    already gets its own tmp_path/state.json), but a real, confusing
    collision in DigitalOcean's own droplet list. The timestamp -- same
    %Y%m%dT%H%M%SZ format scripts/run_system_tests.py already uses for
    its log filenames -- makes a leaked droplet's age visible from its
    name alone; the short random suffix covers two runs starting within
    the same second, which the timestamp alone would still collide on.
    """
    now = datetime.now(UTC)
    return f"{base}-{now:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"


def write_aiform_md(
    project_dir: Path,
    *,
    name: str | None = None,
    region: str = REGION,
    size: str = SIZE,
    image: str = IMAGE,
    ssh_keys: list[str] | None = None,
    extra_tags: list[str] | None = None,
    filename: str = "compute.aiform.md",
) -> Path:
    # A plain string default would be evaluated once, at def time, and
    # then shared by every caller that omits name= -- silently
    # reintroducing the cross-run DO-namespace collision unique_name()
    # exists to prevent. None + a per-call fallback keeps
    # specs/system_test.md's Isolation guarantee universal, not just
    # true of today's five call sites (which all pass name= explicitly).
    name = name or unique_name("aiform-system-test-droplet")
    params_lines = [
        f"  region: {region}",
        f"  size: {size}",
        f"  image: {image}",
        "  tags:",
        f'    - "{SYSTEM_TEST_TAG}"',
    ]
    # SYSTEM_TEST_TAG always stays first and is never optional -- the
    # orphan sweep in specs/system_test.md keys off it, so a droplet
    # this helper writes must remain findable however the caller
    # customizes the rest of the tag list.
    params_lines.extend(f'    - "{tag}"' for tag in extra_tags or [])
    if ssh_keys:
        params_lines.append("  ssh_keys:")
        params_lines.extend(f'    - "{key}"' for key in ssh_keys)

    content = (
        "---\n"
        "resource: compute\n"
        f"name: {name}\n"
        "provider: digitalocean\n"
        "params:\n" + "\n".join(params_lines) + "\n"
        "---\n\n"
        "## Intent\n\n"
        "Ephemeral droplet created by aiform's live system test suite "
        f"(tag {SYSTEM_TEST_TAG!r}); safe to destroy at any time.\n"
    )
    path = project_dir / filename
    path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture
def teardown_tracked_resources(project_dir: Path):
    """Primary cleanup path for a live-created droplet: drives the real
    `aiform plan destroy` in a finally clause so a droplet is torn down
    even when an assertion mid-test raises. This depends on the code
    under test (orchestrator.py / the driver) and only runs if the test
    process survives long enough to reach it -- specs/system_test.md's
    "Orphan cleanup" section specs an independent, non-aiform sweep
    script as the backstop for the case where even this doesn't run;
    that script is not implemented yet (tracked as separate follow-up
    work, out of scope for this suite)."""
    try:
        yield
    finally:
        state_path = project_dir / ".aiform" / "state.json"
        if state_path.exists():
            code = cli.main(["plan", "destroy", "--yes", "--state-file", str(state_path)])
            if code != 0:
                warnings.warn(
                    f"teardown 'plan destroy' exited {code} -- a resource may still be live; "
                    f"check {state_path}, DigitalOcean's droplet list (tag "
                    f"{SYSTEM_TEST_TAG!r}) and its domain list "
                    f"({SYSTEM_TEST_ZONE_PREFIX}*.{SYSTEM_TEST_ZONE_PARENT}) by hand. "
                    "Zones carry no tag, so the name prefix is the only handle for those.",
                    stacklevel=2,
                )


def assert_cli_ok(code: int, captured, step: str) -> None:
    """Assert a CLI invocation exited 0, surfacing its stderr when it
    didn't. Every failure mode these suites exist to catch (a gate #2
    plan-review block, a DriverExecutionError from the live DO API, a
    credential resolution failure) reports itself only through the
    `Error: ...` line cli.main() prints to stderr before returning 2 --
    and capsys.readouterr() has already consumed that by the time a bare
    `assert code == 0` fires, so pytest's own capture sections show
    nothing. Without this the log says `assert 2 == 0` and nothing else,
    which is not enough to diagnose a multi-minute live run."""
    assert code == 0, (
        f"{step} exited {code}\n--- stderr ---\n{captured.err}\n--- stdout ---\n{captured.out}"
    )


def count_driver_reads(monkeypatch) -> list[str]:
    """Record every driver.read() the orchestrator performs.

    orchestrator.load_driver() execs the driver module fresh via
    importlib.util.spec_from_file_location on every call, never caching
    it in sys.modules (tests/test_orchestrator.py's
    test_each_call_returns_a_fresh_instance) -- so a statically imported
    Driver class is never the same object orchestrator.py actually
    instantiates. Wrap the instance load_driver() itself returns instead.
    """
    calls: list[str] = []
    real_load_driver = orchestrator.load_driver

    def counting_load_driver(provider, resource_type):
        driver = real_load_driver(provider, resource_type)
        real_read = driver.read

        def counting_read(id, credentials):
            calls.append(id)
            return real_read(id, credentials)

        driver.read = counting_read
        return driver

    monkeypatch.setattr(orchestrator, "load_driver", counting_load_driver)
    return calls


def get_droplet_or_none(token: str, droplet_id: str) -> dict | None:
    request = urllib.request.Request(
        f"{do_compute.BASE_URL}/droplets/{droplet_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(
            request, timeout=do_compute.REQUEST_TIMEOUT_SECONDS
        ) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    return payload["droplet"]


def wait_until_droplet_gone(
    token: str, droplet_id: str, *, timeout_seconds: int = 300, poll_seconds: int = 5
) -> dict | None:
    """Poll until `droplet_id` 404s. Returns None once it's gone, or the
    still-live droplet if it outlasts the timeout.

    DigitalOcean's `DELETE /v2/droplets/{id}` is asynchronous: it returns
    204 as soon as the teardown is *accepted*, and the droplet keeps
    answering GET for a while afterwards. The driver's delete() is right
    to treat 204 as done (exactly one API call, per
    specs/digitalocean_compute.md) -- it's this suite's
    "is it actually gone" check that has to tolerate the lag, or it
    races DO's own convergence and fails a destroy that in fact worked.

    The deadline is deliberately generous. All we actually know about
    DO's convergence time is one observation -- a droplet still live at
    the moment of the check and confirmed gone "minutes later" -- which
    bounds it from *above*, not below. A too-short deadline reproduces
    the exact failure this helper exists to prevent, and costs nothing
    on the happy path since the loop returns on the first 404.

    Transient errors do not end the poll, for the same reason:
    get_droplet_or_none() re-raises every non-404, so without this a
    single DO 500/429 or socket timeout on any one of ~60 GETs would
    fail a destroy that had already succeeded. An error is only
    surfaced if the deadline passes without ever observing a 404.

    Returns the droplet rather than raising so callers can assert on the
    result: passing `token` into an asserted expression puts the live
    credential into pytest's assertion-introspection output.
    """
    deadline = time.monotonic() + timeout_seconds
    last_droplet: dict | None = None
    last_error: Exception | None = None

    while True:
        try:
            droplet = get_droplet_or_none(token, droplet_id)
        except (
            urllib.error.URLError,
            TimeoutError,
            http.client.HTTPException,
            OSError,
            json.JSONDecodeError,
        ) as exc:
            # Deliberately wide. urllib only wraps OSError from the
            # request itself into URLError -- anything raised by
            # getresponse() or response.read() (RemoteDisconnected,
            # IncompleteRead) propagates unwrapped, and a truncated body
            # surfaces as JSONDecodeError. Those are the *likely*
            # transient failures across ~60 GETs, so a narrow clause
            # would abort on precisely the errors this loop exists to
            # ride out.
            last_error = exc
        else:
            if droplet is None:
                return None
            last_droplet, last_error = droplet, None

        if time.monotonic() >= deadline:
            if last_error is not None:
                raise last_error
            return last_droplet

        time.sleep(poll_seconds)


def list_account_ssh_key_fingerprints(token: str) -> list[str]:
    request = urllib.request.Request(
        f"{do_compute.BASE_URL}/account/keys",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=do_compute.REQUEST_TIMEOUT_SECONDS) as response:
        payload = json.loads(response.read())
    return [key["fingerprint"] for key in payload.get("ssh_keys", [])]


# --------------------------------------------------------------------------
# digitalocean/domain -- see specs/system_test_domain.md
# --------------------------------------------------------------------------

# Spelled out rather than reused from drivers.digitalocean.domain. The
# helpers below are the independent backstop for the domain suite, and
# specs/system_test.md's "the backstop must not depend on the code under
# test" applies most sharply to the sweep: if delete() is what's broken,
# routing the cleanup through the same module disables both layers at
# once. (get_droplet_or_none above borrows do_compute's constants, which
# predates that rule being written down and is a constant rather than
# logic -- not a precedent to extend here.)
DO_API_BASE = "https://api.digitalocean.com/v2"
# The only host a paginated `next` may point at, and a ceiling on how many
# pages will be followed. See list_domains().
DO_API_BASE_HOST = "https://api.digitalocean.com"
DO_API_MAX_PAGES = 100
DO_API_TIMEOUT_SECONDS = 30

SYSTEM_TEST_ZONE_PREFIX = "systest-"
SYSTEM_TEST_ZONE_PARENT = "telleztec.com"
# Lowercase 't'/'z' to match what unique_zone_name() emits and what
# DigitalOcean stores (it folds a zone name's case -- a requested
# ...T000759Z... came back as ...t000759z...). Note this is cosmetic
# rather than load-bearing on its own: strptime matches format literals
# case-INsensitively, so the uppercase spelling would parse either
# spelling too. Keeping generation and parsing in one case is what makes
# "the name we asked for is the name DO reports" hold exactly, which is
# what any direct name comparison against DO's listing relies on.
ZONE_TIMESTAMP_FORMAT = "%Y%m%dt%H%M%Sz"
_ZONE_TIMESTAMP_LENGTH = 16
# Mirrors specs/system_test.md's droplet sweep default, for the same
# reason: this suite never legitimately runs more than a few minutes, so
# a threshold well above that can never race a healthy concurrent run
# while still catching a real leak promptly.
SWEEP_MIN_AGE_MINUTES = 60

# Everything the sweep must survive rather than raise on. Deliberately as
# wide as wait_until_domain_gone()'s: http.client.HTTPException is listed
# explicitly because IncompleteRead and RemoteDisconnected are NOT
# OSError subclasses and would otherwise escape. HTTPError is a URLError
# subclass, so a 403 from a droplet-scoped token is covered.
_SWEEP_TRANSIENT_ERRORS = (
    urllib.error.URLError,
    TimeoutError,
    http.client.HTTPException,
    OSError,
    json.JSONDecodeError,
)


def unique_zone_name(label: str) -> str:
    """A throwaway zone name, unique per run and safe for the sweep.

    Three properties, each load-bearing (see specs/system_test_domain.md's
    "Zone naming"): lowercased so requested == stored; a fixed literal
    prefix so the timestamp sits at a known offset; and a subdomain of a
    zone the account already owns, so every name created lives in a
    namespace the operator controls. A child zone is an independent
    object -- creating one does not touch the parent's records.
    """
    stem = unique_name(SYSTEM_TEST_ZONE_PREFIX.rstrip("-"))
    return f"{stem}-{label}.{SYSTEM_TEST_ZONE_PARENT}".lower()


def zone_created_at(zone_name: str) -> datetime | None:
    """The creation time encoded in a zone name, or None if it isn't ours.

    None means "don't touch it": the sweep deletes only zones it can
    positively identify, so an unrecognized name is a leak someone
    notices rather than something this code removes.
    """
    if not zone_name.startswith(SYSTEM_TEST_ZONE_PREFIX):
        return None
    if not zone_name.endswith(f".{SYSTEM_TEST_ZONE_PARENT}"):
        return None
    stamp = zone_name[len(SYSTEM_TEST_ZONE_PREFIX) :][:_ZONE_TIMESTAMP_LENGTH]
    try:
        return datetime.strptime(stamp, ZONE_TIMESTAMP_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def write_domain_aiform_md(
    project_dir: Path,
    *,
    name: str,
    records: list[dict],
    filename: str = "domain.aiform.md",
) -> Path:
    """The `resource: domain` parallel to write_aiform_md().

    `records` is serialized verbatim -- caller's order, caller's exact
    field set, no normalization. Cases 6 and 7d exist precisely to
    observe what the driver does with a given spelling and ordering, so a
    helper that tidied either would defeat them.

    Each record is emitted as a JSON object, which is valid YAML flow
    style and gets the escaping right for free. That matters for the TXT
    record whose own data contains quotes -- hand-rolled quoting is
    exactly where a fixture like this silently corrupts the value it
    claims to be testing.
    """
    record_lines = "\n".join(f"    - {json.dumps(record)}" for record in records)
    params_block = "  records: []\n" if not records else f"  records:\n{record_lines}\n"
    content = (
        "---\n"
        "resource: domain\n"
        f"name: {name}\n"
        "provider: digitalocean\n"
        "params:\n" + params_block + "---\n\n"
        "## Intent\n\n"
        "Ephemeral DNS zone created by aiform's live system test suite; "
        "safe to destroy at any time.\n"
    )
    path = project_dir / filename
    path.write_text(content, encoding="utf-8")
    return path


def _domain_api(token: str, method: str, url: str, body: dict | None = None):
    data = None
    headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=DO_API_TIMEOUT_SECONDS) as response:
        raw = response.read()
    return json.loads(raw) if raw else None


def token_has_domain_scope(token: str) -> bool:
    """Whether this token can read the domain API at all.

    `aiform init`'s preflight probes GET /v2/droplets only, so a
    droplet-scoped token earns a green [✓] and then fails at the first
    domain apply (specs/digitalocean_domain.md). The domain suite skips
    on a False here rather than failing.
    """
    try:
        _domain_api(token, "GET", f"{DO_API_BASE}/domains?per_page=1")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return False
        raise
    return True


def get_domain_or_none(token: str, zone: str) -> dict | None:
    try:
        payload = _domain_api(token, "GET", f"{DO_API_BASE}/domains/{zone}")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    if payload is None:
        raise AssertionError(f"empty 200 body from GET /v2/domains/{zone}")
    return payload["domain"]


def list_domain_records(token: str, zone: str) -> list[dict]:
    """Every record in `zone`, unfiltered -- SOA and DO-managed apex NS
    included. The driver's read() filters those out; this returns the raw
    listing so a test can assert they really are present on DO's side and
    really are absent from state."""
    payload = _domain_api(token, "GET", f"{DO_API_BASE}/domains/{zone}/records?per_page=200")
    if payload is None:
        raise AssertionError(f"empty 200 body from GET /v2/domains/{zone}/records")
    return payload.get("domain_records", [])


def list_domains(token: str) -> list[dict]:
    """Every zone on the account, following pagination.

    GET /v2/domains defaults to per_page=20 and this account hosts real
    zones alongside the suite's throwaways, so an unpaginated read could
    miss a leaked zone entirely -- the one failure this listing exists to
    catch.

    `next` comes out of a response *body*, and this loop sends the live
    DIGITALOCEAN_TOKEN to whatever it names -- so the host is checked
    before it is followed, and the page count is capped. Both guards
    mirror drivers/digitalocean/_common.py's `_check_host`/`MAX_PAGES`
    and are deliberately re-implemented rather than imported, for the
    same reason the rest of these helpers are: this is the backstop, and
    it must not depend on the code it backs up. Re-implementing a
    security guard means re-implementing *all* of it -- an earlier
    version of this function copied the pagination and silently dropped
    both, which is a token-exfiltration path, not a style nit.
    """
    domains: list[dict] = []
    url = f"{DO_API_BASE}/domains?per_page=200"
    pages = 0
    while url:
        parts = urllib.parse.urlsplit(url)
        if f"{parts.scheme}://{parts.netloc}" != DO_API_BASE_HOST:
            raise AssertionError(f"refusing to follow a next url off {DO_API_BASE_HOST}: {url}")
        payload = _domain_api(token, "GET", url)
        pages += 1
        if payload is None:
            break
        domains.extend(payload.get("domains") or [])
        url = ((payload.get("links") or {}).get("pages") or {}).get("next")
        if url and pages >= DO_API_MAX_PAGES:
            raise AssertionError(
                f"listing domains exceeded {DO_API_MAX_PAGES} pages; refusing to keep following"
            )
    return domains


def create_domain_directly(token: str, zone: str) -> dict:
    payload = _domain_api(token, "POST", f"{DO_API_BASE}/domains", body={"name": zone})
    return payload["domain"]


def delete_domain_directly(token: str, zone: str) -> None:
    try:
        _domain_api(token, "DELETE", f"{DO_API_BASE}/domains/{zone}")
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise


def wait_until_domain_gone(
    token: str, zone: str, *, timeout_seconds: int = 120, poll_seconds: int = 3
) -> dict | None:
    """Poll until `zone` 404s. Returns None once gone, or the still-live
    zone if it outlasts the timeout.

    Mirrors wait_until_droplet_gone()'s contract, including returning
    rather than raising so a caller's assertion never puts `token` into
    pytest's assertion-introspection output. DO's zone delete looks
    synchronous, unlike its droplet delete -- but a suite that races a
    provider's convergence fails a destroy that in fact worked, and the
    loop costs nothing on the happy path since it returns on the first
    404. Transient errors do not end the poll, for the same reason they
    don't there.
    """
    deadline = time.monotonic() + timeout_seconds
    last_domain: dict | None = None
    last_error: Exception | None = None

    while True:
        try:
            domain = get_domain_or_none(token, zone)
        except (
            urllib.error.URLError,
            TimeoutError,
            http.client.HTTPException,
            OSError,
            json.JSONDecodeError,
        ) as exc:
            last_error = exc
        else:
            if domain is None:
                return None
            last_domain, last_error = domain, None

        if time.monotonic() >= deadline:
            if last_error is not None:
                raise last_error
            return last_domain

        time.sleep(poll_seconds)


@pytest.fixture(scope="session", autouse=True)
def _sweep_leaked_system_test_zones(_require_live_credentials):
    """Independent backstop for zones the per-test teardown never reached.

    specs/system_test.md's orphan sweep keys on the `aiform-system-test`
    tag; that does not transfer, because DigitalOcean has no tagging API
    for domains (specs/digitalocean_domain.md records this as impossible,
    not merely unimplemented). The zone name is the only handle, which is
    why unique_zone_name()'s shape is part of the safety mechanism.

    A zone is deleted only if all three hold: the literal `systest-`
    prefix, the `.telleztec.com` suffix, and an encoded creation time at
    least SWEEP_MIN_AGE_MINUTES old. The account's real zones fail the
    first two independently -- `telleztec.com` itself is excluded twice
    over, not once -- and an unparseable name is skipped, never deleted.

    The age threshold is what keeps this from deleting a *concurrent*
    run's live zones: "older than this session started" would do exactly
    that to a run that began a minute earlier. Same reasoning, and the
    same 60-minute default, as specs/system_test.md's droplet sweep --
    this suite never legitimately runs for more than a few minutes, so a
    threshold well above that cannot race a healthy run.

    Runs after the session, so a crash mid-run is cleaned up by the
    *next* run rather than never; the per-test teardown handles the
    ordinary case.
    """
    yield

    # live_token(), not os.environ directly: this is the one code path
    # that runs on every session, and a URLError/timeout inside
    # _domain_api() would otherwise put the raw token into the teardown
    # traceback's frame arguments. The scrub hook would still catch it,
    # but the whole point of the three layers is that none of them is
    # relied on alone. No presence check is needed -- this fixture
    # depends on _require_live_credentials, which has already skipped the
    # session if either variable is unset.
    token = live_token()

    # This fixture is autouse for the whole of tests/system/, so it runs
    # on a compute-only session too -- where the token may legitimately
    # carry no `domain` scope. Listing would then 403 and turn a green
    # droplet run into a session-teardown ERROR, failing a suite that has
    # nothing to do with domains.
    #
    # Every *transient* failure here warns rather than raises: this is a
    # best-effort backstop for a leak that has already happened, and it
    # must never be the thing that fails an otherwise-passing run. That
    # applies to the per-zone DELETE as much as to the listing -- an
    # earlier version guarded the delete for HTTPError only, so a socket
    # timeout on one DELETE raised out of the fixture and aborted the
    # rest of the sweep, which is exactly the promise this docstring
    # makes and that version broke.
    #
    # Two things deliberately DO raise, and are not in
    # _SWEEP_TRANSIENT_ERRORS: list_domains()'s refusal to follow an
    # off-host `next`, and its page ceiling. Those are security refusals,
    # not transient errors -- a body trying to redirect the bearer token
    # should fail the run loudly rather than degrade to a warning nobody
    # reads.
    try:
        domains = list_domains(token)
    except _SWEEP_TRANSIENT_ERRORS as exc:
        warnings.warn(
            f"could not list domains to sweep leaked system-test zones ({exc}) -- "
            "check by hand for zones named "
            f"{SYSTEM_TEST_ZONE_PREFIX}*.{SYSTEM_TEST_ZONE_PARENT}",
            stacklevel=2,
        )
        return

    cutoff = datetime.now(UTC) - timedelta(minutes=SWEEP_MIN_AGE_MINUTES)
    swept = []
    for domain in domains:
        name = domain.get("name", "")
        created = zone_created_at(name)
        if created is None or created > cutoff:
            continue
        try:
            delete_domain_directly(token, name)
        except _SWEEP_TRANSIENT_ERRORS as exc:
            warnings.warn(f"could not sweep leaked zone {name!r}: {exc}", stacklevel=2)
        else:
            swept.append(name)

    if swept:
        # Always a bug report, never routine maintenance: every hit means
        # the per-test teardown failed to clean up after itself.
        warnings.warn(
            f"swept {len(swept)} leaked system-test zone(s) left by an earlier run: "
            f"{swept} -- the per-test teardown did not run to completion",
            stacklevel=2,
        )
