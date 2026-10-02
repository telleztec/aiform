# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

"""What a live fault stage needs to know about one provider -- see
specs/system_test_interrupt.md, "Interface".

A stage names no provider: it asks the profile which request creates the
resource, which one polls it, where the id sits in a response, how to make a
poll report "not ready", which driver function loops on the poll, and how
teardown lists what the suite made. DigitalOcean is the only implementation.
"""

from collections.abc import Callable
from dataclasses import dataclass

from tests.system.conftest import SYSTEM_TEST_TAG, list_droplets_tagged


@dataclass(frozen=True)
class ProviderProfile:
    name: str
    create: tuple[str, str]
    poll: tuple[str, str]
    resize: tuple[str, str, str]
    destroy: tuple[str, str]
    resource_id: Callable[[dict | None], str | None]
    list_owned: Callable[[str], list[dict]]
    not_ready: Callable[[dict], dict]
    poll_loop: str


def _droplet_id(body: dict | None) -> str | None:
    droplet = (body or {}).get("droplet")
    if isinstance(droplet, dict) and droplet.get("id") is not None:
        return str(droplet["id"])
    return None


def _droplet_not_ready(body: dict) -> dict:
    droplet = body.get("droplet")
    if not isinstance(droplet, dict):
        return body
    return {**body, "droplet": {**droplet, "status": "new"}}


DIGITALOCEAN = ProviderProfile(
    name="digitalocean",
    create=("POST", r"/v2/droplets$"),
    poll=("GET", r"/v2/droplets/\d+$"),
    resize=("POST", r"/v2/droplets/\d+/actions$", r'"type":\s*"resize"'),
    destroy=("DELETE", r"/v2/droplets/\d+$"),
    resource_id=_droplet_id,
    list_owned=lambda token: list_droplets_tagged(token, SYSTEM_TEST_TAG),
    not_ready=_droplet_not_ready,
    poll_loop="_poll_until",
)
