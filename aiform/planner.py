import json
from typing import Any

import anthropic

from aiform import llm
from aiform.models import LLMConfig, PlanAction, PlanEntry

# "destroy" is deliberately excluded from this enum, unlike PLAN.md §5
# step 6's literal 4-value PlanAction list: a current-vs-desired diff has
# no structural basis to conclude a resource should stop existing (see
# specs/planner.md's judgment call 2). Every destroy PlanEntry comes from
# destroy_entry() below instead -- explicit, zero-LLM, for an `aiform
# destroy` argument or an AIFORM-DELETE-<name>.aiform.md file (PLAN.md's
# "Resource deletion"), never from categorizing a diff. Narrowing the
# schema means the model cannot select "destroy" here even if it
# disregards prompts/diff_plan.md's instruction not to -- a stronger
# guarantee than a prompt alone.
PLAN_CATEGORIZATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["create", "update", "no-op"]},
        "rationale": {"type": "string"},
        "likely_replace": {"type": "boolean"},
    },
    "required": ["action", "rationale", "likely_replace"],
    "additionalProperties": False,
}


def destroy_entry(resource_key: str, rationale: str) -> PlanEntry:
    return PlanEntry(
        resource_key=resource_key,
        action=PlanAction.DESTROY,
        rationale=rationale,
        likely_replace=False,
    )


def diff_attributes(current: dict[str, Any], desired: dict[str, Any]) -> dict[str, dict[str, Any]]:
    diff: dict[str, dict[str, Any]] = {}
    for key, desired_value in desired.items():
        current_value = current.get(key)
        if current_value != desired_value:
            diff[key] = {"current": current_value, "desired": desired_value}
    return diff


def categorize_diff(
    resource_key: str,
    diff: dict[str, Any],
    *,
    intent_notes: list[dict[str, str]],
    param_schema: dict[str, Any],
    likely_replace_fields: list[str],
    drifted_missing: bool = False,
    client: anthropic.Anthropic | None = None,
    llm_config: LLMConfig | None = None,
) -> PlanEntry:
    system_prompt = llm.load_prompt("diff_plan.md")
    user_content = json.dumps(
        {
            "diff": diff,
            "intent_notes": intent_notes,
            "param_schema": param_schema,
            "likely_replace_fields": likely_replace_fields,
            "drifted_missing": drifted_missing,
        }
    )
    response_text = llm.intent_orchestration_call(
        system_prompt,
        user_content,
        output_schema=PLAN_CATEGORIZATION_SCHEMA,
        client=client,
        llm_config=llm_config,
    )
    data = json.loads(response_text)
    return PlanEntry(
        resource_key=resource_key,
        action=PlanAction(data["action"]),
        rationale=data["rationale"],
        likely_replace=data["likely_replace"],
    )


def plan_resource(
    resource_key: str,
    current_attributes: dict[str, Any],
    desired_params: dict[str, Any],
    *,
    intent_notes: list[dict[str, str]],
    param_schema: dict[str, Any],
    likely_replace_fields: list[str],
    state_aiform_md_sha256: str | None,
    current_aiform_md_sha256: str,
    drifted_missing: bool = False,
    client: anthropic.Anthropic | None = None,
    llm_config: LLMConfig | None = None,
) -> PlanEntry:
    diff = diff_attributes(current_attributes, desired_params)

    if not diff and state_aiform_md_sha256 == current_aiform_md_sha256 and not drifted_missing:
        return PlanEntry(
            resource_key=resource_key,
            action=PlanAction.NO_OP,
            rationale=(
                "no changes detected: live attributes match desired params and the "
                "aiform.md source is unchanged"
            ),
            likely_replace=False,
        )

    return categorize_diff(
        resource_key,
        diff,
        intent_notes=intent_notes,
        param_schema=param_schema,
        likely_replace_fields=likely_replace_fields,
        drifted_missing=drifted_missing,
        client=client,
        llm_config=llm_config,
    )
