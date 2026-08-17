import json
import os
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from aiform import cli
from drivers.digitalocean import compute as do_compute

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
    under test (orchestrator.py / the driver), unlike the independent
    sweep script that backstops the case where even this doesn't run --
    see specs/system_test.md's "Orphan cleanup" section."""
    try:
        yield
    finally:
        state_path = project_dir / ".aiform" / "state.json"
        if state_path.exists():
            cli.main(["plan", "destroy", "--yes", "--state-file", str(state_path)])


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


def list_account_ssh_key_fingerprints(token: str) -> list[str]:
    request = urllib.request.Request(
        f"{do_compute.BASE_URL}/account/keys",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=do_compute.REQUEST_TIMEOUT_SECONDS) as response:
        payload = json.loads(response.read())
    return [key["fingerprint"] for key in payload.get("ssh_keys", [])]
