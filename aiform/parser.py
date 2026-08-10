import hashlib
import json
from pathlib import Path
from typing import Any

import anthropic
import yaml

from aiform import llm
from aiform.models import LLMConfig, ParsedResource, ResourceSpec

INTENT_NOTES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "intent_notes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "concerns_field": {
                        "type": "string",
                        "description": "params.* key this note applies to, or 'general'",
                    },
                    "guidance": {
                        "type": "string",
                        "description": (
                            "One atomic, diff/plan-relevant instruction extracted from the prose."
                        ),
                    },
                },
                "required": ["concerns_field", "guidance"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["intent_notes"],
    "additionalProperties": False,
}


def compute_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _find_delimiter_lines(lines: list[str]) -> tuple[int, int] | None:
    if not lines or lines[0].strip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return 0, i
    return None


def parse_frontmatter(content: str) -> ResourceSpec:
    lines = content.splitlines()
    delimiters = _find_delimiter_lines(lines)
    if delimiters is None:
        raise ValueError(
            "malformed .aiform.md: expected a frontmatter block delimited by two '---' lines"
        )
    _, closing_index = delimiters
    frontmatter_text = "\n".join(lines[1:closing_index])

    try:
        data = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError as e:
        raise ValueError(f"malformed .aiform.md frontmatter: invalid YAML: {e}") from e

    if not isinstance(data, dict):
        raise ValueError(
            f"malformed .aiform.md frontmatter: expected a YAML mapping, got {type(data).__name__}"
        )

    return ResourceSpec(**data)


def extract_intent_prose(content: str) -> str:
    lines = content.splitlines()
    delimiters = _find_delimiter_lines(lines)
    body_lines = lines[delimiters[1] + 1 :] if delimiters is not None else lines

    heading_index = None
    for i, line in enumerate(body_lines):
        if line.strip() == "## Intent":
            heading_index = i
            break
    if heading_index is None:
        return ""

    prose_lines = []
    for line in body_lines[heading_index + 1 :]:
        if line.strip().startswith("##"):
            break
        prose_lines.append(line)

    while prose_lines and not prose_lines[0].strip():
        prose_lines.pop(0)
    while prose_lines and not prose_lines[-1].strip():
        prose_lines.pop()

    return "\n".join(prose_lines)


def extract_intent_notes(
    prose_intent_text: str,
    *,
    client: anthropic.Anthropic | None = None,
    llm_config: LLMConfig | None = None,
) -> list[dict[str, str]]:
    if not prose_intent_text.strip():
        return []

    system_prompt = llm.load_prompt("parse_intent.md")
    response_text = llm.implementation_call(
        system_prompt,
        prose_intent_text,
        output_schema=INTENT_NOTES_SCHEMA,
        client=client,
        llm_config=llm_config,
    )
    return json.loads(response_text)["intent_notes"]


def parse_file(
    path: Path,
    *,
    previous_aiform_md_sha256: str | None = None,
    client: anthropic.Anthropic | None = None,
    llm_config: LLMConfig | None = None,
) -> ParsedResource:
    content = path.read_text(encoding="utf-8-sig")
    aiform_md_sha256 = compute_sha256(content)
    spec = parse_frontmatter(content)

    if aiform_md_sha256 == previous_aiform_md_sha256:
        intent_notes: list[dict[str, str]] = []
    else:
        intent_notes = extract_intent_notes(
            extract_intent_prose(content), client=client, llm_config=llm_config
        )

    return ParsedResource(spec=spec, intent_notes=intent_notes, aiform_md_sha256=aiform_md_sha256)
