from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from aiform.models import (
    DriverInfo,
    DriverReview,
    LLMConfig,
    LLMRoleConfig,
    LoggingConfig,
    ModelSource,
    ParsedResource,
    PlanAction,
    PlanEntry,
    PlanReview,
    PlanReviewFlag,
    PlanReviewSeverity,
    ResourceSpec,
    StateEntry,
)


class TestResourceSpec:
    def test_accepts_valid_fields(self):
        spec = ResourceSpec(
            resource="compute",
            name="telleztec-app-01",
            provider="digitalocean",
            params={"region": "sfo3", "size": "s-1vcpu-2gb", "image": "ubuntu-24-04-x64"},
        )
        assert spec.resource == "compute"
        assert spec.name == "telleztec-app-01"
        assert spec.provider == "digitalocean"
        assert spec.params["region"] == "sfo3"

    def test_rejects_extra_fields(self):
        with pytest.raises(ValidationError):
            ResourceSpec(
                resource="compute",
                name="telleztec-app-01",
                provider="digitalocean",
                params={},
                unexpected_field="oops",
            )

    def test_rejects_uppercase_resource(self):
        with pytest.raises(ValidationError):
            ResourceSpec(resource="Compute", name="x", provider="digitalocean", params={})

    def test_rejects_uppercase_provider(self):
        with pytest.raises(ValidationError):
            ResourceSpec(resource="compute", name="x", provider="DigitalOcean", params={})

    def test_rejects_resource_with_slash(self):
        with pytest.raises(ValidationError):
            ResourceSpec(resource="compute/../etc", name="x", provider="digitalocean", params={})

    def test_rejects_resource_with_space(self):
        with pytest.raises(ValidationError):
            ResourceSpec(resource="compute node", name="x", provider="digitalocean", params={})

    def test_rejects_resource_starting_with_digit(self):
        with pytest.raises(ValidationError):
            ResourceSpec(resource="1compute", name="x", provider="digitalocean", params={})

    def test_rejects_empty_name(self):
        with pytest.raises(ValidationError):
            ResourceSpec(resource="compute", name="", provider="digitalocean", params={})

    def test_params_is_open_dict(self):
        spec = ResourceSpec(
            resource="compute",
            name="x",
            provider="digitalocean",
            params={"anything_goes": True, "nested": {"a": 1}},
        )
        assert spec.params == {"anything_goes": True, "nested": {"a": 1}}


class TestPlanAction:
    def test_values(self):
        assert PlanAction.CREATE == "create"
        assert PlanAction.UPDATE == "update"
        assert PlanAction.DESTROY == "destroy"
        assert PlanAction.NO_OP == "no-op"

    def test_constructible_from_raw_string(self):
        assert PlanAction("no-op") is PlanAction.NO_OP

    def test_rejects_unknown_value(self):
        with pytest.raises(ValueError):
            PlanAction("recreate")


class TestPlanEntry:
    def test_defaults_likely_replace_false(self):
        entry = PlanEntry(
            resource_key="digitalocean.compute.telleztec-app-01",
            action=PlanAction.CREATE,
            rationale="no changes detected",
        )
        assert entry.likely_replace is False

    def test_normalizes_likely_replace_for_non_update_actions(self):
        entry = PlanEntry(
            resource_key="digitalocean.compute.telleztec-app-01",
            action=PlanAction.CREATE,
            rationale="new resource",
            likely_replace=True,
        )
        assert entry.likely_replace is False

    def test_preserves_likely_replace_for_update_action(self):
        entry = PlanEntry(
            resource_key="digitalocean.compute.telleztec-app-01",
            action=PlanAction.UPDATE,
            rationale="image changed, requires replace",
            likely_replace=True,
        )
        assert entry.likely_replace is True

    def test_destroy_action_ignores_likely_replace(self):
        entry = PlanEntry(
            resource_key="digitalocean.compute.telleztec-app-01",
            action=PlanAction.DESTROY,
            rationale="removed from aiform.md",
            likely_replace=True,
        )
        assert entry.likely_replace is False


class TestPlanReview:
    def test_severity_values(self):
        assert PlanReviewSeverity.INFO == "info"
        assert PlanReviewSeverity.WARNING == "warning"
        assert PlanReviewSeverity.BLOCK == "block"

    def test_flag_rejects_unknown_severity(self):
        with pytest.raises(ValidationError):
            PlanReviewFlag(
                resource_key="digitalocean.compute.telleztec-app-01",
                concern="unexpected destroy",
                severity="urgent",
            )

    def test_parses_flags_into_objects(self):
        review = PlanReview(
            safe_to_proceed=False,
            flags=[
                {
                    "resource_key": "digitalocean.compute.telleztec-app-01",
                    "concern": "this destroy wasn't mentioned in the Intent section",
                    "severity": "block",
                }
            ],
        )
        assert len(review.flags) == 1
        flag = review.flags[0]
        assert isinstance(flag, PlanReviewFlag)
        assert flag.severity is PlanReviewSeverity.BLOCK
        assert flag.resource_key == "digitalocean.compute.telleztec-app-01"

    def test_safe_to_proceed_with_no_flags(self):
        review = PlanReview(safe_to_proceed=True, flags=[])
        assert review.safe_to_proceed is True
        assert review.flags == []


class TestLLMConfig:
    def test_only_anthropic_source_today(self):
        assert ModelSource.ANTHROPIC == "anthropic"

    def test_rejects_unknown_source(self):
        with pytest.raises(ValidationError):
            LLMRoleConfig(source="bedrock", model="some-model", max_tokens=4096)

    def test_rejects_missing_max_tokens(self):
        with pytest.raises(ValidationError):
            LLMRoleConfig(source=ModelSource.ANTHROPIC, model="some-model")

    def test_rejects_zero_max_tokens(self):
        with pytest.raises(ValidationError):
            LLMRoleConfig(source=ModelSource.ANTHROPIC, model="some-model", max_tokens=0)

    def test_rejects_negative_max_tokens(self):
        with pytest.raises(ValidationError):
            LLMRoleConfig(source=ModelSource.ANTHROPIC, model="some-model", max_tokens=-1)

    def test_parses_roles_into_objects(self):
        config = LLMConfig(
            intent_orchestration={
                "source": "anthropic",
                "model": "claude-sonnet-5",
                "max_tokens": 4096,
            },
            code_generator={
                "source": "anthropic",
                "model": "claude-sonnet-5",
                "max_tokens": 4096,
            },
            code_review={"source": "anthropic", "model": "claude-opus-5", "max_tokens": 8192},
            review_orchestration={
                "source": "anthropic",
                "model": "claude-opus-5",
                "max_tokens": 8192,
            },
        )
        assert isinstance(config.intent_orchestration, LLMRoleConfig)
        assert config.intent_orchestration.source is ModelSource.ANTHROPIC
        assert config.intent_orchestration.model == "claude-sonnet-5"
        assert config.code_generator.model == "claude-sonnet-5"
        assert config.code_review.model == "claude-opus-5"
        assert config.review_orchestration.model == "claude-opus-5"

    def test_roles_are_independent(self):
        config = LLMConfig(
            intent_orchestration=LLMRoleConfig(
                source=ModelSource.ANTHROPIC, model="claude-sonnet-5", max_tokens=4096
            ),
            code_generator=LLMRoleConfig(
                source=ModelSource.ANTHROPIC, model="claude-haiku-4-5", max_tokens=4096
            ),
            code_review=LLMRoleConfig(
                source=ModelSource.ANTHROPIC, model="claude-opus-5", max_tokens=8192
            ),
            review_orchestration=LLMRoleConfig(
                source=ModelSource.ANTHROPIC, model="claude-opus-4-8", max_tokens=8192
            ),
        )
        assert config.intent_orchestration.model != config.code_generator.model
        assert config.code_review.model != config.review_orchestration.model


class TestLoggingConfig:
    def test_accepts_all_four_known_levels(self):
        for level in ("DEBUG", "INFO", "WARNING", "ERROR"):
            assert LoggingConfig(level=level, max_files=10).level == level

    def test_rejects_unknown_level(self):
        with pytest.raises(ValidationError):
            LoggingConfig(level="TRACE", max_files=10)

    def test_rejects_stdlib_critical_not_in_this_codebase_s_set(self):
        with pytest.raises(ValidationError):
            LoggingConfig(level="CRITICAL", max_files=10)

    def test_rejects_zero_max_files(self):
        with pytest.raises(ValidationError):
            LoggingConfig(level="INFO", max_files=0)

    def test_rejects_negative_max_files(self):
        with pytest.raises(ValidationError):
            LoggingConfig(level="INFO", max_files=-1)


class TestDriverReview:
    def test_rejects_approved_with_blocking_issues(self):
        with pytest.raises(ValidationError):
            DriverReview(
                approved=True,
                concerns=[],
                blocking_issues=["update() swallows errors"],
                reviewed_at=datetime(2026, 7, 30, 18, 22, 40),
                model="claude-opus-5",
            )

    def test_allows_approved_with_empty_blocking_issues(self):
        review = DriverReview(
            approved=True,
            concerns=["update() resizes on any diff"],
            blocking_issues=[],
            reviewed_at=datetime(2026, 7, 30, 18, 22, 40),
            model="claude-opus-5",
        )
        assert review.approved is True

    def test_allows_not_approved_with_blocking_issues(self):
        review = DriverReview(
            approved=False,
            concerns=[],
            blocking_issues=["delete() is not idempotent"],
            reviewed_at=datetime(2026, 7, 30, 18, 22, 40),
            model="claude-opus-5",
        )
        assert review.approved is False
        assert review.blocking_issues == ["delete() is not idempotent"]


class TestDriverInfo:
    def test_round_trip_through_json(self):
        info = DriverInfo(
            path="drivers/digitalocean/compute.py",
            sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b8",
            generated_at=datetime(2026, 7, 30, 18, 22, 11),
            code_review=DriverReview(
                approved=True,
                concerns=["update() resizes on any diff, not just size/region"],
                blocking_issues=[],
                reviewed_at=datetime(2026, 7, 30, 18, 22, 40),
                model="claude-opus-5",
            ),
        )
        dumped = info.model_dump(mode="json")
        assert dumped["path"] == "drivers/digitalocean/compute.py"
        assert dumped["code_review"]["approved"] is True

        reparsed = DriverInfo.model_validate(dumped)
        assert reparsed == info


class TestStateEntry:
    def _make(self, **overrides):
        defaults = dict(
            provider="digitalocean",
            resource_type="compute",
            name="telleztec-app-01",
            id="123456789",
            attributes={
                "region": "sfo3",
                "size": "s-1vcpu-2gb",
                "image": "ubuntu-24-04-x64",
                "ssh_keys": ["juan-macbook-ed25519"],
                "backups": False,
                "monitoring": True,
                "tags": ["aiform", "production"],
                "ipv4_address": "203.0.113.10",
                "status": "active",
            },
            driver=DriverInfo(
                path="drivers/digitalocean/compute.py",
                sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b8",
                generated_at="2026-07-30T18:22:11Z",
                code_review=DriverReview(
                    approved=True,
                    concerns=["update() resizes on any diff, not just size/region"],
                    blocking_issues=[],
                    reviewed_at="2026-07-30T18:22:40Z",
                    model="claude-opus-5",
                ),
            ),
            last_applied_at="2026-07-30T18:23:05Z",
            last_refreshed_at="2026-07-31T09:10:00Z",
            aiform_md_path="examples/compute.aiform.md",
            aiform_md_sha256="5f4dcc3b5aa765d61d8327deb882cf99",
        )
        defaults.update(overrides)
        return StateEntry(**defaults)

    def test_parses_iso8601_datetime_strings(self):
        entry = self._make()
        assert entry.last_applied_at == datetime(2026, 7, 30, 18, 23, 5, tzinfo=UTC)

    def test_round_trip_matches_plan_md_json_shape(self):
        entry = self._make()
        dumped = entry.model_dump(mode="json")

        assert set(dumped.keys()) == {
            "provider",
            "resource_type",
            "name",
            "id",
            "attributes",
            "driver",
            "last_applied_at",
            "last_refreshed_at",
            "aiform_md_path",
            "aiform_md_sha256",
        }
        assert set(dumped["driver"].keys()) == {"path", "sha256", "generated_at", "code_review"}
        assert set(dumped["driver"]["code_review"].keys()) == {
            "approved",
            "concerns",
            "blocking_issues",
            "reviewed_at",
            "model",
        }

        reparsed = StateEntry.model_validate(dumped)
        assert reparsed == entry

    def test_rejects_path_unsafe_provider(self):
        with pytest.raises(ValidationError):
            self._make(provider="../../etc")

    def test_rejects_path_unsafe_resource_type(self):
        with pytest.raises(ValidationError):
            self._make(resource_type="../../passwd")

    def test_rejects_empty_name(self):
        with pytest.raises(ValidationError):
            self._make(name="")


class TestParsedResource:
    def _spec(self) -> ResourceSpec:
        return ResourceSpec(
            resource="compute",
            name="telleztec-app-01",
            provider="digitalocean",
            params={"region": "sfo3"},
        )

    def test_accepts_spec_intent_notes_and_hash(self):
        parsed = ParsedResource(
            spec=self._spec(),
            intent_notes=[{"concerns_field": "size", "guidance": "prefer resize"}],
            aiform_md_sha256="abc123",
        )
        assert parsed.spec.name == "telleztec-app-01"
        assert parsed.intent_notes == [{"concerns_field": "size", "guidance": "prefer resize"}]
        assert parsed.aiform_md_sha256 == "abc123"

    def test_accepts_empty_intent_notes(self):
        parsed = ParsedResource(spec=self._spec(), intent_notes=[], aiform_md_sha256="abc123")
        assert parsed.intent_notes == []

    def test_rejects_non_dict_intent_note_items(self):
        with pytest.raises(ValidationError):
            ParsedResource(
                spec=self._spec(), intent_notes=["not-a-dict"], aiform_md_sha256="abc123"
            )

    def test_rejects_missing_spec(self):
        with pytest.raises(ValidationError):
            ParsedResource(intent_notes=[], aiform_md_sha256="abc123")
