import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anthropic

from aiform import config
from aiform.models import DriverReview, LLMConfig, LLMRoleConfig, ModelSource, PlanReview

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


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
    output_schema: dict[str, Any] | None = None,
    max_tokens: int = 4096,
    client: anthropic.Anthropic | None = None,
) -> str:
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

    response = client.messages.create(**kwargs)
    for block in response.content:
        if getattr(block, "type", None) == "text":
            return block.text
    raise RuntimeError("model response contained no text content block")


MODEL_SOURCES: dict[ModelSource, Callable[..., str]] = {
    ModelSource.ANTHROPIC: _anthropic_call,
}


def _resolve_role(
    llm_config: LLMConfig | None, role_name: str
) -> tuple[LLMRoleConfig, Callable[..., str]]:
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
    return call(
        role.model,
        system_prompt,
        user_content,
        output_schema=output_schema,
        max_tokens=max_tokens if max_tokens is not None else role.max_tokens,
        client=client,
    )


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
    response_text = call(
        role.model,
        system_prompt,
        driver_source,
        output_schema=DRIVER_REVIEW_SCHEMA,
        max_tokens=role.max_tokens,
        client=client,
    )
    data = json.loads(response_text)
    return DriverReview(
        approved=data["approved"],
        concerns=data["concerns"],
        blocking_issues=data["blocking_issues"],
        reviewed_at=datetime.now(UTC),
        model=role.model,
    )


def review_plan(
    plan_summary: str,
    *,
    client: anthropic.Anthropic | None = None,
    llm_config: LLMConfig | None = None,
) -> PlanReview:
    role, call = _resolve_role(llm_config, "review_orchestration")
    system_prompt = load_prompt("review_plan.md")
    response_text = call(
        role.model,
        system_prompt,
        plan_summary,
        output_schema=PLAN_REVIEW_SCHEMA,
        max_tokens=role.max_tokens,
        client=client,
    )
    return PlanReview.model_validate_json(response_text)
