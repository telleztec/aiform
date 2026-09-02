# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

import ast
import json
import logging
from datetime import datetime
from pathlib import Path

import anthropic
import httpx
import pytest
from pydantic import ValidationError

from aiform import llm
from aiform.models import (
    DriverReview,
    KeyState,
    LLMConfig,
    LLMRoleConfig,
    ModelSource,
    PlanReview,
)


class FakeThinkingBlock:
    def __init__(self):
        self.type = "thinking"
        self.thinking = "reasoning about the request..."


class FakeTextBlock:
    def __init__(self, text: str):
        self.type = "text"
        self.text = text


class FakeUsageDetails:
    def __init__(self, thinking_tokens: int | None):
        self.thinking_tokens = thinking_tokens


class FakeUsage:
    def __init__(
        self,
        *,
        input_tokens: int = 10,
        output_tokens: int = 20,
        thinking_tokens: int | None = None,
    ):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.output_tokens_details = (
            FakeUsageDetails(thinking_tokens) if thinking_tokens is not None else None
        )


class FakeResponse:
    def __init__(
        self,
        text: str | None,
        *,
        include_thinking_block: bool = False,
        stop_reason: str = "end_turn",
        usage: FakeUsage | None = None,
    ):
        content = [FakeThinkingBlock()] if include_thinking_block else []
        if text is not None:
            content.append(FakeTextBlock(text))
        self.content = content
        self.stop_reason = stop_reason
        self.usage = usage if usage is not None else FakeUsage()


class FakeMessages:
    def __init__(
        self,
        response_text: str | None,
        *,
        include_thinking_block: bool = False,
        stop_reason: str = "end_turn",
        usage: FakeUsage | None = None,
    ):
        self._response_text = response_text
        self._include_thinking_block = include_thinking_block
        self._stop_reason = stop_reason
        self._usage = usage
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return FakeResponse(
            self._response_text,
            include_thinking_block=self._include_thinking_block,
            stop_reason=self._stop_reason,
            usage=self._usage,
        )


class FakeClient:
    def __init__(
        self,
        response_text: str | None,
        *,
        include_thinking_block: bool = False,
        stop_reason: str = "end_turn",
        usage: FakeUsage | None = None,
    ):
        self.messages = FakeMessages(
            response_text,
            include_thinking_block=include_thinking_block,
            stop_reason=stop_reason,
            usage=usage,
        )
        self.closed = False

    def close(self):
        self.closed = True


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
    intent_orchestration_max_tokens: int = 4096,
    code_generator_max_tokens: int = 4096,
    code_review_max_tokens: int = 8192,
    review_orchestration_max_tokens: int = 8192,
) -> LLMConfig:
    return LLMConfig(
        intent_orchestration=LLMRoleConfig(
            source=ModelSource.ANTHROPIC,
            model=intent_orchestration_model,
            max_tokens=intent_orchestration_max_tokens,
        ),
        code_generator=LLMRoleConfig(
            source=ModelSource.ANTHROPIC,
            model=code_generator_model,
            max_tokens=code_generator_max_tokens,
        ),
        code_review=LLMRoleConfig(
            source=ModelSource.ANTHROPIC, model=code_review_model, max_tokens=code_review_max_tokens
        ),
        review_orchestration=LLMRoleConfig(
            source=ModelSource.ANTHROPIC,
            model=review_orchestration_model,
            max_tokens=review_orchestration_max_tokens,
        ),
    )


class TestNoCredentialsInThisFile:
    def test_source_never_mentions_credentials(self):
        source = Path(llm.__file__).read_text(encoding="utf-8")
        assert "credentials" not in source.lower()


# anthropic.Client is the same object as anthropic.Anthropic, so a call
# through either name constructs a client and has to be caught.
_SDK_CLIENT_NAMES = frozenset({"Anthropic", "Client"})


def _package_modules() -> list[Path]:
    return sorted(Path(llm.__file__).resolve().parent.rglob("*.py"))


def _sdk_construction_sites(tree: ast.Module, filename: str) -> list[str]:
    """Every `anthropic.Anthropic(...)`/`anthropic.Client(...)` call, labelled
    with the function that encloses it (`<module>` when there is none)."""
    sites = []

    def visit(node, scope):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                visit(child, child.name)
                continue
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr in _SDK_CLIENT_NAMES
                and isinstance(child.func.value, ast.Name)
                and child.func.value.id == "anthropic"
            ):
                sites.append(f"{filename}:{scope}")
            visit(child, scope)

    visit(tree, "<module>")
    return sites


class TestBuildClientIsTheOnlyConstructor:
    """The redirect refusal is only worth anything if it holds at every
    construction site: #97 hardened the probe and left the model-call path
    following redirects until #101, and the CLI's own wrapper was a third
    site neither of them touched. Pinned structurally, the way CLAUDE.md
    pins the "credentials" rule, rather than left to prose. (drivers/ is
    covered separately -- driver_gen rejects a driver that so much as
    imports anthropic.)
    """

    def test_no_other_module_constructs_an_sdk_client(self):
        sites = []
        for path in _package_modules():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            sites += _sdk_construction_sites(tree, path.name)

        assert sites == ["llm.py:build_client"]

    def test_every_module_reaches_the_sdk_through_the_bare_module_name(self):
        # What makes the check above complete rather than merely suggestive:
        # `import anthropic as a` or `from anthropic import Anthropic` would
        # both put a construction call beyond an attribute match on
        # `anthropic.<name>`, and module-level rebinding is the likeliest
        # shape for a "one client per process" regression. Forbidding the
        # aliases is cheaper than chasing them.
        offenders = []
        for path in _package_modules():
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    offenders += [
                        f"{path.name}:{node.lineno}"
                        for alias in node.names
                        if alias.name == "anthropic" and alias.asname is not None
                    ]
                elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                    "anthropic"
                ):
                    offenders.append(f"{path.name}:{node.lineno}")

        assert offenders == []


class TestModelSources:
    def test_anthropic_is_the_only_registered_source(self):
        assert set(llm.MODEL_SOURCES) == {ModelSource.ANTHROPIC}

    def test_anthropic_source_dispatches_to_anthropic_call(self):
        assert llm.MODEL_SOURCES[ModelSource.ANTHROPIC] is llm._anthropic_call

    def test_anthropic_call_requires_max_tokens_explicitly(self):
        # No hardcoded fallback default -- every real call path resolves
        # max_tokens from a role's own config and must pass it explicitly;
        # a caller that forgets is a bug, not a silent revert to a shared
        # constant.
        client = FakeClient("ignored")
        with pytest.raises(TypeError):
            llm._anthropic_call("claude-sonnet-5", "system", "user", client=client)

    def test_returns_model_call_result_with_response_metadata(self):
        client = FakeClient(
            "hello world",
            stop_reason="end_turn",
            usage=FakeUsage(input_tokens=100, output_tokens=42),
        )

        result = llm._anthropic_call(
            "claude-sonnet-5", "system", "user", max_tokens=4096, client=client
        )

        assert result.text == "hello world"
        assert result.stop_reason == "end_turn"
        assert result.input_tokens == 100
        assert result.output_tokens == 42
        assert result.thinking_tokens is None
        assert result.duration_ms >= 0

    def test_thinking_tokens_extracted_when_present(self):
        client = FakeClient("hello world", usage=FakeUsage(thinking_tokens=777))

        result = llm._anthropic_call(
            "claude-sonnet-5", "system", "user", max_tokens=4096, client=client
        )

        assert result.thinking_tokens == 777


class TestAnthropicCallRefusesRedirects:
    """The model-call half of the rule TestVerifyApiKeyRefusesRedirects
    enforces on the probe, and the more damaging half. The probe leaks only
    x-api-key and gets a forged endpoint; this path can leak the system
    prompt and the user content too -- a plan summary, or a driver's source
    -- and parses the target's 200 back as a model response that plan/apply
    acts on. #101.

    Both statuses are pinned because httpx does not treat them alike: it
    downgrades POST to GET on 301/302/303, so those carry the key with an
    empty body, while 307/308 preserve the method and carry the request body
    with it. 302 is what a captive portal sends; 307 is where the prompt
    itself would travel.
    """

    @pytest.mark.parametrize("status", [302, 307])
    def test_the_key_and_prompt_never_reach_a_redirect_target(
        self, status, api_key_set, monkeypatch
    ):
        # The test the fix exists for. Only the transport is injected -- the
        # client is built by _anthropic_call itself, through the real SDK and
        # the real httpx redirect machinery, so follow_redirects is the
        # production value and not the test's.
        #
        # The counterfactual: drop follow_redirects=False and keep
        # http_client=, and this records a second hop to evil.example.com
        # carrying x-api-key -- and, on the 307, the prompt as well -- and
        # returns "forged" as the model's answer. Drop http_client= entirely
        # and the patched constructor below is never called, so guard_client
        # raises rather than letting a live request off the unit suite.
        hops = []

        def handler(request):
            hops.append((request, request.read()))
            if request.url.host == "api.anthropic.com":
                return httpx.Response(
                    status, headers={"Location": "https://evil.example.com/v1/messages"}
                )
            return httpx.Response(
                200,
                json={
                    "id": "msg_forged",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-sonnet-5",
                    "content": [{"type": "text", "text": "forged"}],
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            )

        real_http_client = llm.anthropic.DefaultHttpxClient
        monkeypatch.setattr(
            llm.anthropic,
            "DefaultHttpxClient",
            lambda **kwargs: real_http_client(**kwargs, transport=httpx.MockTransport(handler)),
        )

        real_anthropic = llm.anthropic.Anthropic

        def guard_client(**kwargs):
            assert "http_client" in kwargs, "the call must be built with an explicit http_client"
            return real_anthropic(**kwargs)

        monkeypatch.setattr(llm.anthropic, "Anthropic", guard_client)

        # Caught rather than pytest.raises'd so the hop assertions below run
        # first: under the counterfactual the failure then names the leak
        # ("evil.example.com" in the recorded hosts) instead of the less
        # legible "DID NOT RAISE".
        outcome = None
        try:
            outcome = llm._anthropic_call(
                "claude-sonnet-5",
                "system prompt",
                "SENTINEL-PROMPT-BODY",
                max_tokens=64,
            )
        except anthropic.APIStatusError as exc:
            outcome = exc

        assert [request.url.host for request, _ in hops] == ["api.anthropic.com"]
        # Both assertions are about the one hop that IS allowed -- the
        # outbound POST to the real API -- and so hold identically on both
        # statuses; what differs between them is only what a *followed*
        # redirect would have carried onward, which no assertion here can
        # reach because there is no second hop to inspect. The 307 is
        # parametrized in anyway: it is the status where the counterfactual
        # forwards this body, so the refusal has to cover it, and
        # outcome.status_code below is what proves it did.
        assert "x-api-key" in hops[0][0].headers
        assert b"SENTINEL-PROMPT-BODY" in hops[0][1]
        # The redirect target's 200 must never come back as a model answer.
        assert not isinstance(outcome, llm.ModelCallResult)
        # status_code, not just "it raised". An httpx.MockTransport injected
        # into a client from a different stack -- anthropic 1.x builds
        # DefaultHttpxClient on httpx2 -- is foreign to it, so the request
        # dies as an APIConnectionError, and every assertion above still
        # holds with follow_redirects=True. APIConnectionError is not an
        # APIStatusError, so a stack mismatch fails loudly instead of going
        # quietly inert. This is the model path's equivalent of the probe
        # test's canned-detail pin; there is no detail string here.
        assert isinstance(outcome, anthropic.APIStatusError)
        assert outcome.status_code == status

    def test_the_client_it_builds_refuses_redirects(self, api_key_set, monkeypatch):
        # The guard above only holds because the constructed client refuses
        # redirects. Pin it directly, so the SDK restoring its
        # follow_redirects default is a test failure and not a silent leak.
        built = {}

        def fake_anthropic(**kwargs):
            built.update(kwargs)
            return FakeClient("ok")

        monkeypatch.setattr(llm.anthropic, "Anthropic", fake_anthropic)

        llm._anthropic_call("claude-sonnet-5", "system", "user", max_tokens=64)

        assert built["http_client"].follow_redirects is False

    def test_the_client_it_builds_is_closed(self, api_key_set, monkeypatch):
        # http_client= costs the SDK wrapper's __del__, which is what closed
        # the pool before; without an explicit close the socket outlives the
        # call, once per model call on the plan/apply path.
        client = FakeClient("ok")
        monkeypatch.setattr(llm.anthropic, "Anthropic", lambda **kwargs: client)

        llm._anthropic_call("claude-sonnet-5", "system", "user", max_tokens=64)

        assert client.closed

    def test_the_client_it_builds_is_closed_even_when_the_call_raises(
        self, api_key_set, monkeypatch
    ):
        client = FakeClient(None)
        monkeypatch.setattr(llm.anthropic, "Anthropic", lambda **kwargs: client)

        with pytest.raises(RuntimeError):
            llm._anthropic_call("claude-sonnet-5", "system", "user", max_tokens=64)

        assert client.closed

    def test_an_injected_client_is_left_open(self):
        # It belongs to the caller, who reuses it across every call of a run
        # (cli._CountingClient does exactly that).
        client = FakeClient("ok")

        llm._anthropic_call("claude-sonnet-5", "system", "user", max_tokens=64, client=client)

        assert not client.closed


class TestBuildClient:
    def test_refuses_redirects(self, monkeypatch):
        built = {}

        def fake_anthropic(**kwargs):
            built.update(kwargs)
            return FakeClient("ok")

        monkeypatch.setattr(llm.anthropic, "Anthropic", fake_anthropic)

        llm.build_client()

        assert built["http_client"].follow_redirects is False

    def test_passes_the_callers_kwargs_through(self, monkeypatch):
        # verify_api_key builds through this with its own timeout and
        # max_retries; the hardening must not cost it those.
        built = {}

        def fake_anthropic(**kwargs):
            built.update(kwargs)
            return FakeClient("ok")

        monkeypatch.setattr(llm.anthropic, "Anthropic", fake_anthropic)

        llm.build_client(timeout=3.0, max_retries=0)

        assert built["timeout"] == 3.0
        assert built["max_retries"] == 0
        assert built["http_client"].follow_redirects is False


class TestCallLogging:
    def test_intent_orchestration_call_logs_role_model_and_response_metadata(self, caplog):
        caplog.set_level("INFO", logger="aiform.llm")
        client = FakeClient(
            "ignored",
            stop_reason="end_turn",
            usage=FakeUsage(input_tokens=10, output_tokens=20, thinking_tokens=5),
        )

        llm.intent_orchestration_call("system", "user", client=client, llm_config=make_llm_config())

        record = next(r for r in caplog.records if r.role == "intent_orchestration")
        assert record.model == "claude-sonnet-5"
        assert record.stop_reason == "end_turn"
        assert record.input_tokens == 10
        assert record.output_tokens == 20
        assert record.thinking_tokens == 5
        assert record.levelname == "INFO"

    def test_max_tokens_stop_reason_logs_a_warning(self, caplog):
        caplog.set_level("INFO", logger="aiform.llm")
        client = FakeClient("truncated json...", stop_reason="max_tokens")

        llm.intent_orchestration_call("system", "user", client=client, llm_config=make_llm_config())

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "truncated" in warnings[0].getMessage()
        assert warnings[0].role == "intent_orchestration"

    def test_end_turn_stop_reason_logs_no_warning(self, caplog):
        caplog.set_level("INFO", logger="aiform.llm")
        client = FakeClient("ignored", stop_reason="end_turn")

        llm.intent_orchestration_call("system", "user", client=client, llm_config=make_llm_config())

        assert not [r for r in caplog.records if r.levelno == logging.WARNING]

    def test_review_driver_logs_decision_counts(self, caplog, prompts_dir: Path):
        caplog.set_level("INFO", logger="aiform.llm")
        response_text = json.dumps(
            {"approved": False, "concerns": ["a", "b"], "blocking_issues": ["c"]}
        )
        client = FakeClient(response_text)

        llm.review_driver("driver source code", client=client, llm_config=make_llm_config())

        decision = next(r for r in caplog.records if hasattr(r, "approved"))
        assert decision.approved is False
        assert decision.concerns_count == 2
        assert decision.blocking_issues_count == 1

    def test_review_plan_logs_decision_counts(self, caplog, prompts_dir: Path):
        caplog.set_level("INFO", logger="aiform.llm")
        response_text = json.dumps(
            {
                "safe_to_proceed": False,
                "flags": [
                    {
                        "resource_key": "digitalocean.compute.x",
                        "concern": "unexpected destroy",
                        "severity": "block",
                    }
                ],
            }
        )
        client = FakeClient(response_text)

        llm.review_plan("plan summary", client=client, llm_config=make_llm_config())

        decision = next(r for r in caplog.records if hasattr(r, "safe_to_proceed"))
        assert decision.safe_to_proceed is False
        assert decision.flags_count == 1

    def test_review_driver_never_logs_concern_text_only_counts(self, caplog, prompts_dir: Path):
        caplog.set_level("INFO", logger="aiform.llm")
        response_text = json.dumps(
            {
                "approved": False,
                "concerns": [],
                "blocking_issues": ["a very specific sensitive-looking finding"],
            }
        )
        client = FakeClient(response_text)

        llm.review_driver("driver source code", client=client, llm_config=make_llm_config())

        all_text = " ".join(r.getMessage() for r in caplog.records)
        assert "sensitive-looking finding" not in all_text
        for record in caplog.records:
            assert "sensitive-looking finding" not in str(record.__dict__)


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

    def test_uses_role_configured_max_tokens_not_a_hardcoded_default(self):
        client = FakeClient("ignored")
        config = make_llm_config(intent_orchestration_max_tokens=12345)
        llm.intent_orchestration_call("system", "user", client=client, llm_config=config)
        assert client.messages.calls[0]["max_tokens"] == 12345

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

    def test_uses_role_configured_max_tokens_not_a_hardcoded_default(self):
        client = FakeClient("ignored")
        config = make_llm_config(code_generator_max_tokens=12345)
        llm.code_generator_call("system", "user", client=client, llm_config=config)
        assert client.messages.calls[0]["max_tokens"] == 12345

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

    def test_uses_role_configured_max_tokens(self, prompts_dir: Path):
        response_text = json.dumps({"approved": True, "concerns": [], "blocking_issues": []})
        client = FakeClient(response_text)
        config = make_llm_config(code_review_max_tokens=12345)

        llm.review_driver("driver source code", client=client, llm_config=config)

        assert client.messages.calls[0]["max_tokens"] == 12345

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

    def test_uses_role_configured_max_tokens(self, prompts_dir: Path):
        response_text = json.dumps({"safe_to_proceed": True, "flags": []})
        client = FakeClient(response_text)
        config = make_llm_config(review_orchestration_max_tokens=12345)

        llm.review_plan("plan summary text", client=client, llm_config=config)

        assert client.messages.calls[0]["max_tokens"] == 12345


class TestRealPromptFiles:
    def test_review_driver_prompt_exists_and_is_nonempty(self):
        path = llm.PROMPTS_DIR / "review_driver.md"
        assert path.exists()
        assert len(path.read_text(encoding="utf-8").strip()) > 0

    def test_review_plan_prompt_exists_and_is_nonempty(self):
        path = llm.PROMPTS_DIR / "review_plan.md"
        assert path.exists()
        assert len(path.read_text(encoding="utf-8").strip()) > 0


class FakeHTTPResponse:
    """The subset of an HTTP response anthropic.APIStatusError reads.

    Duck-typed rather than a real response object, which is cheaper and
    needs no transport. The original reason given -- avoiding a dependency
    on which anthropic major resolved, since 1.x is built on httpx2 -- no
    longer applies: pyproject caps anthropic<1, so the stack is now
    deterministic and this module imports httpx directly at the top.
    """

    def __init__(self, status_code: int):
        self.status_code = status_code
        self.request = object()
        self.headers: dict[str, str] = {}


def _api_error(status_code: int, message: str) -> anthropic.APIStatusError:
    return anthropic.APIStatusError(
        message, response=FakeHTTPResponse(status_code), body={"error": {"message": message}}
    )


class FakeModels:
    """Duck-typed like anthropic.Anthropic().models -- verify_api_key()
    calls only .list()."""

    def __init__(self, raises: Exception | None = None):
        self._raises = raises
        self.calls: list[dict] = []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return object()


class FakeProbeClient:
    def __init__(self, raises: Exception | None = None):
        self.models = FakeModels(raises)
        self.messages = FakeMessages([])
        self.closed = False

    def close(self):
        self.closed = True


@pytest.fixture
def api_key_set(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    # The SDK reads ANTHROPIC_BASE_URL when no base_url is passed, so a
    # developer or runner with the gateway variable exported would send these
    # probes at a host the tests do not expect -- the redirect test asserts on
    # the host it reached. Supported in production, pinned out here.
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)


class TestVerifyApiKey:
    def test_unset_key_is_missing_without_calling_the_api(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        client = FakeProbeClient()

        result = llm.verify_api_key(client=client)

        assert result.state is KeyState.MISSING
        assert result.detail is None
        assert client.models.calls == []

    def test_accepted_key_is_ok(self, api_key_set):
        result = llm.verify_api_key(client=FakeProbeClient())

        assert result.state is KeyState.OK
        assert result.detail is None

    def test_probes_the_free_models_endpoint_not_messages(self, api_key_set):
        client = FakeProbeClient()

        llm.verify_api_key(client=client)

        assert len(client.models.calls) == 1
        assert client.messages.calls == []

    def test_identity_linked_key_400s_and_is_rejected(self, api_key_set):
        # The bug this function exists for: an identity-linked key returns
        # 400 on every endpoint, not 401, so treating only 401/403 as
        # rejection would miss exactly the case that motivated it.
        client = FakeProbeClient(_api_error(400, "workspace is not accessible"))

        result = llm.verify_api_key(client=client)

        assert result.state is KeyState.REJECTED
        assert "workspace is not accessible" in result.detail

    def test_invalid_key_401_is_rejected(self, api_key_set):
        client = FakeProbeClient(_api_error(401, "invalid x-api-key"))

        result = llm.verify_api_key(client=client)

        assert result.state is KeyState.REJECTED
        assert "invalid x-api-key" in result.detail

    def test_forbidden_key_403_is_rejected(self, api_key_set):
        client = FakeProbeClient(_api_error(403, "permission denied"))

        result = llm.verify_api_key(client=client)

        assert result.state is KeyState.REJECTED

    @pytest.mark.parametrize("status_code", [404, 405])
    def test_wrong_endpoint_is_unverified_not_a_key_verdict(self, api_key_set, status_code):
        # ANTHROPIC_BASE_URL pointing at a gateway that proxies /v1/messages
        # but not /v1/models answers 404 for a key that works. The status is
        # a verdict on the endpoint, not the credential, so reporting it as
        # rejected sends the user to rotate a working key. The provider
        # probe's test_bad_endpoint_is_unverified_not_a_token_verdict
        # (tests/test_cli.py) is this test's other half.
        client = FakeProbeClient(_api_error(status_code, "not found"))

        assert llm.verify_api_key(client=client).state is KeyState.UNVERIFIED

    @pytest.mark.parametrize("status_code", [408, 429])
    def test_rate_limited_or_timed_out_is_unverified_not_rejected(self, api_key_set, status_code):
        # A busy org key routinely 429s. Reporting that as a rejected key
        # sends the user to rotate a credential that works. These two are
        # the reason the verdict set is enumerated rather than derived from
        # the status class -- a bare `< 500` test gets them wrong.
        client = FakeProbeClient(_api_error(status_code, "rate limit exceeded"))

        assert llm.verify_api_key(client=client).state is KeyState.UNVERIFIED

    def test_server_error_is_unverified_not_rejected(self, api_key_set):
        # A 500 is Anthropic's problem, not the key's -- reporting it as a
        # bad key would send the user to rotate a credential that works.
        client = FakeProbeClient(_api_error(500, "internal server error"))

        result = llm.verify_api_key(client=client)

        assert result.state is KeyState.UNVERIFIED

    def test_connection_error_is_unverified_not_rejected(self, api_key_set):
        # Offline must never be reported as an invalid key.
        client = FakeProbeClient(anthropic.APIConnectionError(request=object()))

        result = llm.verify_api_key(client=client)

        assert result.state is KeyState.UNVERIFIED
        assert result.detail

    def test_never_raises_on_a_bad_key(self, api_key_set):
        client = FakeProbeClient(_api_error(401, "nope"))

        llm.verify_api_key(client=client)


class TestVerifyApiKeyRefusesRedirects:
    """The Anthropic half of the rule tests/test_cli.py already enforces on
    the provider probe (test_redirect_is_refused_not_followed,
    test_opener_does_not_follow_redirects). The reasoning recorded there --
    "requests and httpx both drop it" -- is about the Authorization header;
    this SDK authenticates with x-api-key, which httpx's cross-origin
    stripping does not cover. #97.
    """

    def test_the_key_never_reaches_a_redirect_target(self, api_key_set, monkeypatch):
        # The test the fix exists for. Only the transport is injected -- the
        # client is built by verify_api_key itself, through the real SDK and
        # the real httpx redirect machinery, so follow_redirects is the
        # production value and not the test's.
        #
        # The counterfactual is worth stating precisely, because the obvious
        # one is wrong. Drop follow_redirects=False and keep http_client=, and
        # this records a second hop to evil.example.com with x-api-key intact
        # and returns OK -- the #97 bug. Drop http_client= entirely and the
        # patched constructor below is never called at all, so guard_client
        # raises rather than letting the probe build a real transport and put
        # a live request on the network from the unit suite.
        hops = []

        def handler(request):
            hops.append(request)
            if request.url.host == "api.anthropic.com":
                return httpx.Response(
                    302, headers={"Location": "https://evil.example.com/v1/models"}
                )
            return httpx.Response(200, json={"data": [], "has_more": False})

        real_http_client = llm.anthropic.DefaultHttpxClient
        monkeypatch.setattr(
            llm.anthropic,
            "DefaultHttpxClient",
            lambda **kwargs: real_http_client(**kwargs, transport=httpx.MockTransport(handler)),
        )

        real_anthropic = llm.anthropic.Anthropic

        def guard_client(**kwargs):
            assert "http_client" in kwargs, "the probe must be built with an explicit http_client"
            return real_anthropic(**kwargs)

        monkeypatch.setattr(llm.anthropic, "Anthropic", guard_client)

        result = llm.verify_api_key()

        assert [request.url.host for request in hops] == ["api.anthropic.com"]
        # The header that makes this worth testing: it is sent, and httpx
        # would have carried it across the redirect.
        assert "x-api-key" in hops[0].headers
        assert result.state is KeyState.UNVERIFIED
        # UNVERIFIED alone is too weak to carry this test. An httpx.MockTransport
        # injected into a client from a different stack -- anthropic 1.x builds
        # DefaultHttpxClient on httpx2 -- is foreign to it, so the request dies as
        # an APIConnectionError, which also maps to UNVERIFIED. Every assertion
        # above then holds with follow_redirects=True and the test goes quietly
        # inert. Pinning the canned 3xx detail proves the probe actually refused
        # a redirect rather than failing to connect, so a stack mismatch fails
        # loudly instead.
        assert result.detail == "unexpected redirect (HTTP 302)"

    def test_probe_client_is_built_with_redirects_off(self, api_key_set, monkeypatch):
        # The guard above only holds because the constructed client refuses
        # redirects. Pin it directly, so the SDK restoring its
        # follow_redirects default is a test failure and not a silent leak.
        built = {}

        def fake_anthropic(**kwargs):
            built.update(kwargs)
            return FakeProbeClient()

        monkeypatch.setattr(llm.anthropic, "Anthropic", fake_anthropic)

        llm.verify_api_key()

        assert built["http_client"].follow_redirects is False

    @pytest.mark.parametrize("status_code", [301, 302, 303, 307, 308])
    def test_redirect_is_unverified_not_a_key_verdict(self, api_key_set, status_code):
        # A 3xx is not a verdict on the credential -- and it is the one
        # status that must not fall into the benign bucket either.
        client = FakeProbeClient(_api_error(status_code, "moved"))

        result = llm.verify_api_key(client=client)

        assert result.state is KeyState.UNVERIFIED
        assert "redirect" in result.detail
        assert str(status_code) in result.detail

    def test_redirect_body_is_not_echoed(self, api_key_set):
        # The body of a 3xx from a captive portal or a hostile base URL is
        # attacker-authored text, and _api_error_detail would lift its
        # error.message straight into what init prints.
        client = FakeProbeClient(_api_error(302, "visit https://evil.example.com to continue"))

        assert "evil.example.com" not in llm.verify_api_key(client=client).detail

    def test_the_probe_client_it_builds_is_closed(self, api_key_set, monkeypatch):
        # http_client= costs the SDK wrapper's __del__, which is what closed
        # the pool before; without an explicit close the probe's socket
        # outlives init.
        probe = FakeProbeClient()
        monkeypatch.setattr(llm.anthropic, "Anthropic", lambda **kwargs: probe)

        llm.verify_api_key()

        assert probe.closed

    def test_an_injected_client_is_left_open(self, api_key_set):
        # It belongs to the caller, who may reuse it.
        client = FakeProbeClient()

        llm.verify_api_key(client=client)

        assert not client.closed
