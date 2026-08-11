import json
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from aiform import llm
from aiform.models import DriverReview, LLMConfig, LLMRoleConfig, ModelSource, PlanReview


class FakeThinkingBlock:
    def __init__(self):
        self.type = "thinking"
        self.thinking = "reasoning about the request..."


class FakeTextBlock:
    def __init__(self, text: str):
        self.type = "text"
        self.text = text


class FakeResponse:
    def __init__(self, text: str | None, *, include_thinking_block: bool = False):
        content = [FakeThinkingBlock()] if include_thinking_block else []
        if text is not None:
            content.append(FakeTextBlock(text))
        self.content = content


class FakeMessages:
    def __init__(self, response_text: str | None, *, include_thinking_block: bool = False):
        self._response_text = response_text
        self._include_thinking_block = include_thinking_block
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return FakeResponse(
            self._response_text, include_thinking_block=self._include_thinking_block
        )


class FakeClient:
    def __init__(self, response_text: str | None, *, include_thinking_block: bool = False):
        self.messages = FakeMessages(response_text, include_thinking_block=include_thinking_block)


@pytest.fixture
def prompts_dir(tmp_path: Path, monkeypatch) -> Path:
    directory = tmp_path / "prompts"
    directory.mkdir()
    (directory / "review_driver.md").write_text("Review the driver source for correctness.\n")
    (directory / "review_plan.md").write_text("Review the plan for safety before apply.\n")
    monkeypatch.setattr(llm, "PROMPTS_DIR", directory)
    return directory


def make_llm_config(
    intent_orchestration_model: str = "claude-sonnet-5",
    code_generator_model: str = "claude-sonnet-5",
    code_review_model: str = "claude-opus-5",
    review_orchestration_model: str = "claude-opus-5",
) -> LLMConfig:
    return LLMConfig(
        intent_orchestration=LLMRoleConfig(
            source=ModelSource.ANTHROPIC, model=intent_orchestration_model
        ),
        code_generator=LLMRoleConfig(source=ModelSource.ANTHROPIC, model=code_generator_model),
        code_review=LLMRoleConfig(source=ModelSource.ANTHROPIC, model=code_review_model),
        review_orchestration=LLMRoleConfig(
            source=ModelSource.ANTHROPIC, model=review_orchestration_model
        ),
    )


class TestNoCredentialsInThisFile:
    def test_source_never_mentions_credentials(self):
        source = Path(llm.__file__).read_text(encoding="utf-8")
        assert "credentials" not in source.lower()


class TestModelSources:
    def test_anthropic_is_the_only_registered_source(self):
        assert set(llm.MODEL_SOURCES) == {ModelSource.ANTHROPIC}

    def test_anthropic_source_dispatches_to_anthropic_call(self):
        assert llm.MODEL_SOURCES[ModelSource.ANTHROPIC] is llm._anthropic_call


class TestIntentOrchestrationCall:
    def test_returns_raw_response_text(self):
        client = FakeClient("hello world")
        result = llm.intent_orchestration_call(
            "system prompt", "user content", client=client, llm_config=make_llm_config()
        )
        assert result == "hello world"

    def test_skips_leading_thinking_block(self):
        client = FakeClient("hello world", include_thinking_block=True)
        result = llm.intent_orchestration_call(
            "system prompt", "user content", client=client, llm_config=make_llm_config()
        )
        assert result == "hello world"

    def test_raises_when_response_has_no_text_block(self):
        client = FakeClient(None, include_thinking_block=True)

        with pytest.raises(RuntimeError):
            llm.intent_orchestration_call(
                "system prompt", "user content", client=client, llm_config=make_llm_config()
            )

    def test_uses_configured_intent_orchestration_model(self):
        client = FakeClient("ignored")
        config = make_llm_config(intent_orchestration_model="claude-sonnet-5-custom")
        llm.intent_orchestration_call("system", "user", client=client, llm_config=config)
        assert client.messages.calls[0]["model"] == "claude-sonnet-5-custom"

    def test_defaults_to_resolve_llm_config_when_not_given(self, monkeypatch):
        client = FakeClient("ignored")
        config = make_llm_config(intent_orchestration_model="from-resolver")
        monkeypatch.setattr(llm.config, "resolve_llm_config", lambda: config)

        llm.intent_orchestration_call("system", "user", client=client)

        assert client.messages.calls[0]["model"] == "from-resolver"

    def test_passes_system_and_user_content(self):
        client = FakeClient("ignored")
        llm.intent_orchestration_call(
            "my system prompt", "my user content", client=client, llm_config=make_llm_config()
        )
        call = client.messages.calls[0]
        assert call["system"] == "my system prompt"
        assert call["messages"] == [{"role": "user", "content": "my user content"}]

    def test_omits_output_config_when_no_schema(self):
        client = FakeClient("ignored")
        llm.intent_orchestration_call("system", "user", client=client, llm_config=make_llm_config())
        assert "output_config" not in client.messages.calls[0]

    def test_sets_output_config_when_schema_given(self):
        client = FakeClient("ignored")
        schema = {"type": "object", "properties": {}}
        llm.intent_orchestration_call(
            "system", "user", output_schema=schema, client=client, llm_config=make_llm_config()
        )
        assert client.messages.calls[0]["output_config"] == {
            "format": {"type": "json_schema", "schema": schema}
        }

    def test_default_max_tokens(self):
        client = FakeClient("ignored")
        llm.intent_orchestration_call("system", "user", client=client, llm_config=make_llm_config())
        assert client.messages.calls[0]["max_tokens"] == 4096

    def test_max_tokens_override(self):
        client = FakeClient("ignored")
        llm.intent_orchestration_call(
            "system", "user", max_tokens=2048, client=client, llm_config=make_llm_config()
        )
        assert client.messages.calls[0]["max_tokens"] == 2048

    def test_unregistered_source_raises_key_error(self):
        client = FakeClient("ignored")
        config = make_llm_config()
        config.intent_orchestration.source = "totally-unregistered"  # type: ignore[assignment]

        with pytest.raises(KeyError):
            llm.intent_orchestration_call("system", "user", client=client, llm_config=config)


class TestCodeGeneratorCall:
    def test_returns_raw_response_text(self):
        client = FakeClient("driver source code")
        result = llm.code_generator_call(
            "system prompt", "user content", client=client, llm_config=make_llm_config()
        )
        assert result == "driver source code"

    def test_skips_leading_thinking_block(self):
        client = FakeClient("driver source code", include_thinking_block=True)
        result = llm.code_generator_call(
            "system prompt", "user content", client=client, llm_config=make_llm_config()
        )
        assert result == "driver source code"

    def test_raises_when_response_has_no_text_block(self):
        client = FakeClient(None, include_thinking_block=True)

        with pytest.raises(RuntimeError):
            llm.code_generator_call(
                "system prompt", "user content", client=client, llm_config=make_llm_config()
            )

    def test_uses_configured_code_generator_model(self):
        client = FakeClient("ignored")
        config = make_llm_config(code_generator_model="claude-sonnet-5-custom")
        llm.code_generator_call("system", "user", client=client, llm_config=config)
        assert client.messages.calls[0]["model"] == "claude-sonnet-5-custom"

    def test_defaults_to_resolve_llm_config_when_not_given(self, monkeypatch):
        client = FakeClient("ignored")
        config = make_llm_config(code_generator_model="from-resolver")
        monkeypatch.setattr(llm.config, "resolve_llm_config", lambda: config)

        llm.code_generator_call("system", "user", client=client)

        assert client.messages.calls[0]["model"] == "from-resolver"

    def test_passes_system_and_user_content(self):
        client = FakeClient("ignored")
        llm.code_generator_call(
            "my system prompt", "my user content", client=client, llm_config=make_llm_config()
        )
        call = client.messages.calls[0]
        assert call["system"] == "my system prompt"
        assert call["messages"] == [{"role": "user", "content": "my user content"}]

    def test_omits_output_config_when_no_schema(self):
        client = FakeClient("ignored")
        llm.code_generator_call("system", "user", client=client, llm_config=make_llm_config())
        assert "output_config" not in client.messages.calls[0]

    def test_default_max_tokens(self):
        client = FakeClient("ignored")
        llm.code_generator_call("system", "user", client=client, llm_config=make_llm_config())
        assert client.messages.calls[0]["max_tokens"] == 4096

    def test_max_tokens_override(self):
        client = FakeClient("ignored")
        llm.code_generator_call(
            "system", "user", max_tokens=8192, client=client, llm_config=make_llm_config()
        )
        assert client.messages.calls[0]["max_tokens"] == 8192

    def test_unregistered_source_raises_key_error(self):
        client = FakeClient("ignored")
        config = make_llm_config()
        config.code_generator.source = "totally-unregistered"  # type: ignore[assignment]

        with pytest.raises(KeyError):
            llm.code_generator_call("system", "user", client=client, llm_config=config)

    def test_intent_orchestration_and_code_generator_resolve_independently(self):
        client = FakeClient("ignored")
        config = make_llm_config(
            intent_orchestration_model="intent-model", code_generator_model="codegen-model"
        )

        llm.intent_orchestration_call("system", "user", client=client, llm_config=config)
        llm.code_generator_call("system", "user", client=client, llm_config=config)

        assert client.messages.calls[0]["model"] == "intent-model"
        assert client.messages.calls[1]["model"] == "codegen-model"


class TestReviewDriver:
    def test_returns_driver_review(self, prompts_dir: Path):
        response_text = json.dumps(
            {"approved": True, "concerns": ["minor nit"], "blocking_issues": []}
        )
        client = FakeClient(response_text)

        review = llm.review_driver(
            "driver source code", client=client, llm_config=make_llm_config()
        )

        assert isinstance(review, DriverReview)
        assert review.approved is True
        assert review.concerns == ["minor nit"]
        assert review.blocking_issues == []
        assert isinstance(review.reviewed_at, datetime)

    def test_stamps_configured_review_model(self, prompts_dir: Path):
        response_text = json.dumps({"approved": True, "concerns": [], "blocking_issues": []})
        client = FakeClient(response_text)
        config = make_llm_config(code_review_model="claude-opus-5-custom")

        review = llm.review_driver("driver source code", client=client, llm_config=config)

        assert review.model == "claude-opus-5-custom"

    def test_uses_configured_review_model_and_driver_review_schema(self, prompts_dir: Path):
        response_text = json.dumps({"approved": True, "concerns": [], "blocking_issues": []})
        client = FakeClient(response_text)
        config = make_llm_config(code_review_model="claude-opus-5-custom")

        llm.review_driver("driver source code", client=client, llm_config=config)

        call = client.messages.calls[0]
        assert call["model"] == "claude-opus-5-custom"
        assert call["output_config"]["format"]["schema"] == llm.DRIVER_REVIEW_SCHEMA

    def test_loads_review_driver_prompt_as_system(self, prompts_dir: Path):
        response_text = json.dumps({"approved": True, "concerns": [], "blocking_issues": []})
        client = FakeClient(response_text)

        llm.review_driver("driver source code", client=client, llm_config=make_llm_config())

        call = client.messages.calls[0]
        assert call["system"] == (prompts_dir / "review_driver.md").read_text()
        assert call["messages"] == [{"role": "user", "content": "driver source code"}]

    def test_raises_when_approved_with_blocking_issues(self, prompts_dir: Path):
        response_text = json.dumps(
            {"approved": True, "concerns": [], "blocking_issues": ["delete() is not idempotent"]}
        )
        client = FakeClient(response_text)

        with pytest.raises(ValidationError):
            llm.review_driver("driver source code", client=client, llm_config=make_llm_config())


class TestReviewPlan:
    def test_returns_plan_review(self, prompts_dir: Path):
        response_text = json.dumps(
            {
                "safe_to_proceed": False,
                "flags": [
                    {
                        "resource_key": "digitalocean.compute.telleztec-app-01",
                        "concern": "unexpected destroy",
                        "severity": "block",
                    }
                ],
            }
        )
        client = FakeClient(response_text)

        review = llm.review_plan("plan summary text", client=client, llm_config=make_llm_config())

        assert isinstance(review, PlanReview)
        assert review.safe_to_proceed is False
        assert len(review.flags) == 1
        assert review.flags[0].severity == "block"

    def test_uses_configured_review_model_and_plan_review_schema(self, prompts_dir: Path):
        response_text = json.dumps({"safe_to_proceed": True, "flags": []})
        client = FakeClient(response_text)
        config = make_llm_config(review_orchestration_model="claude-opus-5-custom")

        llm.review_plan("plan summary text", client=client, llm_config=config)

        call = client.messages.calls[0]
        assert call["model"] == "claude-opus-5-custom"
        assert call["output_config"]["format"]["schema"] == llm.PLAN_REVIEW_SCHEMA

    def test_loads_review_plan_prompt_as_system(self, prompts_dir: Path):
        response_text = json.dumps({"safe_to_proceed": True, "flags": []})
        client = FakeClient(response_text)

        llm.review_plan("plan summary text", client=client, llm_config=make_llm_config())

        call = client.messages.calls[0]
        assert call["system"] == (prompts_dir / "review_plan.md").read_text()
        assert call["messages"] == [{"role": "user", "content": "plan summary text"}]


class TestRealPromptFiles:
    def test_review_driver_prompt_exists_and_is_nonempty(self):
        path = llm.PROMPTS_DIR / "review_driver.md"
        assert path.exists()
        assert len(path.read_text(encoding="utf-8").strip()) > 0

    def test_review_plan_prompt_exists_and_is_nonempty(self):
        path = llm.PROMPTS_DIR / "review_plan.md"
        assert path.exists()
        assert len(path.read_text(encoding="utf-8").strip()) > 0
