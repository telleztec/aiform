# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

import pytest

from aiform.exceptions import DriverExecutionError, PlanBlockedError, ResourceNotFoundError


class TestResourceNotFoundError:
    def test_raisable_with_message(self):
        with pytest.raises(ResourceNotFoundError, match="droplet 123 not found"):
            raise ResourceNotFoundError("droplet 123 not found")

    def test_is_not_a_lookup_error(self):
        assert not issubclass(ResourceNotFoundError, LookupError)

    def test_does_not_collide_with_key_error(self):
        with pytest.raises(KeyError):
            try:
                {}["missing"]
            except ResourceNotFoundError:
                pytest.fail("KeyError should not be caught as ResourceNotFoundError")


class TestDriverExecutionError:
    def test_stores_fields_verbatim(self):
        original = RuntimeError("connection reset")
        exc = DriverExecutionError("digitalocean", "compute", "read", original)

        assert exc.provider == "digitalocean"
        assert exc.resource_type == "compute"
        assert exc.operation == "read"
        assert exc.original is original

    def test_message_names_provider_resource_type_and_operation(self):
        original = RuntimeError("connection reset")
        exc = DriverExecutionError("digitalocean", "compute", "create", original)

        message = str(exc)
        assert "digitalocean.compute" in message
        assert "create" in message
        assert "connection reset" in message

    def test_is_a_plain_exception(self):
        assert issubclass(DriverExecutionError, Exception)
        assert not issubclass(DriverExecutionError, LookupError)


class TestPlanBlockedError:
    def test_stores_reason(self):
        exc = PlanBlockedError("driver drivers/digitalocean/compute.py failed gate #1 review")
        assert exc.reason == "driver drivers/digitalocean/compute.py failed gate #1 review"

    def test_str_is_exactly_the_reason(self):
        exc = PlanBlockedError("no driver found for (aws, compute)")
        assert str(exc) == "no driver found for (aws, compute)"

    def test_is_a_plain_exception(self):
        assert issubclass(PlanBlockedError, Exception)
