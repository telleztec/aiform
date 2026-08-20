import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import anthropic
import yaml

from aiform import llm
from aiform.models import LLMConfig, ParsedResource, ResourceSpec

logger = logging.getLogger(__name__)

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


_MALFORMED_DELIMITERS_MESSAGE = (
    "malformed .aiform.md: expected a frontmatter block delimited by two '---' lines"
)


def _closing_delimiter_index(content: str, lines: list[str]) -> int:
    """Line index of the frontmatter's closing '---', found by asking
    PyYAML's own document composer where the first YAML document
    actually ends -- not by naively matching any line that strips to
    '---'. A '---' indented inside a block scalar (e.g. a cloud-init
    `user_data: |` value) is content, not a document boundary, and only
    a real YAML parse correctly tells the two apart."""
    if not lines or lines[0].strip() != "---":
        raise ValueError(_MALFORMED_DELIMITERS_MESSAGE)

    try:
        document = next(yaml.compose_all(content), None)
    except yaml.YAMLError as e:
        raise ValueError(f"malformed .aiform.md frontmatter: invalid YAML: {e}") from e

    closing_index = document.end_mark.line if document is not None else len(lines)
    if closing_index >= len(lines) or lines[closing_index].strip() != "---":
        raise ValueError(_MALFORMED_DELIMITERS_MESSAGE)
    return closing_index


def parse_frontmatter(content: str) -> ResourceSpec:
    lines = content.splitlines()
    closing_index = _closing_delimiter_index(content, lines)
    frontmatter_text = "\n".join(lines[1:closing_index])

    try:
        data = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError as e:
        raise ValueError(f"malformed .aiform.md frontmatter: invalid YAML: {e}") from e

    if not isinstance(data, dict):
        raise ValueError(
            f"malformed .aiform.md frontmatter: expected a YAML mapping, got {type(data).__name__}"
        )
    if not all(isinstance(key, str) for key in data):
        raise ValueError(
            "malformed .aiform.md frontmatter: all keys must be strings -- a bare "
            "yes/no/on/off/true/false key is resolved to a boolean by YAML; quote it"
        )

    return ResourceSpec(**data)


def extract_intent_prose(content: str) -> str:
    lines = content.splitlines()
    closing_index = _closing_delimiter_index(content, lines)
    body_lines = lines[closing_index + 1 :]

    heading_index = None
    for i, line in enumerate(body_lines):
        if line.strip() == "## Intent":
            heading_index = i
            break
    if heading_index is None:
        return ""

    prose_lines = []
    in_fence = False
    for line in body_lines[heading_index + 1 :]:
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
        elif not in_fence and stripped.startswith("##"):
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
        logger.info("", extra={"intent_prose_empty": True, "notes_count": 0})
        return []

    system_prompt = llm.load_prompt("parse_intent.md")
    response_text = llm.intent_orchestration_call(
        system_prompt,
        prose_intent_text,
        output_schema=INTENT_NOTES_SCHEMA,
        client=client,
        llm_config=llm_config,
    )
    notes = json.loads(response_text)["intent_notes"]
    logger.info("", extra={"notes_count": len(notes)})
    return notes


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
