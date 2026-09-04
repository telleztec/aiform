# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

import json
import types
from pathlib import Path

import pytest

from aiform import llm, planner
from aiform.models import PlanAction, PlanEntry


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
    (directory / "diff_plan.md").write_text("Categorize the diff into a plan action.\n")
    monkeypatch.setattr(llm, "PROMPTS_DIR", directory)
    return directory


def categorization_response(
    action: str = "update", rationale: str = "size changed", likely_replace: bool = False
) -> str:
    return json.dumps({"action": action, "rationale": rationale, "likely_replace": likely_replace})


RESOURCE_KEY = "digitalocean.compute.telleztec-app-01"


class TestCreateEntry:
    def test_returns_create_plan_entry(self):
        entry = planner.create_entry(
            RESOURCE_KEY, "no state entry is tracked for this resource yet"
        )

        assert isinstance(entry, PlanEntry)
        assert entry.resource_key == RESOURCE_KEY
        assert entry.action == PlanAction.CREATE
        assert entry.rationale == "no state entry is tracked for this resource yet"
        assert entry.likely_replace is False

    def test_preserves_caller_supplied_rationale_verbatim(self):
        # The caller distinguishes untracked from drifted-missing; this
        # module only records which it said.
        rationale = "tracked resource no longer exists on the provider side"
        assert planner.create_entry(RESOURCE_KEY, rationale).rationale == rationale

    def test_likely_replace_is_always_false(self):
        # A resource that does not exist cannot be replaced, only made.
        for rationale in ("untracked", "drifted missing", ""):
            assert planner.create_entry(RESOURCE_KEY, rationale).likely_replace is False

    def test_makes_no_llm_call(self, prompts_dir: Path):
        # No client passed at all -- if create_entry() ever fell through
        # to an LLM call it would blow up constructing a real
        # anthropic.Anthropic() rather than silently succeeding. Same
        # guard as destroy_entry()'s and plan_resource()'s no-op test.
        assert planner.create_entry(RESOURCE_KEY, "untracked").action == PlanAction.CREATE


class TestDestroyEntry:
    def test_returns_destroy_plan_entry(self):
        entry = planner.destroy_entry(RESOURCE_KEY, "explicitly requested via aiform destroy")

        assert isinstance(entry, PlanEntry)
        assert entry.resource_key == RESOURCE_KEY
        assert entry.action == PlanAction.DESTROY
        assert entry.rationale == "explicitly requested via aiform destroy"
        assert entry.likely_replace is False

    def test_preserves_caller_supplied_rationale_verbatim(self):
        rationale = "marked via AIFORM-DELETE-telleztec-app-01.aiform.md"
        entry = planner.destroy_entry(RESOURCE_KEY, rationale)
        assert entry.rationale == rationale

    def test_makes_no_llm_call(self, prompts_dir: Path):
        # No client is passed at all -- if destroy_entry() ever fell
        # through to an LLM call, this would blow up trying to construct
        # a real anthropic.Anthropic() client rather than silently
        # succeeding, same guard as plan_resource()'s no-op test.
        entry = planner.destroy_entry(RESOURCE_KEY, "explicit destroy")
        assert entry.action == PlanAction.DESTROY


class TestDiffAttributes:
    def test_empty_when_all_desired_keys_match_current(self):
        current = {"region": "sfo3", "size": "s-1vcpu-2gb"}
        desired = {"region": "sfo3", "size": "s-1vcpu-2gb"}
        assert planner.diff_attributes(current, desired) == {}

    def test_reports_changed_field(self):
        current = {"region": "sfo3", "size": "s-1vcpu-2gb"}
        desired = {"region": "sfo3", "size": "s-2vcpu-4gb"}
        assert planner.diff_attributes(current, desired) == {
            "size": {"current": "s-1vcpu-2gb", "desired": "s-2vcpu-4gb"}
        }

    def test_reports_multiple_changed_fields(self):
        current = {"region": "sfo3", "size": "s-1vcpu-2gb"}
        desired = {"region": "nyc3", "size": "s-2vcpu-4gb"}
        diff = planner.diff_attributes(current, desired)
        assert set(diff) == {"region", "size"}
        assert diff["region"] == {"current": "sfo3", "desired": "nyc3"}
        assert diff["size"] == {"current": "s-1vcpu-2gb", "desired": "s-2vcpu-4gb"}

    def test_missing_key_in_current_reports_none(self):
        current = {"region": "sfo3"}
        desired = {"region": "sfo3", "monitoring": True}
        assert planner.diff_attributes(current, desired) == {
            "monitoring": {"current": None, "desired": True}
        }

    def test_ignores_keys_present_only_in_current(self):
        current = {"region": "sfo3", "ipv4_address": "203.0.113.10", "status": "active"}
        desired = {"region": "sfo3"}
        assert planner.diff_attributes(current, desired) == {}

    def test_empty_desired_produces_empty_diff(self):
        current = {"region": "sfo3", "size": "s-1vcpu-2gb"}
        assert planner.diff_attributes(current, {}) == {}

    def test_list_and_dict_values_use_deep_equality(self):
        current = {"tags": ["aiform", "production"]}
        desired = {"tags": ["aiform", "production"]}
        assert planner.diff_attributes(current, desired) == {}

        desired_changed = {"tags": ["aiform"]}
        assert planner.diff_attributes(current, desired_changed) == {
            "tags": {"current": ["aiform", "production"], "desired": ["aiform"]}
        }


class TestDiffAttributesUnorderedFields:
    def test_declared_unordered_field_reordered_yields_empty_diff(self):
        current = {"tags": ["aiform", "production"]}
        desired = {"tags": ["production", "aiform"]}
        assert planner.diff_attributes(current, desired, unordered_fields=["tags"]) == {}

    def test_same_field_not_declared_still_yields_a_diff(self):
        current = {"tags": ["aiform", "production"]}
        desired = {"tags": ["production", "aiform"]}
        assert planner.diff_attributes(current, desired) == {
            "tags": {"current": ["aiform", "production"], "desired": ["production", "aiform"]}
        }

    def test_genuinely_changed_declared_field_is_reported_verbatim_and_unsorted(self):
        current = {"tags": ["zebra", "aiform"]}
        desired = {"tags": ["mail", "aiform", "web"]}
        diff = planner.diff_attributes(current, desired, unordered_fields=["tags"])
        assert diff == {
            "tags": {"current": ["zebra", "aiform"], "desired": ["mail", "aiform", "web"]}
        }
        # Never canonicalized or sorted -- the original order survives
        # exactly as passed in, on both sides.
        assert diff["tags"]["current"] == ["zebra", "aiform"]
        assert diff["tags"]["desired"] == ["mail", "aiform", "web"]

    def test_declared_field_absent_from_desired_is_not_compared_at_all(self):
        current = {"tags": ["aiform"], "region": "sfo3"}
        desired = {"region": "sfo3"}
        assert planner.diff_attributes(current, desired, unordered_fields=["tags"]) == {}

    def test_declared_field_absent_from_current_reports_a_diff(self):
        current = {"region": "sfo3"}
        desired = {"region": "sfo3", "tags": ["aiform"]}
        assert planner.diff_attributes(current, desired, unordered_fields=["tags"]) == {
            "tags": {"current": None, "desired": ["aiform"]}
        }

    def test_default_unordered_fields_is_empty_and_behaves_as_before(self):
        current = {"tags": ["aiform", "production"]}
        desired = {"tags": ["production", "aiform"]}
        assert planner.diff_attributes(current, desired) == planner.diff_attributes(
            current, desired, unordered_fields=()
        )
        assert planner.diff_attributes(current, desired) != {}


class TestCategorizeDiff:
    def test_returns_plan_entry_with_resource_key_filled_in(self, prompts_dir: Path):
        client = FakeClient([categorization_response(action="update")])

        entry = planner.categorize_diff(
            RESOURCE_KEY,
            {"size": {"current": "s-1vcpu-2gb", "desired": "s-2vcpu-4gb"}},
            intent_notes=[],
            param_schema={"type": "object", "properties": {}},
            likely_replace_fields=[],
            client=client,
        )

        assert isinstance(entry, PlanEntry)
        assert entry.resource_key == RESOURCE_KEY
        assert entry.action == PlanAction.UPDATE
        assert entry.rationale == "size changed"

    def test_logs_the_categorization_decision(self, prompts_dir: Path, caplog):
        caplog.set_level("INFO", logger="aiform.planner")
        client = FakeClient([categorization_response(action="update", likely_replace=True)])

        planner.categorize_diff(
            RESOURCE_KEY,
            {"region": {"current": "sfo3", "desired": "nyc3"}},
            intent_notes=[],
            param_schema={},
            likely_replace_fields=["region"],
            client=client,
        )

        record = next(r for r in caplog.records if hasattr(r, "action"))
        assert record.resource_key == RESOURCE_KEY
        assert record.action == "update"
        assert record.likely_replace is True

    def test_uses_diff_plan_prompt_as_system(self, prompts_dir: Path):
        client = FakeClient([categorization_response()])

        planner.categorize_diff(
            RESOURCE_KEY,
            {},
            intent_notes=[],
            param_schema={},
            likely_replace_fields=[],
            client=client,
        )

        call = client.messages.calls[0]
        assert call["system"] == (prompts_dir / "diff_plan.md").read_text()

    def test_uses_categorization_schema(self, prompts_dir: Path):
        client = FakeClient([categorization_response()])

        planner.categorize_diff(
            RESOURCE_KEY,
            {},
            intent_notes=[],
            param_schema={},
            likely_replace_fields=[],
            client=client,
        )

        call = client.messages.calls[0]
        assert call["output_config"]["format"]["schema"] == planner.PLAN_CATEGORIZATION_SCHEMA

    def test_user_content_includes_diff_intent_notes_schema_and_flags(self, prompts_dir: Path):
        client = FakeClient([categorization_response()])
        diff = {"size": {"current": "s-1vcpu-2gb", "desired": "s-2vcpu-4gb"}}
        intent_notes = [{"concerns_field": "size", "guidance": "prefer in-place resize"}]
        param_schema = {"type": "object", "properties": {"size": {"type": "string"}}}

        planner.categorize_diff(
            RESOURCE_KEY,
            diff,
            intent_notes=intent_notes,
            param_schema=param_schema,
            likely_replace_fields=["image"],
            drifted_missing=True,
            client=client,
        )

        content = json.loads(client.messages.calls[0]["messages"][0]["content"])
        assert content["diff"] == diff
        assert content["intent_notes"] == intent_notes
        assert content["param_schema"] == param_schema
        assert content["likely_replace_fields"] == ["image"]
        assert content["drifted_missing"] is True

    def test_defaults_drifted_missing_to_false(self, prompts_dir: Path):
        client = FakeClient([categorization_response()])

        planner.categorize_diff(
            RESOURCE_KEY,
            {},
            intent_notes=[],
            param_schema={},
            likely_replace_fields=[],
            client=client,
        )

        content = json.loads(client.messages.calls[0]["messages"][0]["content"])
        assert content["drifted_missing"] is False

    def test_likely_replace_true_on_update_is_preserved(self, prompts_dir: Path):
        client = FakeClient([categorization_response(action="update", likely_replace=True)])

        entry = planner.categorize_diff(
            RESOURCE_KEY,
            {},
            intent_notes=[],
            param_schema={},
            likely_replace_fields=[],
            client=client,
        )

        assert entry.likely_replace is True

    def test_likely_replace_normalized_false_for_create(self, prompts_dir: Path):
        client = FakeClient([categorization_response(action="create", likely_replace=True)])

        entry = planner.categorize_diff(
            RESOURCE_KEY,
            {},
            intent_notes=[],
            param_schema={},
            likely_replace_fields=[],
            client=client,
        )

        assert entry.action == PlanAction.CREATE
        assert entry.likely_replace is False

    def test_unrecognized_action_raises(self, prompts_dir: Path):
        client = FakeClient([categorization_response(action="not-a-real-action")])

        with pytest.raises(ValueError):
            planner.categorize_diff(
                RESOURCE_KEY,
                {},
                intent_notes=[],
                param_schema={},
                likely_replace_fields=[],
                client=client,
            )


class TestPlanResource:
    def test_no_op_when_diff_empty_hash_matches_and_not_drifted(self, prompts_dir: Path):
        client = FakeClient([])

        entry = planner.plan_resource(
            RESOURCE_KEY,
            {"region": "sfo3"},
            {"region": "sfo3"},
            intent_notes=[],
            param_schema={},
            likely_replace_fields=[],
            state_aiform_md_sha256="abc123",
            current_aiform_md_sha256="abc123",
            client=client,
        )

        assert entry.action == PlanAction.NO_OP
        assert entry.resource_key == RESOURCE_KEY
        assert len(client.messages.calls) == 0

    def test_no_op_path_logs_zero_diff_reason(self, prompts_dir: Path, caplog):
        caplog.set_level("INFO", logger="aiform.planner")
        client = FakeClient([])

        planner.plan_resource(
            RESOURCE_KEY,
            {"region": "sfo3"},
            {"region": "sfo3"},
            intent_notes=[],
            param_schema={},
            likely_replace_fields=[],
            state_aiform_md_sha256="abc123",
            current_aiform_md_sha256="abc123",
            client=client,
        )

        record = caplog.records[0]
        assert record.resource_key == RESOURCE_KEY
        assert record.action == "no-op"
        assert record.reason == "zero-diff"

    def test_no_op_path_makes_zero_llm_calls_even_with_no_client(
        self, prompts_dir: Path, forbid_llm_client
    ):
        # Guarded by the forbid_llm_client fixture rather than by the
        # absence of a client. The comment here previously claimed a
        # keyless anthropic.Anthropic() "would blow up" -- it does not,
        # and .envrc exports a real key into pytest, so on a regression
        # this made a real billed call and then passed anyway. See the
        # fixture's docstring in tests/conftest.py.
        entry = planner.plan_resource(
            RESOURCE_KEY,
            {"region": "sfo3"},
            {"region": "sfo3"},
            intent_notes=[],
            param_schema={},
            likely_replace_fields=[],
            state_aiform_md_sha256="abc123",
            current_aiform_md_sha256="abc123",
        )
        assert entry.action == PlanAction.NO_OP

    def test_categorizes_when_diff_is_nonempty(self, prompts_dir: Path):
        client = FakeClient([categorization_response(action="update")])

        entry = planner.plan_resource(
            RESOURCE_KEY,
            {"region": "sfo3", "size": "s-1vcpu-2gb"},
            {"region": "sfo3", "size": "s-2vcpu-4gb"},
            intent_notes=[],
            param_schema={},
            likely_replace_fields=[],
            state_aiform_md_sha256="abc123",
            current_aiform_md_sha256="abc123",
            client=client,
        )

        assert entry.action == PlanAction.UPDATE
        assert len(client.messages.calls) == 1

    def test_categorizes_when_hash_changed_despite_empty_diff(self, prompts_dir: Path):
        client = FakeClient([categorization_response(action="no-op")])

        planner.plan_resource(
            RESOURCE_KEY,
            {"region": "sfo3"},
            {"region": "sfo3"},
            intent_notes=[],
            param_schema={},
            likely_replace_fields=[],
            state_aiform_md_sha256="old-hash",
            current_aiform_md_sha256="new-hash",
            client=client,
        )

        assert len(client.messages.calls) == 1

    def test_categorizes_when_drifted_missing_despite_empty_diff(self, prompts_dir: Path):
        client = FakeClient([categorization_response(action="create")])

        entry = planner.plan_resource(
            RESOURCE_KEY,
            {"region": "sfo3"},
            {"region": "sfo3"},
            intent_notes=[],
            param_schema={},
            likely_replace_fields=[],
            state_aiform_md_sha256="abc123",
            current_aiform_md_sha256="abc123",
            drifted_missing=True,
            client=client,
        )

        assert len(client.messages.calls) == 1
        assert entry.action == PlanAction.CREATE

    def test_a_null_prior_hash_still_reaches_categorization(self, prompts_dir: Path):
        """A `state_aiform_md_sha256=None` argument does not itself
        short-circuit anything -- it just never matches, so the no-op
        branch is skipped and the call goes out.

        This is a statement about this function in isolation, NOT about
        how new resources are planned. `orchestrator.py` never calls
        plan_resource() for an untracked resource at all: it uses
        create_entry(), because whether a state entry exists is its own
        record rather than something to infer from a diff (issue #117).
        This test previously asserted the opposite as the sanctioned
        path, under a name that read as though a new resource ought to be
        categorized here -- which is how #117 would get reinstated by
        someone reading the planner tests for guidance.
        """
        client = FakeClient([categorization_response(action="update")])

        # The diff is EMPTY on purpose. With a non-empty one the no-op
        # branch fails on `not diff` and the hash is never compared, so
        # the test would pass identically even if None did match -- it
        # would not isolate the property its name claims. Empty diff plus
        # a mismatched hash makes the hash the only reason the call goes
        # out.
        params = {"region": "sfo3", "size": "s-1vcpu-2gb"}
        entry = planner.plan_resource(
            RESOURCE_KEY,
            dict(params),
            params,
            intent_notes=[],
            param_schema={},
            likely_replace_fields=[],
            state_aiform_md_sha256=None,
            current_aiform_md_sha256="new-file-hash",
            client=client,
        )

        assert entry.action == PlanAction.UPDATE
        assert len(client.messages.calls) == 1

    def test_no_op_rationale_is_deterministic_not_llm_authored(self, prompts_dir: Path):
        entry = planner.plan_resource(
            RESOURCE_KEY,
            {"region": "sfo3"},
            {"region": "sfo3"},
            intent_notes=[],
            param_schema={},
            likely_replace_fields=[],
            state_aiform_md_sha256="abc123",
            current_aiform_md_sha256="abc123",
        )
        assert entry.rationale
        assert entry.likely_replace is False

    def test_reordered_declared_unordered_field_with_unchanged_hash_is_no_op_zero_llm_calls(
        self, prompts_dir: Path, forbid_llm_client
    ):
        # The single most important test in this file: a CSP returning
        # the same tags in a different order must not permanently defeat
        # CLAUDE.md's "zero Anthropic API calls on an unchanged second
        # run" guarantee. This variant passes no client, asserting the
        # short-circuit fires before one is even needed; the
        # forbid_llm_client fixture is what makes that assertion real.
        entry = planner.plan_resource(
            RESOURCE_KEY,
            {"tags": ["aiform", "production"]},
            {"tags": ["production", "aiform"]},
            intent_notes=[],
            param_schema={},
            likely_replace_fields=[],
            unordered_fields=["tags"],
            state_aiform_md_sha256="abc123",
            current_aiform_md_sha256="abc123",
        )

        assert entry.action == PlanAction.NO_OP

    def test_reordered_declared_unordered_field_makes_zero_llm_calls_with_client_present(
        self, prompts_dir: Path
    ):
        client = FakeClient([])

        entry = planner.plan_resource(
            RESOURCE_KEY,
            {"tags": ["aiform", "production"]},
            {"tags": ["production", "aiform"]},
            intent_notes=[],
            param_schema={},
            likely_replace_fields=[],
            unordered_fields=["tags"],
            state_aiform_md_sha256="abc123",
            current_aiform_md_sha256="abc123",
            client=client,
        )

        assert entry.action == PlanAction.NO_OP
        assert len(client.messages.calls) == 0

    def test_reordered_field_not_declared_unordered_still_categorizes(self, prompts_dir: Path):
        client = FakeClient([categorization_response(action="update")])

        entry = planner.plan_resource(
            RESOURCE_KEY,
            {"tags": ["aiform", "production"]},
            {"tags": ["production", "aiform"]},
            intent_notes=[],
            param_schema={},
            likely_replace_fields=[],
            state_aiform_md_sha256="abc123",
            current_aiform_md_sha256="abc123",
            client=client,
        )

        assert entry.action == PlanAction.UPDATE
        assert len(client.messages.calls) == 1

    def test_default_unordered_fields_leaves_existing_callers_unaffected(self, prompts_dir: Path):
        client = FakeClient([])

        entry = planner.plan_resource(
            RESOURCE_KEY,
            {"region": "sfo3"},
            {"region": "sfo3"},
            intent_notes=[],
            param_schema={},
            likely_replace_fields=[],
            state_aiform_md_sha256="abc123",
            current_aiform_md_sha256="abc123",
            client=client,
        )

        assert entry.action == PlanAction.NO_OP
        assert len(client.messages.calls) == 0


class TestRealPromptFile:
    def test_diff_plan_prompt_exists_and_is_nonempty(self):
        path = llm.PROMPTS_DIR / "diff_plan.md"
        assert path.exists()
        assert len(path.read_text(encoding="utf-8").strip()) > 0
