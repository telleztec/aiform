import pytest

from aiform.exceptions import ResourceNotFoundError


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
