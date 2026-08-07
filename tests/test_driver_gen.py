import json
from pathlib import Path

import pytest

from aiform import driver_gen, llm
from aiform.models import ResourceSpec

VALID_DRIVER_SOURCE = """\
from aiform.driver import ResourceDriver


class Driver(ResourceDriver):
    PARAM_SCHEMA = {"type": "object", "properties": {}}

    def create(self, params, credentials):
        return {"id": "1"}

    def read(self, id, credentials):
        return {"id": id}

    def update(self, id, current, desired, credentials):
        return {"id": id}

    def delete(self, id, credentials):
        return None
"""


class FakeTextBlock:
    def __init__(self, text: str):
        self.type = "text"
        self.text = text


class FakeResponse:
    def __init__(self, text: str):
        self.content = [FakeTextBlock(text)]


class FakeMessages:
    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return FakeResponse(self._responses.pop(0))


class FakeClient:
    def __init__(self, responses: list[str]):
        self.messages = FakeMessages(responses)


@pytest.fixture
def prompts_dir(tmp_path: Path, monkeypatch) -> Path:
    directory = tmp_path / "prompts"
    directory.mkdir()
    (directory / "review_driver.md").write_text("Review the driver source for correctness.\n")
    (directory / "generate_driver.md").write_text("Draft a driver against the contract.\n")
    monkeypatch.setattr(llm, "PROMPTS_DIR", directory)
    return directory


@pytest.fixture(autouse=True)
def specs_dir(tmp_path: Path, monkeypatch) -> Path:
    # autouse: without this, draft_driver() defaults (provider="digitalocean",
    # resource="compute" via make_spec()) resolve SPECS_DIR against the real
    # repo, so every test would silently pull in the real, evolving
    # specs/digitalocean_compute.md instead of a hermetic empty directory.
    directory = tmp_path / "specs"
    directory.mkdir()
    monkeypatch.setattr(driver_gen, "SPECS_DIR", directory)
    return directory


def make_spec(**overrides) -> ResourceSpec:
    defaults = dict(
        resource="compute",
        name="telleztec-app-01",
        provider="digitalocean",
        params={"region": "sfo3", "size": "s-1vcpu-2gb"},
    )
    defaults.update(overrides)
    return ResourceSpec(**defaults)


def approve_response() -> str:
    return json.dumps({"approved": True, "concerns": [], "blocking_issues": []})


def block_response(issues: list[str]) -> str:
    return json.dumps({"approved": False, "concerns": [], "blocking_issues": issues})


def not_approved_no_issues_response() -> str:
    return json.dumps({"approved": False, "concerns": [], "blocking_issues": []})


class TestValidateDriverSource:
    def test_valid_source_passes(self):
        assert driver_gen.validate_driver_source(VALID_DRIVER_SOURCE) is None

    def test_syntax_error_reports_single_reason(self):
        with pytest.raises(driver_gen.DriverValidationError) as excinfo:
            driver_gen.validate_driver_source("def foo(:\n    pass\n")
        assert len(excinfo.value.reasons) == 1
        assert "syntax" in excinfo.value.reasons[0].lower()

    def test_missing_driver_class(self):
        with pytest.raises(driver_gen.DriverValidationError) as excinfo:
            driver_gen.validate_driver_source("x = 1\n")
        assert any("driver" in r.lower() for r in excinfo.value.reasons)

    def test_missing_driver_class_and_anthropic_import_both_reported(self):
        with pytest.raises(driver_gen.DriverValidationError) as excinfo:
            driver_gen.validate_driver_source("import anthropic\n\nx = 1\n")
        reasons = " ".join(excinfo.value.reasons).lower()
        assert "driver" in reasons
        assert "anthropic" in reasons

    def test_wrong_base_class(self):
        source = "class Driver:\n    pass\n"
        with pytest.raises(driver_gen.DriverValidationError) as excinfo:
            driver_gen.validate_driver_source(source)
        assert any("resourcedriver" in r.lower() for r in excinfo.value.reasons)

    def test_missing_param_schema(self):
        source = VALID_DRIVER_SOURCE.replace(
            '    PARAM_SCHEMA = {"type": "object", "properties": {}}\n\n', ""
        )
        with pytest.raises(driver_gen.DriverValidationError) as excinfo:
            driver_gen.validate_driver_source(source)
        assert any("param_schema" in r.lower() for r in excinfo.value.reasons)

    def test_missing_one_method(self):
        source = VALID_DRIVER_SOURCE.replace(
            "    def delete(self, id, credentials):\n        return None\n", ""
        )
        with pytest.raises(driver_gen.DriverValidationError) as excinfo:
            driver_gen.validate_driver_source(source)
        assert any("delete" in r.lower() for r in excinfo.value.reasons)

    def test_method_wrong_parameter_names(self):
        source = VALID_DRIVER_SOURCE.replace(
            "    def delete(self, id, credentials):\n        return None\n",
            "    def delete(self, resource_id, creds):\n        return None\n",
        )
        with pytest.raises(driver_gen.DriverValidationError) as excinfo:
            driver_gen.validate_driver_source(source)
        assert any("delete" in r.lower() for r in excinfo.value.reasons)

    def test_import_anthropic_statement(self):
        source = "import anthropic\n\n" + VALID_DRIVER_SOURCE
        with pytest.raises(driver_gen.DriverValidationError) as excinfo:
            driver_gen.validate_driver_source(source)
        assert any("anthropic" in r.lower() for r in excinfo.value.reasons)

    def test_from_anthropic_import_statement(self):
        source = "from anthropic import Anthropic\n\n" + VALID_DRIVER_SOURCE
        with pytest.raises(driver_gen.DriverValidationError) as excinfo:
            driver_gen.validate_driver_source(source)
        assert any("anthropic" in r.lower() for r in excinfo.value.reasons)

    def test_string_literal_containing_anthropic_substring(self):
        source = VALID_DRIVER_SOURCE.replace(
            '    def create(self, params, credentials):\n        return {"id": "1"}\n',
            "    def create(self, params, credentials):\n"
            "        import os\n"
            '        os.environ.get("ANTHROPIC_API_KEY")\n'
            '        return {"id": "1"}\n',
        )
        with pytest.raises(driver_gen.DriverValidationError) as excinfo:
            driver_gen.validate_driver_source(source)
        assert any("anthropic" in r.lower() for r in excinfo.value.reasons)

    def test_multiple_failures_all_accumulate(self):
        source = "import anthropic\n\n\nclass Driver:\n    pass\n"
        with pytest.raises(driver_gen.DriverValidationError) as excinfo:
            driver_gen.validate_driver_source(source)
        reasons = " ".join(excinfo.value.reasons).lower()
        assert "anthropic" in reasons
        assert "resourcedriver" in reasons
        assert "param_schema" in reasons
        assert "create" in reasons
        assert len(excinfo.value.reasons) >= 5


class TestDraftDriver:
    def test_returns_raw_text_unmodified(self, prompts_dir: Path):
        client = FakeClient([VALID_DRIVER_SOURCE])
        result = driver_gen.draft_driver(make_spec(), client=client)
        assert result == VALID_DRIVER_SOURCE

    def test_uses_generate_driver_prompt_as_system(self, prompts_dir: Path):
        client = FakeClient([VALID_DRIVER_SOURCE])
        driver_gen.draft_driver(make_spec(), client=client)
        call = client.messages.calls[0]
        assert call["system"] == (prompts_dir / "generate_driver.md").read_text()

    def test_user_content_includes_provider_resource_and_params(self, prompts_dir: Path):
        client = FakeClient([VALID_DRIVER_SOURCE])
        spec = make_spec(provider="digitalocean", resource="compute", params={"region": "sfo3"})
        driver_gen.draft_driver(spec, client=client)
        content = client.messages.calls[0]["messages"][0]["content"]
        assert "digitalocean" in content
        assert "compute" in content
        assert "sfo3" in content

    def test_omits_output_schema(self, prompts_dir: Path):
        client = FakeClient([VALID_DRIVER_SOURCE])
        driver_gen.draft_driver(make_spec(), client=client)
        assert "output_config" not in client.messages.calls[0]

    def test_uses_max_tokens_8192(self, prompts_dir: Path):
        client = FakeClient([VALID_DRIVER_SOURCE])
        driver_gen.draft_driver(make_spec(), client=client)
        assert client.messages.calls[0]["max_tokens"] == 8192

    def test_appends_feedback_when_given(self, prompts_dir: Path):
        client = FakeClient([VALID_DRIVER_SOURCE])
        driver_gen.draft_driver(
            make_spec(), feedback="- delete() has the wrong parameter names", client=client
        )
        content = client.messages.calls[0]["messages"][0]["content"]
        assert "delete() has the wrong parameter names" in content

    def test_omits_feedback_block_when_not_given(self, prompts_dir: Path):
        client = FakeClient([VALID_DRIVER_SOURCE])
        driver_gen.draft_driver(make_spec(), client=client)
        content = client.messages.calls[0]["messages"][0]["content"]
        assert "rejected" not in content.lower()

    def test_includes_credentials_env_var_for_known_provider(
        self, prompts_dir: Path, specs_dir: Path
    ):
        client = FakeClient([VALID_DRIVER_SOURCE])
        driver_gen.draft_driver(make_spec(provider="digitalocean"), client=client)
        content = client.messages.calls[0]["messages"][0]["content"]
        assert "DIGITALOCEAN_TOKEN" in content

    def test_omits_credentials_hint_for_unrecognized_provider(
        self, prompts_dir: Path, specs_dir: Path
    ):
        client = FakeClient([VALID_DRIVER_SOURCE])
        driver_gen.draft_driver(make_spec(provider="aws", resource="compute"), client=client)
        content = client.messages.calls[0]["messages"][0]["content"]
        assert "credentials" not in content.lower()

    def test_includes_existing_spec_file_content(self, prompts_dir: Path, specs_dir: Path):
        (specs_dir / "digitalocean_compute.md").write_text(
            "## Behavior\n\nResize requires powering off first.\n"
        )
        client = FakeClient([VALID_DRIVER_SOURCE])
        driver_gen.draft_driver(
            make_spec(provider="digitalocean", resource="compute"), client=client
        )
        content = client.messages.calls[0]["messages"][0]["content"]
        assert "Resize requires powering off first." in content

    def test_frames_existing_spec_as_authoritative(self, prompts_dir: Path, specs_dir: Path):
        (specs_dir / "digitalocean_compute.md").write_text("## Behavior\n\nSome detail.\n")
        client = FakeClient([VALID_DRIVER_SOURCE])
        driver_gen.draft_driver(
            make_spec(provider="digitalocean", resource="compute"), client=client
        )
        content = client.messages.calls[0]["messages"][0]["content"].lower()
        assert "authoritative" in content

    def test_omits_spec_content_when_no_spec_file_exists(self, prompts_dir: Path, specs_dir: Path):
        client = FakeClient([VALID_DRIVER_SOURCE])
        driver_gen.draft_driver(
            make_spec(provider="digitalocean", resource="load_balancer"), client=client
        )
        content = client.messages.calls[0]["messages"][0]["content"].lower()
        assert "authoritative" not in content

    def test_spec_lookup_uses_provider_and_resource_specific_filename(
        self, prompts_dir: Path, specs_dir: Path
    ):
        (specs_dir / "digitalocean_load_balancer.md").write_text(
            "## Behavior\n\nUnrelated resource's spec.\n"
        )
        client = FakeClient([VALID_DRIVER_SOURCE])
        driver_gen.draft_driver(
            make_spec(provider="digitalocean", resource="compute"), client=client
        )
        content = client.messages.calls[0]["messages"][0]["content"]
        assert "Unrelated resource's spec." not in content

    def test_ignores_collision_with_a_reserved_module_spec_filename(
        self, prompts_dir: Path, specs_dir: Path
    ):
        # provider="driver", resource="gen" would otherwise resolve to
        # specs/driver_gen.md -- this module's own dev-process spec, not a
        # driver acceptance-criteria spec -- and get pasted into the
        # generation prompt as if it were authoritative for this resource.
        (specs_dir / "driver_gen.md").write_text("## Purpose\n\nThe generation half of PLAN.md.\n")
        client = FakeClient([VALID_DRIVER_SOURCE])
        driver_gen.draft_driver(make_spec(provider="driver", resource="gen"), client=client)
        content = client.messages.calls[0]["messages"][0]["content"].lower()
        assert "authoritative" not in content
        assert "the generation half of plan.md" not in content


class TestGenerateDriver:
    def test_happy_path_first_attempt_returns_source_and_review(self, prompts_dir: Path):
        client = FakeClient([VALID_DRIVER_SOURCE, approve_response()])

        source, review = driver_gen.generate_driver(make_spec(), client=client)

        assert source == VALID_DRIVER_SOURCE
        assert review.approved is True
        assert len(client.messages.calls) == 2

    def test_static_failure_then_success_retries_once_with_feedback(self, prompts_dir: Path):
        broken_source = "class Driver:\n    pass\n"
        client = FakeClient([broken_source, VALID_DRIVER_SOURCE, approve_response()])

        source, review = driver_gen.generate_driver(make_spec(), client=client)

        assert source == VALID_DRIVER_SOURCE
        assert review.approved is True
        assert len(client.messages.calls) == 3
        second_draft_content = client.messages.calls[1]["messages"][0]["content"]
        assert "resourcedriver" in second_draft_content.lower()

    def test_static_failure_twice_raises_with_review_none(self, prompts_dir: Path):
        broken_source = "class Driver:\n    pass\n"
        client = FakeClient([broken_source, broken_source])

        with pytest.raises(driver_gen.DriverGenerationFailed) as excinfo:
            driver_gen.generate_driver(make_spec(), client=client)

        assert excinfo.value.source == broken_source
        assert excinfo.value.review is None
        assert len(excinfo.value.reasons) > 0
        assert len(client.messages.calls) == 2

    def test_opus_block_then_approve_retries_once_with_feedback(self, prompts_dir: Path):
        client = FakeClient(
            [
                VALID_DRIVER_SOURCE,
                block_response(["delete() is not idempotent"]),
                VALID_DRIVER_SOURCE,
                approve_response(),
            ]
        )

        source, review = driver_gen.generate_driver(make_spec(), client=client)

        assert source == VALID_DRIVER_SOURCE
        assert review.approved is True
        assert len(client.messages.calls) == 4
        second_draft_content = client.messages.calls[2]["messages"][0]["content"]
        assert "delete() is not idempotent" in second_draft_content

    def test_opus_blocks_twice_raises_with_review(self, prompts_dir: Path):
        client = FakeClient(
            [
                VALID_DRIVER_SOURCE,
                block_response(["delete() is not idempotent"]),
                VALID_DRIVER_SOURCE,
                block_response(["delete() is still not idempotent"]),
            ]
        )

        with pytest.raises(driver_gen.DriverGenerationFailed) as excinfo:
            driver_gen.generate_driver(make_spec(), client=client)

        assert excinfo.value.source == VALID_DRIVER_SOURCE
        assert excinfo.value.review is not None
        assert excinfo.value.review.approved is False
        assert excinfo.value.reasons == ["delete() is still not idempotent"]
        assert len(client.messages.calls) == 4

    def test_static_failure_then_opus_block_raises_no_third_attempt(self, prompts_dir: Path):
        broken_source = "class Driver:\n    pass\n"
        client = FakeClient(
            [
                broken_source,
                VALID_DRIVER_SOURCE,
                block_response(["delete() is not idempotent"]),
            ]
        )

        with pytest.raises(driver_gen.DriverGenerationFailed) as excinfo:
            driver_gen.generate_driver(make_spec(), client=client)

        assert excinfo.value.source == VALID_DRIVER_SOURCE
        assert excinfo.value.reasons == ["delete() is not idempotent"]
        assert len(client.messages.calls) == 3

    def test_not_approved_with_no_blocking_issues_triggers_retry(self, prompts_dir: Path):
        client = FakeClient(
            [
                VALID_DRIVER_SOURCE,
                not_approved_no_issues_response(),
                VALID_DRIVER_SOURCE,
                approve_response(),
            ]
        )

        source, review = driver_gen.generate_driver(make_spec(), client=client)

        assert source == VALID_DRIVER_SOURCE
        assert review.approved is True
        assert len(client.messages.calls) == 4

    def test_not_approved_with_no_blocking_issues_raises_after_max_attempts(
        self, prompts_dir: Path
    ):
        client = FakeClient(
            [
                VALID_DRIVER_SOURCE,
                not_approved_no_issues_response(),
                VALID_DRIVER_SOURCE,
                not_approved_no_issues_response(),
            ]
        )

        with pytest.raises(driver_gen.DriverGenerationFailed) as excinfo:
            driver_gen.generate_driver(make_spec(), client=client)

        assert excinfo.value.review is not None
        assert excinfo.value.review.approved is False
        assert excinfo.value.reasons
        assert len(client.messages.calls) == 4


class TestConstants:
    def test_max_draft_attempts_is_two(self):
        assert driver_gen.MAX_DRAFT_ATTEMPTS == 2
