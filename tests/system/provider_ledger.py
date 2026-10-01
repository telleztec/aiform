# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

"""What the interrupt suite created at the provider, recorded as it learns of
it -- see specs/system_test_interrupt.md, "Teardown".

State alone is not enough: an interrupted create leaves a droplet no state
file knows about. Pure bookkeeping, no I/O, so the matching rules that decide
what teardown deletes are tested offline
(tests/test_system_interrupt_ledger.py).
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Ledger:
    droplet_ids: set[str] = field(default_factory=set)
    firewall_ids: set[str] = field(default_factory=set)
    droplet_names: set[str] = field(default_factory=set)
    firewall_names: set[str] = field(default_factory=set)

    def note_state(self, st: Any) -> None:
        for entry in st.resources.values():
            if entry.resource_type == "compute":
                self.droplet_ids.add(str(entry.id))
                self.droplet_names.add(entry.name)
            elif entry.resource_type == "firewall":
                self.firewall_ids.add(str(entry.id))
                self.firewall_names.add(entry.name)

    def note_response(self, body: dict | None) -> None:
        if not body:
            return
        for key, ids in (("droplet", self.droplet_ids), ("firewall", self.firewall_ids)):
            created = body.get(key)
            if isinstance(created, dict) and created.get("id") is not None:
                ids.add(str(created["id"]))

    def droplet_ids_to_delete(self, list_tagged: Callable[[], list[dict]]) -> set[str]:
        return self.droplet_ids | _ids_named(self.droplet_names, list_tagged)

    def firewall_ids_to_delete(self, list_all: Callable[[], list[dict]]) -> set[str]:
        return self.firewall_ids | _ids_named(
            self.firewall_names, list_all, deployment_prefixed=True
        )


def _ids_named(
    names: set[str], listing: Callable[[], list[dict]], *, deployment_prefixed: bool = False
) -> set[str]:
    if not names:
        return set()

    def matches(listed: object) -> bool:
        if listed in names:
            return True
        # Firewalls aiform creates are named `aiform-<deployment>-<name>` (#249).
        return deployment_prefixed and any(
            str(listed).startswith("aiform-") and str(listed).endswith("-" + n) for n in names
        )

    return {str(item["id"]) for item in listing() if matches(item.get("name"))}
