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


class RedactedToken(str):
    """A `str` that carries a live credential but never renders it in a repr.

    pytest writes two things into the log verbatim on a failure: the
    operands of a failing assert, and **the arguments of every frame in
    the traceback**. The second one is the dangerous one here -- it
    needs no assert at all. `get_droplet_or_none()` re-raises any
    non-404 HTTPError, so a single transient DO 500 or timeout anywhere
    in a 7-minute run prints `token = 'dop_v1_...'` into
    .aiform/testlog/*.log. That is exactly how the real token leaked
    once already.

    Subclassing `str` keeps it usable everywhere a token is used
    (f-string interpolation into an Authorization header, dict values,
    equality) because those go through `__str__`/`__eq__`, while
    pytest's display path goes through `repr()` -- which this
    overrides. Redacting at the value means every call site is covered
    by construction, including ones added later that never think about
    this.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "<DIGITALOCEAN_TOKEN redacted>"


def live_token() -> RedactedToken:
    """The real DIGITALOCEAN_TOKEN, wrapped so it can't leak into a log.

    Always use this rather than reading os.environ directly in a test.
    """
    return RedactedToken(os.environ["DIGITALOCEAN_TOKEN"])


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
    token: str, droplet_id: str, *, timeout_seconds: int = 120, poll_seconds: int = 5
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

    Returns the droplet rather than raising so callers can assert on the
    result: passing `token` into an asserted expression puts the live
    credential into pytest's assertion-introspection output, which is
    written verbatim to .aiform/testlog/*.log.
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        droplet = get_droplet_or_none(token, droplet_id)
        if droplet is None:
            return None
        if time.monotonic() >= deadline:
            return droplet
        time.sleep(poll_seconds)


def list_account_ssh_key_fingerprints(token: str) -> list[str]:
    request = urllib.request.Request(
        f"{do_compute.BASE_URL}/account/keys",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=do_compute.REQUEST_TIMEOUT_SECONDS) as response:
        payload = json.loads(response.read())
    return [key["fingerprint"] for key in payload.get("ssh_keys", [])]
