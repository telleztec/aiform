import json
import os
import time
import urllib.error
import urllib.request
import warnings
from pathlib import Path

import pytest

from aiform import cli
from drivers.digitalocean import compute as do_compute

_SECRET_ENV_VARS = ("DIGITALOCEAN_TOKEN", "ANTHROPIC_API_KEY")


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


def _scrub(text: str) -> str:
    for var in _SECRET_ENV_VARS:
        value = os.environ.get(var)
        if value:
            text = text.replace(value, f"<{var} redacted>")
    return text


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    """Strip live credentials out of any failure report before it is written.

    RedactedSecret covers the credentials *this suite* passes around, but not
    the one the code under test resolves for itself: cli.main() reads
    os.environ via config.resolve_credentials(), stores a plain str in
    PlannedResource.credentials, and hands it to apply_plan(planned,
    ...) as a frame argument. cli.main only catches _HANDLED_EXCEPTIONS,
    so anything else -- an anthropic 529 on the gate #2 review call is
    the realistic one on a 7-minute live run -- propagates out as a
    traceback, and pytest prints that frame's arguments verbatim,
    credentials dict included.

    No value-level wrapper can reach that path, because the value is
    constructed inside the code under test. Scrubbing the rendered
    report is the layer that actually covers every source at once, so
    this is the backstop rather than the first line of defence
    (RedactedSecret still keeps the token out of the common assert-
    introspection path, and keeps it readable when it does show).

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


def write_aiform_md(
    project_dir: Path,
    *,
    name: str = "aiform-system-test-droplet",
    region: str = REGION,
    size: str = SIZE,
    image: str = IMAGE,
    ssh_keys: list[str] | None = None,
    filename: str = "compute.aiform.md",
) -> Path:
    params_lines = [
        f"  region: {region}",
        f"  size: {size}",
        f"  image: {image}",
        "  tags:",
        f'    - "{SYSTEM_TEST_TAG}"',
    ]
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
                    f"teardown 'plan destroy' exited {code} -- a droplet may still be live "
                    f"and billable; check {state_path} and DigitalOcean's droplet list "
                    f"(tag {SYSTEM_TEST_TAG!r}) by hand",
                    stacklevel=2,
                )


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
        except (urllib.error.URLError, TimeoutError) as exc:
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
