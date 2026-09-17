# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

import pytest

from aiform.driver import CapabilityNotSupported, DriverUpdateNotSupported, ResourceDriver
from aiform.models import HealthReport, HealthStatus, MetricKind, Sample


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


class UnorderedFieldsDriver(FullDriver):
    UNORDERED_FIELDS = ["tags"]


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


class TestUnorderedFields:
    def test_defaults_to_empty_list_when_not_overridden(self):
        driver = FullDriver()
        assert driver.UNORDERED_FIELDS == []

    def test_subclass_can_override(self):
        driver = UnorderedFieldsDriver()
        assert driver.UNORDERED_FIELDS == ["tags"]

    def test_overriding_one_driver_does_not_affect_another_sharing_the_base_default(self):
        # Same reassign-don't-mutate rule as LIKELY_REPLACE_FIELDS and
        # NON_DIFFABLE_FIELDS: a subclass appending in place to the
        # inherited list would corrupt every other driver still relying
        # on the base class's empty default.
        UnorderedFieldsDriver()
        assert FullDriver().UNORDERED_FIELDS == []


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


class HealthyDriver(FullDriver):
    def health(self, id, credentials):
        return HealthReport(status=HealthStatus.OK, summary=f"{id} is fine")


class MeasuredDriver(FullDriver):
    def metrics(self, id, credentials):
        return [Sample(name="memory_bytes", kind=MetricKind.GAUGE, value=2147483648.0)]


class DeliberatelyOpaqueDriver(FullDriver):
    def health(self, id, credentials):
        raise CapabilityNotSupported("health", "DNS records have no status the API reports")


class TestCapabilityNotSupported:
    def test_capability_and_reason_are_stored(self):
        exc = CapabilityNotSupported("health", "this driver does not implement health()")
        assert exc.capability == "health"
        assert exc.reason == "this driver does not implement health()"

    def test_str_joins_capability_and_reason(self):
        exc = CapabilityNotSupported("metrics", "no monitoring endpoint")
        assert str(exc) == "metrics: no monitoring endpoint"

    def test_is_a_plain_exception_and_not_a_driver_update_not_supported(self):
        assert issubclass(CapabilityNotSupported, Exception)
        assert not issubclass(CapabilityNotSupported, DriverUpdateNotSupported)


class TestOptionalHealth:
    def test_a_driver_that_does_not_override_it_is_still_instantiable(self):
        # Concrete, not @abstractmethod: making it abstract would break
        # every existing driver at instantiation time.
        assert isinstance(FullDriver(), ResourceDriver)

    def test_the_default_declines_with_capability_not_supported(self):
        with pytest.raises(CapabilityNotSupported) as excinfo:
            FullDriver().health("123", {"TOKEN": "x"})
        assert excinfo.value.capability == "health"
        assert "does not implement health()" in excinfo.value.reason

    def test_a_driver_can_override_it(self):
        report = HealthyDriver().health("123", {"TOKEN": "x"})
        assert report.status is HealthStatus.OK
        assert report.summary == "123 is fine"

    def test_a_driver_can_decline_with_its_own_reason(self):
        with pytest.raises(CapabilityNotSupported) as excinfo:
            DeliberatelyOpaqueDriver().health("123", {"TOKEN": "x"})
        assert excinfo.value.reason == "DNS records have no status the API reports"

    def test_overriding_health_leaves_metrics_declining(self):
        with pytest.raises(CapabilityNotSupported):
            HealthyDriver().metrics("123", {"TOKEN": "x"})


class TestOptionalMetrics:
    def test_the_default_declines_with_capability_not_supported(self):
        with pytest.raises(CapabilityNotSupported) as excinfo:
            FullDriver().metrics("123", {"TOKEN": "x"})
        assert excinfo.value.capability == "metrics"
        assert "does not implement metrics()" in excinfo.value.reason

    def test_a_driver_can_override_it(self):
        samples = MeasuredDriver().metrics("123", {"TOKEN": "x"})
        assert [(s.name, s.kind, s.value) for s in samples] == [
            ("memory_bytes", MetricKind.GAUGE, 2147483648.0)
        ]

    def test_overriding_metrics_leaves_health_declining(self):
        with pytest.raises(CapabilityNotSupported):
            MeasuredDriver().health("123", {"TOKEN": "x"})


class TestOptionalMethodSignatures:
    # Contract, not style: specs/driver_gen.md specifies an
    # OPTIONAL_METHOD_PARAMS check that does exact list equality on these
    # names. That check is NOT built -- EXPECTED_METHOD_PARAMS covers
    # only create/read/update/delete, and validate_driver_source()
    # returns None for a driver spelling `id` as `resource_id`. Until it
    # is built this assertion is the only thing holding the names, which
    # is why it asserts against the base class rather than trusting the
    # validator to.
    def test_both_take_exactly_self_id_credentials(self):
        import inspect

        for method in (ResourceDriver.health, ResourceDriver.metrics):
            params = list(inspect.signature(method).parameters)
            assert params == ["self", "id", "credentials"]
