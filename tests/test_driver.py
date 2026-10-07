# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

import importlib
from pathlib import Path

import pytest

from aiform.driver import (
    AIFORM_MANAGED_TAG,
    CapabilityNotSupported,
    DriverUpdateNotSupported,
    ReferenceField,
    ResourceDriver,
    is_reserved_tag,
    reserved_tags,
    values_at,
)
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


class ReferenceFieldsDriver(FullDriver):
    REFERENCE_FIELDS = [ReferenceField("vm_ids", ("acme", "vm"), "integer")]


class TestReferenceFields:
    def test_defaults_to_empty_list_when_not_overridden(self):
        assert FullDriver().REFERENCE_FIELDS == []

    def test_subclass_can_override(self):
        assert ReferenceFieldsDriver().REFERENCE_FIELDS == [
            ReferenceField("vm_ids", ("acme", "vm"), "integer")
        ]

    def test_overriding_one_driver_does_not_affect_another_sharing_the_base_default(self):
        ReferenceFieldsDriver()
        assert FullDriver().REFERENCE_FIELDS == []
        assert ResourceDriver.REFERENCE_FIELDS == []

    def test_a_declaration_carries_path_target_and_id_type(self):
        field = ReferenceField("rules[].sources.vm_ids", ("acme", "vm"), "string")

        assert field.path == "rules[].sources.vm_ids"
        assert field.target == ("acme", "vm")
        assert field.id_type == "string"

    @pytest.mark.parametrize(
        ("path", "top_level"),
        [("vm_ids", True), ("rules[].vm_ids", False), ("sources.vm_ids", False)],
    )
    def test_only_a_plain_key_is_top_level(self, path, top_level):
        assert ReferenceField(path, ("acme", "vm"), "integer").top_level is top_level


class TestValuesAt:
    def test_yields_the_ids_of_a_top_level_list(self):
        assert list(values_at({"vm_ids": [1, 2]}, "vm_ids")) == [1, 2]

    def test_descends_keys_and_fans_out_lists(self):
        attributes = {
            "rules": [
                {"sources": {"vm_ids": [1, 2]}},
                {"sources": {"vm_ids": [3]}},
                {"sources": {"addresses": ["10.0.0.0/8"]}},
            ]
        }

        assert list(values_at(attributes, "rules[].sources.vm_ids")) == [1, 2, 3]

    @pytest.mark.parametrize(
        "attributes",
        [
            {},
            {"vm_ids": None},
            {"vm_ids": []},
            {"rules": None},
            {"rules": []},
            {"rules": [None, {"sources": None}, {}]},
        ],
    )
    def test_yields_nothing_for_missing_none_or_empty(self, attributes):
        assert list(values_at(attributes, "vm_ids")) == []
        assert list(values_at(attributes, "rules[].sources.vm_ids")) == []


def _curated_driver_modules():
    drivers_root = Path(__file__).resolve().parent.parent / "drivers"
    for path in sorted(drivers_root.glob("*/*.py")):
        if not path.name.startswith("_"):
            yield pytest.param(
                f"drivers.{path.parent.name}.{path.stem}", id=f"{path.parent.name}.{path.stem}"
            )


def schema_node_at(schema, path):
    node = schema
    for segment in path.split("."):
        node = node["properties"][segment.removesuffix("[]")]
        if segment.endswith("[]"):
            node = node["items"]
    return node


class TestEveryDriversDeclarationMatchesItsSchema:
    @pytest.mark.parametrize("module_name", _curated_driver_modules())
    def test_each_declared_path_resolves_to_a_list_of_the_declared_id_type(self, module_name):
        driver_class = importlib.import_module(module_name).Driver

        for field in driver_class.REFERENCE_FIELDS:
            node = schema_node_at(driver_class.PARAM_SCHEMA, field.path)
            assert node["type"] == "array", field
            assert node["items"]["type"] == field.id_type, field


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


class TestReservedTagNames:
    def test_reserved_tags_are_the_marker_and_the_deployment_tag(self):
        assert reserved_tags("prod") == ("aiform-managed", "aiform:prod")

    def test_the_marker_constant(self):
        assert AIFORM_MANAGED_TAG == "aiform-managed"

    def test_the_default_deployment_is_tagged_like_any_other(self):
        assert reserved_tags("default") == ("aiform-managed", "aiform:default")

    @pytest.mark.parametrize("tag", ["aiform-managed", "aiform:prod", "aiform:", "aiform:a:b"])
    def test_reserved(self, tag):
        assert is_reserved_tag(tag)

    @pytest.mark.parametrize(
        "tag",
        ["aiform-system-test", "aiform", "Aiform-Managed", "AIFORM:prod", "web", "x-aiform:a"],
    )
    def test_not_reserved(self, tag):
        assert not is_reserved_tag(tag)


class TestReservedTagsOnTheBaseClass:
    def test_constructed_without_arguments_has_no_reserved_tags(self):
        driver = FullDriver()
        assert driver.reserved_tags == ()
        assert driver._deployment_tag is None

    def test_constructor_stores_the_tags_and_finds_the_deployment_tag(self):
        driver = FullDriver(reserved_tags=reserved_tags("prod"))
        assert driver.reserved_tags == ("aiform-managed", "aiform:prod")
        assert driver._deployment_tag == "aiform:prod"

    def test_tags_for_create_appends_both_after_the_requested_tags(self):
        driver = FullDriver(reserved_tags=reserved_tags("prod"))
        assert driver._tags_for_create(["web", "db"]) == [
            "web",
            "db",
            "aiform-managed",
            "aiform:prod",
        ]

    def test_tags_for_create_with_no_requested_tags_still_attaches_both(self):
        driver = FullDriver(reserved_tags=reserved_tags("prod"))
        assert driver._tags_for_create([]) == ["aiform-managed", "aiform:prod"]

    def test_tags_for_create_without_reserved_tags_returns_the_requested_ones(self):
        assert FullDriver()._tags_for_create(["web"]) == ["web"]

    @pytest.mark.parametrize("tag", ["aiform-managed", "aiform:prod", "aiform:other"])
    def test_tags_for_create_rejects_a_reserved_tag_naming_it(self, tag):
        driver = FullDriver(reserved_tags=reserved_tags("prod"))
        with pytest.raises(ValueError, match=tag):
            driver._tags_for_create(["web", tag])

    def test_a_driver_with_no_reserved_tags_still_rejects_one(self):
        with pytest.raises(ValueError, match="aiform-managed"):
            FullDriver()._tags_for_create(["aiform-managed"])

    def test_reject_reserved_tags_accepts_ordinary_tags(self):
        FullDriver()._reject_reserved_tags(["web", "aiform-system-test"])

    def test_tags_for_attributes_strips_every_reserved_tag(self):
        driver = FullDriver(reserved_tags=reserved_tags("prod"))
        live = ["web", "aiform-managed", "aiform:prod", "aiform:other", "db"]
        assert driver._tags_for_attributes(live) == ["web", "db"]

    def test_tags_for_attributes_strips_even_without_reserved_tags_on_the_instance(self):
        assert FullDriver()._tags_for_attributes(["aiform-managed", "web"]) == ["web"]

    def test_tags_for_attributes_does_not_mutate_its_argument(self):
        live = ["aiform-managed", "web"]
        FullDriver()._tags_for_attributes(live)
        assert live == ["aiform-managed", "web"]
