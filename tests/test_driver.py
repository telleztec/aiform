import pytest

from aiform.driver import DriverUpdateNotSupported, ResourceDriver


class FullDriver(ResourceDriver):
    PARAM_SCHEMA = {"type": "object", "properties": {}}

    def create(self, name, params, credentials):
        return {"id": "1"}

    def read(self, id, credentials):
        return {"id": id}

    def update(self, id, current, desired, credentials):
        return {"id": id}

    def delete(self, id, credentials):
        return None


class MissingDeleteDriver(ResourceDriver):
    PARAM_SCHEMA = {"type": "object", "properties": {}}

    def create(self, name, params, credentials):
        return {"id": "1"}

    def read(self, id, credentials):
        return {"id": id}

    def update(self, id, current, desired, credentials):
        return {"id": id}


class NoParamSchemaDriver(ResourceDriver):
    def create(self, name, params, credentials):
        return {"id": "1"}

    def read(self, id, credentials):
        return {"id": id}

    def update(self, id, current, desired, credentials):
        return {"id": id}

    def delete(self, id, credentials):
        return None


class LikelyReplaceDriver(FullDriver):
    LIKELY_REPLACE_FIELDS = ["image", "region"]


class NonDiffableFieldsDriver(FullDriver):
    NON_DIFFABLE_FIELDS = ["ssh_keys"]


class TestResourceDriverIsAbstract:
    def test_cannot_instantiate_base_class_directly(self):
        with pytest.raises(TypeError):
            ResourceDriver()

    def test_concrete_subclass_can_be_instantiated(self):
        driver = FullDriver()
        assert isinstance(driver, ResourceDriver)

    def test_subclass_missing_one_method_cannot_be_instantiated(self):
        with pytest.raises(TypeError):
            MissingDeleteDriver()


class TestLikelyReplaceFields:
    def test_defaults_to_empty_list_when_not_overridden(self):
        driver = FullDriver()
        assert driver.LIKELY_REPLACE_FIELDS == []

    def test_subclass_can_override(self):
        driver = LikelyReplaceDriver()
        assert driver.LIKELY_REPLACE_FIELDS == ["image", "region"]


class TestNonDiffableFields:
    def test_defaults_to_empty_list_when_not_overridden(self):
        driver = FullDriver()
        assert driver.NON_DIFFABLE_FIELDS == []

    def test_subclass_can_override(self):
        driver = NonDiffableFieldsDriver()
        assert driver.NON_DIFFABLE_FIELDS == ["ssh_keys"]


class TestParamSchema:
    def test_subclass_can_set_param_schema(self):
        driver = FullDriver()
        assert driver.PARAM_SCHEMA == {"type": "object", "properties": {}}

    def test_omitted_param_schema_can_still_be_instantiated(self):
        driver = NoParamSchemaDriver()
        assert isinstance(driver, ResourceDriver)

    def test_omitted_param_schema_raises_attribute_error_on_access(self):
        driver = NoParamSchemaDriver()
        with pytest.raises(AttributeError):
            _ = driver.PARAM_SCHEMA


class TestDriverUpdateNotSupported:
    def test_reason_is_stored(self):
        exc = DriverUpdateNotSupported("image change requires replace")
        assert exc.reason == "image change requires replace"

    def test_unsupported_fields_defaults_to_empty_list(self):
        exc = DriverUpdateNotSupported("image change requires replace")
        assert exc.unsupported_fields == []

    def test_unsupported_fields_explicit_none_defaults_to_empty_list(self):
        exc = DriverUpdateNotSupported("image change requires replace", None)
        assert exc.unsupported_fields == []

    def test_unsupported_fields_explicit_empty_list(self):
        exc = DriverUpdateNotSupported("image change requires replace", [])
        assert exc.unsupported_fields == []

    def test_unsupported_fields_explicit_list(self):
        exc = DriverUpdateNotSupported("image change requires replace", ["image"])
        assert exc.unsupported_fields == ["image"]

    def test_str_is_reason(self):
        exc = DriverUpdateNotSupported("image change requires replace")
        assert str(exc) == "image change requires replace"

    def test_is_plain_exception_subclass(self):
        assert issubclass(DriverUpdateNotSupported, Exception)
        assert not issubclass(DriverUpdateNotSupported, ResourceDriver)
