# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

"""What a live fault stage needs to know about one provider -- see
specs/system_test_interrupt.md, "Interface".

A stage names no provider: it asks the profile which request creates the
resource, which one polls it, where the id sits in a response, and how teardown
lists and deletes what the suite made. DigitalOcean is the only implementation.
"""

from collections.abc import Callable
from dataclasses import dataclass

from tests.system.conftest import (
    SYSTEM_TEST_TAG,
    destroy_droplet_or_shout,
    list_droplets_tagged,
)


@dataclass(frozen=True)
class ProviderProfile:
    name: str
    create: tuple[str, str]
    poll: tuple[str, str]
    resource_id: Callable[[dict | None], str | None]
    list_owned: Callable[[str], list[dict]]
    delete: Callable[[str, str], None]


def _droplet_id(body: dict | None) -> str | None:
    droplet = (body or {}).get("droplet")
    if isinstance(droplet, dict) and droplet.get("id") is not None:
        return str(droplet["id"])
    return None


DIGITALOCEAN = ProviderProfile(
    name="digitalocean",
    create=("POST", r"/v2/droplets$"),
    poll=("GET", r"/v2/droplets/\d+$"),
    resource_id=_droplet_id,
    list_owned=lambda token: list_droplets_tagged(token, SYSTEM_TEST_TAG),
    delete=lambda token, resource_id: destroy_droplet_or_shout(
        token, resource_id, f"droplet {resource_id}"
    ),
)
