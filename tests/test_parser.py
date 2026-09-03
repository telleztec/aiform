# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

import json
import types
from pathlib import Path

import pytest
from pydantic import ValidationError

from aiform import llm, parser
from aiform.models import ParsedResource, ResourceSpec

VALID_FRONTMATTER = """\
---
resource: compute
name: telleztec-app-01
provider: digitalocean
params:
  region: sfo3
  size: s-1vcpu-2gb
  image: ubuntu-24-04-x64
  ssh_keys:
    - "juan-macbook-ed25519"
  backups: false
  monitoring: true
  tags:
    - aiform
    - production
---
"""

INTENT_PROSE = (
    "This droplet runs the primary application server. It should always have\n"
    "monitoring enabled. If I change the `size` to something bigger, prefer an\n"
    "in-place resize over destroying and recreating."
)

FULL_EXAMPLE = f"""{VALID_FRONTMATTER}
## Intent

{INTENT_PROSE}
"""


class FakeTextBlock:
    def __init__(self, text: str):
        self.type = "text"
        self.text = text


class FakeResponse:
    def __init__(self, text: str):
        self.content = [FakeTextBlock(text)]
        self.stop_reason = "end_turn"
        self.usage = types.SimpleNamespace(input_tokens=0, output_tokens=0)


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
    (directory / "parse_intent.md").write_text("Extract intent notes from the prose.\n")
    monkeypatch.setattr(llm, "PROMPTS_DIR", directory)
    return directory


def intent_notes_response(notes: list[dict[str, str]]) -> str:
    return json.dumps({"intent_notes": notes})


class TestIntentNotesSchema:
    def test_matches_plan_md_shape(self):
        schema = parser.INTENT_NOTES_SCHEMA
        assert schema["required"] == ["intent_notes"]
        item_schema = schema["properties"]["intent_notes"]["items"]
        assert set(item_schema["required"]) == {"concerns_field", "guidance"}
        assert item_schema["additionalProperties"] is False


class TestComputeSha256:
    def test_deterministic(self):
        assert parser.compute_sha256("hello") == parser.compute_sha256("hello")

    def test_differs_on_different_content(self):
        assert parser.compute_sha256("hello") != parser.compute_sha256("hello!")

    def test_matches_hashlib_sha256_of_utf8_bytes(self):
        import hashlib

        content = "some aiform.md content\n"
        assert parser.compute_sha256(content) == hashlib.sha256(content.encode("utf-8")).hexdigest()


class TestParseFrontmatter:
    def test_parses_valid_frontmatter(self):
        spec = parser.parse_frontmatter(VALID_FRONTMATTER)

        assert isinstance(spec, ResourceSpec)
        assert spec.resource == "compute"
        assert spec.name == "telleztec-app-01"
        assert spec.provider == "digitalocean"
        assert spec.params["region"] == "sfo3"
        assert spec.params["ssh_keys"] == ["juan-macbook-ed25519"]
        assert spec.params["tags"] == ["aiform", "production"]

    def test_parses_frontmatter_followed_by_prose_body(self):
        spec = parser.parse_frontmatter(FULL_EXAMPLE)
        assert spec.name == "telleztec-app-01"

    def test_missing_opening_delimiter_raises_value_error(self):
        content = "resource: compute\nname: x\nprovider: digitalocean\nparams: {}\n---\n"
        with pytest.raises(ValueError):
            parser.parse_frontmatter(content)

    def test_missing_closing_delimiter_raises_value_error(self):
        content = "---\nresource: compute\nname: x\nprovider: digitalocean\nparams: {}\n"
        with pytest.raises(ValueError):
            parser.parse_frontmatter(content)

    def test_malformed_yaml_raises_value_error(self):
        content = "---\nresource: [this is not: a valid mapping\n---\n"
        with pytest.raises(ValueError):
            parser.parse_frontmatter(content)

    def test_empty_frontmatter_block_raises_value_error(self):
        content = "---\n---\n"
        with pytest.raises(ValueError):
            parser.parse_frontmatter(content)

    def test_non_mapping_frontmatter_raises_value_error(self):
        content = "---\n- just\n- a\n- list\n---\n"
        with pytest.raises(ValueError):
            parser.parse_frontmatter(content)

    def test_missing_required_field_raises_validation_error(self):
        content = "---\nresource: compute\nname: x\nparams: {}\n---\n"
        with pytest.raises(ValidationError):
            parser.parse_frontmatter(content)

    def test_unexpected_field_raises_validation_error(self):
        content = (
            "---\nresource: compute\nname: x\nprovider: digitalocean\n"
            "params: {}\nunexpected: oops\n---\n"
        )
        with pytest.raises(ValidationError):
            parser.parse_frontmatter(content)

    def test_uppercase_provider_raises_validation_error(self):
        content = "---\nresource: compute\nname: x\nprovider: DigitalOcean\nparams: {}\n---\n"
        with pytest.raises(ValidationError):
            parser.parse_frontmatter(content)

    def test_dashes_line_inside_block_scalar_does_not_truncate_frontmatter(self):
        # A '---' line indented inside a YAML block scalar (e.g. cloud-init
        # user_data) is content, not a document boundary -- the naive
        # "any line that strips to '---'" scan used to mistake it for the
        # closing delimiter and silently drop everything after it.
        content = (
            "---\n"
            "resource: compute\n"
            "name: x\n"
            "provider: digitalocean\n"
            "params:\n"
            "  user_data: |\n"
            "    #!/bin/bash\n"
            "    ---\n"
            "    echo hi\n"
            "  region: sfo3\n"
            "---\n"
        )
        spec = parser.parse_frontmatter(content)
        assert spec.params["user_data"] == "#!/bin/bash\n---\necho hi\n"
        assert spec.params["region"] == "sfo3"

    def test_bareword_boolean_key_raises_value_error_not_type_error(self):
        # PyYAML's "Norway problem": an unquoted yes/no/on/off/true/false
        # key resolves to a Python bool, and dict(**data) with a non-str
        # key raises a bare TypeError -- not the documented ValueError.
        content = (
            "---\nresource: compute\nname: x\nprovider: digitalocean\nparams: {}\non: true\n---\n"
        )
        with pytest.raises(ValueError):
            parser.parse_frontmatter(content)


class TestExtractIntentProse:
    def test_raises_value_error_when_no_frontmatter_delimiters(self):
        # extract_intent_prose() assumes well-formed frontmatter, same as
        # parse_frontmatter() -- in the real parse_file() pipeline,
        # parse_frontmatter() always runs first and would already have
        # raised, so this is only reachable via a direct call.
        with pytest.raises(ValueError):
            parser.extract_intent_prose("no frontmatter here at all\n")

    def test_hash_prefixed_line_inside_fenced_code_block_does_not_truncate(self):
        content = (
            f"{VALID_FRONTMATTER}\n"
            "## Intent\n\n"
            "before fence\n\n"
            "```\n"
            "## not a real heading\n"
            "```\n\n"
            "after fence\n"
        )
        prose = parser.extract_intent_prose(content)
        assert prose == "before fence\n\n```\n## not a real heading\n```\n\nafter fence"

    def test_extracts_prose_under_intent_heading(self):
        prose = parser.extract_intent_prose(FULL_EXAMPLE)
        assert prose == INTENT_PROSE

    def test_returns_empty_string_when_no_intent_heading(self):
        content = VALID_FRONTMATTER + "\nSome other prose with no heading at all.\n"
        assert parser.extract_intent_prose(content) == ""

    def test_stops_at_next_level_two_heading(self):
        content = f"{VALID_FRONTMATTER}\n## Intent\n\nfirst part\n\n## Notes\n\nnot included\n"
        assert parser.extract_intent_prose(content) == "first part"

    def test_only_first_intent_heading_is_used(self):
        content = f"{VALID_FRONTMATTER}\n## Intent\n\nfirst\n\n## Intent\n\nsecond, never reached\n"
        assert parser.extract_intent_prose(content) == "first"

    def test_case_sensitive_heading_match(self):
        content = VALID_FRONTMATTER + "\n## intent\n\nlowercase heading, should not match\n"
        assert parser.extract_intent_prose(content) == ""

    def test_wrong_heading_level_does_not_match(self):
        content = VALID_FRONTMATTER + "\n### Intent\n\nh3, should not match\n"
        assert parser.extract_intent_prose(content) == ""

    def test_strips_surrounding_blank_lines(self):
        content = f"{VALID_FRONTMATTER}\n## Intent\n\n\nsome guidance\n\n\n"
        assert parser.extract_intent_prose(content) == "some guidance"


class TestExtractIntentNotes:
    def test_empty_prose_returns_empty_list_with_zero_llm_calls(
        self, prompts_dir: Path, forbid_llm_client
    ):
        # Guarded by the forbid_llm_client fixture. The previous comment
        # here claimed a keyless anthropic.Anthropic() would blow up; it
        # does not, so this asserted nothing. See tests/conftest.py.
        assert parser.extract_intent_notes("") == []

    def test_whitespace_only_prose_returns_empty_list_with_zero_llm_calls(
        self, prompts_dir: Path, forbid_llm_client
    ):
        assert parser.extract_intent_notes("   \n\n  \t\n") == []

    def test_calls_llm_for_nonempty_prose(self, prompts_dir: Path):
        notes = [{"concerns_field": "size", "guidance": "prefer in-place resize"}]
        client = FakeClient([intent_notes_response(notes)])

        result = parser.extract_intent_notes(INTENT_PROSE, client=client)

        assert result == notes
        assert len(client.messages.calls) == 1

    def test_uses_parse_intent_prompt_as_system(self, prompts_dir: Path):
        client = FakeClient([intent_notes_response([])])

        parser.extract_intent_notes(INTENT_PROSE, client=client)

        call = client.messages.calls[0]
        assert call["system"] == (prompts_dir / "parse_intent.md").read_text()

    def test_uses_intent_notes_schema(self, prompts_dir: Path):
        client = FakeClient([intent_notes_response([])])

        parser.extract_intent_notes(INTENT_PROSE, client=client)

        call = client.messages.calls[0]
        assert call["output_config"]["format"]["schema"] == parser.INTENT_NOTES_SCHEMA

    def test_user_content_is_raw_prose_not_json_wrapped(self, prompts_dir: Path):
        client = FakeClient([intent_notes_response([])])

        parser.extract_intent_notes(INTENT_PROSE, client=client)

        call = client.messages.calls[0]
        assert call["messages"][0]["content"] == INTENT_PROSE

    def test_returns_empty_list_when_model_reports_no_notes(self, prompts_dir: Path):
        client = FakeClient([intent_notes_response([])])
        assert parser.extract_intent_notes(INTENT_PROSE, client=client) == []

    def test_empty_prose_logs_zero_calls_signal(self, prompts_dir: Path, caplog):
        caplog.set_level("INFO", logger="aiform.parser")

        parser.extract_intent_notes("")

        record = caplog.records[0]
        assert record.intent_prose_empty is True
        assert record.notes_count == 0

    def test_nonempty_prose_logs_notes_count(self, prompts_dir: Path, caplog):
        caplog.set_level("INFO", logger="aiform.parser")
        notes = [
            {"concerns_field": "size", "guidance": "a"},
            {"concerns_field": "region", "guidance": "b"},
        ]
        client = FakeClient([intent_notes_response(notes)])

        parser.extract_intent_notes(INTENT_PROSE, client=client)

        record = caplog.records[0]
        assert record.notes_count == 2
        assert not hasattr(record, "intent_prose_empty")


class TestParseFile:
    def test_parses_full_example_and_extracts_intent_notes(self, tmp_path: Path, prompts_dir: Path):
        path = tmp_path / "compute.aiform.md"
        path.write_text(FULL_EXAMPLE)
        notes = [{"concerns_field": "size", "guidance": "prefer in-place resize"}]
        client = FakeClient([intent_notes_response(notes)])

        parsed = parser.parse_file(path, client=client)

        assert isinstance(parsed, ParsedResource)
        assert parsed.spec.name == "telleztec-app-01"
        assert parsed.intent_notes == notes
        assert parsed.aiform_md_sha256 == parser.compute_sha256(FULL_EXAMPLE)
        assert len(client.messages.calls) == 1

    def test_skips_intent_extraction_when_hash_matches_previous(
        self, tmp_path: Path, prompts_dir: Path
    ):
        path = tmp_path / "compute.aiform.md"
        path.write_text(FULL_EXAMPLE)
        previous_hash = parser.compute_sha256(FULL_EXAMPLE)

        # No client passed -- if the hash-match short circuit ever fell
        # through to extract_intent_notes()'s LLM call, this would blow up
        # rather than silently succeeding.
        parsed = parser.parse_file(path, previous_aiform_md_sha256=previous_hash)

        assert parsed.intent_notes == []
        assert parsed.aiform_md_sha256 == previous_hash

    def test_extracts_intent_notes_when_hash_does_not_match(
        self, tmp_path: Path, prompts_dir: Path
    ):
        path = tmp_path / "compute.aiform.md"
        path.write_text(FULL_EXAMPLE)
        notes = [{"concerns_field": "general", "guidance": "always keep monitoring on"}]
        client = FakeClient([intent_notes_response(notes)])

        parsed = parser.parse_file(
            path, previous_aiform_md_sha256="stale-hash-from-a-prior-version", client=client
        )

        assert parsed.intent_notes == notes
        assert len(client.messages.calls) == 1

    def test_no_prior_hash_still_categorizes_when_prose_present(
        self, tmp_path: Path, prompts_dir: Path
    ):
        path = tmp_path / "compute.aiform.md"
        path.write_text(FULL_EXAMPLE)
        client = FakeClient([intent_notes_response([])])

        parsed = parser.parse_file(path, previous_aiform_md_sha256=None, client=client)

        assert len(client.messages.calls) == 1
        assert parsed.intent_notes == []

    def test_no_prior_hash_and_no_intent_section_makes_zero_llm_calls(
        self, tmp_path: Path, prompts_dir: Path
    ):
        path = tmp_path / "compute.aiform.md"
        path.write_text(VALID_FRONTMATTER)

        # No client passed -- stacked short circuit (judgment call 4): even
        # on a brand-new resource's first parse, empty prose alone is
        # enough to skip the LLM call.
        parsed = parser.parse_file(path, previous_aiform_md_sha256=None)

        assert parsed.intent_notes == []

    def test_malformed_frontmatter_propagates_value_error(self, tmp_path: Path):
        path = tmp_path / "broken.aiform.md"
        path.write_text("not frontmatter at all\n")

        with pytest.raises(ValueError):
            parser.parse_file(path)

    def test_invalid_resource_spec_propagates_validation_error(self, tmp_path: Path):
        path = tmp_path / "invalid.aiform.md"
        path.write_text("---\nresource: compute\nname: x\nparams: {}\n---\n")

        with pytest.raises(ValidationError):
            parser.parse_file(path)

    def test_missing_file_raises_file_not_found_error(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            parser.parse_file(tmp_path / "does-not-exist.aiform.md")

    def test_strips_leading_utf8_bom(self, tmp_path: Path, prompts_dir: Path):
        path = tmp_path / "compute.aiform.md"
        path.write_bytes(b"\xef\xbb\xbf" + VALID_FRONTMATTER.encode("utf-8"))

        parsed = parser.parse_file(path)

        assert parsed.spec.name == "telleztec-app-01"
        # The hash must be of the BOM-stripped decoded text, not the raw
        # on-disk bytes -- parse_file()'s no-op short circuit and
        # orchestrator.py's zero-API-call guarantee both depend on this
        # hash being stable and reproducible from compute_sha256(content).
        assert parsed.aiform_md_sha256 == parser.compute_sha256(VALID_FRONTMATTER)

    def test_frontmatter_parse_error_prevents_any_llm_call(self, tmp_path: Path, prompts_dir: Path):
        path = tmp_path / "broken.aiform.md"
        path.write_text("---\nresource: [broken yaml\n---\n\n## Intent\n\nsome prose\n")

        with pytest.raises(ValueError):
            parser.parse_file(path)


class TestRealPromptFile:
    def test_parse_intent_prompt_exists_and_is_nonempty(self):
        path = llm.PROMPTS_DIR / "parse_intent.md"
        assert path.exists()
        assert len(path.read_text(encoding="utf-8").strip()) > 0
