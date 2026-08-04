import pytest

from aiform.exceptions import ResourceNotFoundError


class TestResourceNotFoundError:
    def test_is_exception_subclass(self):
        assert issubclass(ResourceNotFoundError, Exception)

    def test_is_not_a_lookup_error(self):
        assert not issubclass(ResourceNotFoundError, LookupError)

    def test_raisable_and_catchable(self):
        with pytest.raises(ResourceNotFoundError):
            raise ResourceNotFoundError("droplet 123 not found")

    def test_message_preserved(self):
        try:
            raise ResourceNotFoundError("droplet 123 not found")
        except ResourceNotFoundError as e:
            assert str(e) == "droplet 123 not found"

    def test_does_not_collide_with_key_error(self):
        with pytest.raises(KeyError):
            try:
                {}["missing"]
            except ResourceNotFoundError:
                pytest.fail("KeyError should not be caught as ResourceNotFoundError")
