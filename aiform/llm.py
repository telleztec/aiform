import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anthropic

from aiform import config, log
from aiform.models import DriverReview, LLMConfig, LLMRoleConfig, ModelSource, PlanReview

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


@dataclass(frozen=True)
class ModelCallResult:
    text: str
    stop_reason: str
    input_tokens: int
    output_tokens: int
    thinking_tokens: int | None
    duration_ms: int


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


DRIVER_REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "approved": {"type": "boolean"},
        "concerns": {"type": "array", "items": {"type": "string"}},
        "blocking_issues": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["approved", "concerns", "blocking_issues"],
    "additionalProperties": False,
}

PLAN_REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "safe_to_proceed": {"type": "boolean"},
        "flags": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "resource_key": {"type": "string"},
                    "concern": {"type": "string"},
                    "severity": {"type": "string", "enum": ["info", "warning", "block"]},
                },
                "required": ["resource_key", "concern", "severity"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["safe_to_proceed", "flags"],
    "additionalProperties": False,
}


def _anthropic_call(
    model: str,
    system_prompt: str,
    user_content: str,
    *,
    max_tokens: int,
    output_schema: dict[str, Any] | None = None,
    client: anthropic.Anthropic | None = None,
) -> ModelCallResult:
    if client is None:
        client = anthropic.Anthropic()

    kwargs: dict[str, Any] = {
        "model": model,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_content}],
        "max_tokens": max_tokens,
    }
    if output_schema is not None:
        kwargs["output_config"] = {"format": {"type": "json_schema", "schema": output_schema}}

    start = time.monotonic()
    response = client.messages.create(**kwargs)
    duration_ms = log.elapsed_ms(start)

    text = None
    for block in response.content:
        if getattr(block, "type", None) == "text":
            text = block.text
            break
    if text is None:
        raise RuntimeError("model response contained no text content block")

    usage = response.usage
    details = getattr(usage, "output_tokens_details", None)
    thinking_tokens = getattr(details, "thinking_tokens", None) if details is not None else None

    return ModelCallResult(
        text=text,
        stop_reason=response.stop_reason,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        thinking_tokens=thinking_tokens,
        duration_ms=duration_ms,
    )


MODEL_SOURCES: dict[ModelSource, Callable[..., ModelCallResult]] = {
    ModelSource.ANTHROPIC: _anthropic_call,
}


def _log_call(role_name: str, model: str, result: ModelCallResult) -> None:
    logger.info(
        "",
        extra={
            "role": role_name,
            "model": model,
            "stop_reason": result.stop_reason,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "thinking_tokens": result.thinking_tokens,
            "duration_ms": result.duration_ms,
        },
    )
    if result.stop_reason == "max_tokens":
        logger.warning(
            "response likely truncated -- max_tokens reached before JSON completed",
            extra={"role": role_name, "model": model, "output_tokens": result.output_tokens},
        )


def _resolve_role(
    llm_config: LLMConfig | None, role_name: str
) -> tuple[LLMRoleConfig, Callable[..., ModelCallResult]]:
    if llm_config is None:
        llm_config = config.resolve_llm_config()
    role = getattr(llm_config, role_name)
    return role, MODEL_SOURCES[role.source]


def _implementation_tier_call(
    role_name: str,
    system_prompt: str,
    user_content: str,
    *,
    output_schema: dict[str, Any] | None = None,
    max_tokens: int | None = None,
    client: anthropic.Anthropic | None = None,
    llm_config: LLMConfig | None = None,
) -> str:
    role, call = _resolve_role(llm_config, role_name)
    result = call(
        role.model,
        system_prompt,
        user_content,
        output_schema=output_schema,
        max_tokens=max_tokens if max_tokens is not None else role.max_tokens,
        client=client,
    )
    _log_call(role_name, role.model, result)
    return result.text


def intent_orchestration_call(
    system_prompt: str,
    user_content: str,
    *,
    output_schema: dict[str, Any] | None = None,
    max_tokens: int | None = None,
    client: anthropic.Anthropic | None = None,
    llm_config: LLMConfig | None = None,
) -> str:
    return _implementation_tier_call(
        "intent_orchestration",
        system_prompt,
        user_content,
        output_schema=output_schema,
        max_tokens=max_tokens,
        client=client,
        llm_config=llm_config,
    )


def code_generator_call(
    system_prompt: str,
    user_content: str,
    *,
    output_schema: dict[str, Any] | None = None,
    max_tokens: int | None = None,
    client: anthropic.Anthropic | None = None,
    llm_config: LLMConfig | None = None,
) -> str:
    return _implementation_tier_call(
        "code_generator",
        system_prompt,
        user_content,
        output_schema=output_schema,
        max_tokens=max_tokens,
        client=client,
        llm_config=llm_config,
    )


def review_driver(
    driver_source: str,
    *,
    client: anthropic.Anthropic | None = None,
    llm_config: LLMConfig | None = None,
) -> DriverReview:
    role, call = _resolve_role(llm_config, "code_review")
    system_prompt = load_prompt("review_driver.md")
    result = call(
        role.model,
        system_prompt,
        driver_source,
        output_schema=DRIVER_REVIEW_SCHEMA,
        max_tokens=role.max_tokens,
        client=client,
    )
    _log_call("code_review", role.model, result)
    data = json.loads(result.text)
    review = DriverReview(
        approved=data["approved"],
        concerns=data["concerns"],
        blocking_issues=data["blocking_issues"],
        reviewed_at=datetime.now(UTC),
        model=role.model,
    )
    logger.info(
        "",
        extra={
            "approved": review.approved,
            "concerns_count": len(review.concerns),
            "blocking_issues_count": len(review.blocking_issues),
        },
    )
    return review


def review_plan(
    plan_summary: str,
    *,
    client: anthropic.Anthropic | None = None,
    llm_config: LLMConfig | None = None,
) -> PlanReview:
    role, call = _resolve_role(llm_config, "review_orchestration")
    system_prompt = load_prompt("review_plan.md")
    result = call(
        role.model,
        system_prompt,
        plan_summary,
        output_schema=PLAN_REVIEW_SCHEMA,
        max_tokens=role.max_tokens,
        client=client,
    )
    _log_call("review_orchestration", role.model, result)
    review = PlanReview.model_validate_json(result.text)
    logger.info(
        "",
        extra={"safe_to_proceed": review.safe_to_proceed, "flags_count": len(review.flags)},
    )
    return review
