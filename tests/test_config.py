from pathlib import Path

import pytest

from aiform.config import (
    DEFAULT_CREDENTIALS_PATH,
    DEFAULT_LLM_CONFIG,
    DEFAULT_LLM_CONFIG_PATH,
    PROVIDER_TOKEN_ENV_VARS,
    resolve_credentials,
    resolve_llm_config,
)
from aiform.models import LLMConfig, LLMRoleConfig, ModelSource


class TestDefaults:
    def test_default_credentials_path_constant(self):
        assert DEFAULT_CREDENTIALS_PATH == Path(".aiform/credentials.env")

    def test_provider_token_env_vars_mapping(self):
        assert PROVIDER_TOKEN_ENV_VARS == {"digitalocean": "DIGITALOCEAN_TOKEN"}


class TestResolveCredentials:
    def test_env_var_takes_priority_over_file(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "from-env")
        credentials_path = tmp_path / "credentials.env"
        credentials_path.write_text("DIGITALOCEAN_TOKEN=from-file\n")

        result = resolve_credentials("digitalocean", credentials_path)

        assert result == {"DIGITALOCEAN_TOKEN": "from-env"}

    def test_falls_back_to_credentials_file_when_env_unset(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
        credentials_path = tmp_path / "credentials.env"
        credentials_path.write_text("DIGITALOCEAN_TOKEN=dop_v1_xyz\n")

        result = resolve_credentials("digitalocean", credentials_path)

        assert result == {"DIGITALOCEAN_TOKEN": "dop_v1_xyz"}

    def test_empty_env_var_falls_through_to_file(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "")
        credentials_path = tmp_path / "credentials.env"
        credentials_path.write_text("DIGITALOCEAN_TOKEN=dop_v1_xyz\n")

        result = resolve_credentials("digitalocean", credentials_path)

        assert result == {"DIGITALOCEAN_TOKEN": "dop_v1_xyz"}

    def test_missing_file_is_fine_when_env_set(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "from-env")
        credentials_path = tmp_path / "does-not-exist.env"

        result = resolve_credentials("digitalocean", credentials_path)

        assert result == {"DIGITALOCEAN_TOKEN": "from-env"}

    def test_raises_when_neither_env_nor_file_present(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
        credentials_path = tmp_path / "does-not-exist.env"

        with pytest.raises(RuntimeError) as excinfo:
            resolve_credentials("digitalocean", credentials_path)

        message = str(excinfo.value)
        assert "DIGITALOCEAN_TOKEN" in message
        assert str(credentials_path) in message

    def test_empty_credentials_file_behaves_like_missing(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
        credentials_path = tmp_path / "credentials.env"
        credentials_path.write_text("")

        with pytest.raises(RuntimeError):
            resolve_credentials("digitalocean", credentials_path)

    def test_credentials_file_missing_the_key_raises(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
        credentials_path = tmp_path / "credentials.env"
        credentials_path.write_text("SOME_OTHER_KEY=irrelevant\n")

        with pytest.raises(RuntimeError):
            resolve_credentials("digitalocean", credentials_path)

    def test_unsupported_provider_raises_immediately(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "from-env")
        credentials_path = tmp_path / "credentials.env"

        with pytest.raises(RuntimeError):
            resolve_credentials("aws", credentials_path)


class TestDefaultLLMConfig:
    def test_default_llm_config_path_constant(self):
        assert DEFAULT_LLM_CONFIG_PATH == Path(".aiform/config.yaml")

    def test_default_llm_config_preserves_mvp_defaults(self):
        assert DEFAULT_LLM_CONFIG == LLMConfig(
            implementation=LLMRoleConfig(source=ModelSource.ANTHROPIC, model="claude-sonnet-5"),
            review=LLMRoleConfig(source=ModelSource.ANTHROPIC, model="claude-opus-5"),
        )


class TestResolveLLMConfig:
    def test_missing_file_returns_default(self, tmp_path: Path):
        config_path = tmp_path / "does-not-exist.yaml"

        assert resolve_llm_config(config_path) == DEFAULT_LLM_CONFIG

    def test_empty_file_returns_default(self, tmp_path: Path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text("")

        assert resolve_llm_config(config_path) == DEFAULT_LLM_CONFIG

    def test_file_with_no_llm_key_returns_default(self, tmp_path: Path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text("unrelated: true\n")

        assert resolve_llm_config(config_path) == DEFAULT_LLM_CONFIG

    def test_full_override(self, tmp_path: Path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "llm:\n"
            "  implementation:\n"
            "    source: anthropic\n"
            "    model: claude-sonnet-5-override\n"
            "  review:\n"
            "    source: anthropic\n"
            "    model: claude-opus-5-override\n"
        )

        config = resolve_llm_config(config_path)

        assert config.implementation.model == "claude-sonnet-5-override"
        assert config.review.model == "claude-opus-5-override"

    def test_partial_override_keeps_other_role_default(self, tmp_path: Path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "llm:\n  review:\n    source: anthropic\n    model: claude-opus-5-override\n"
        )

        config = resolve_llm_config(config_path)

        assert config.implementation == DEFAULT_LLM_CONFIG.implementation
        assert config.review.model == "claude-opus-5-override"

    def test_partial_field_override_keeps_sibling_field_default(self, tmp_path: Path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text("llm:\n  review:\n    model: claude-opus-5-override\n")

        config = resolve_llm_config(config_path)

        assert config.review.source == ModelSource.ANTHROPIC
        assert config.review.model == "claude-opus-5-override"
        assert config.implementation == DEFAULT_LLM_CONFIG.implementation

    def test_unknown_source_raises_validation_error(self, tmp_path: Path):
        from pydantic import ValidationError

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "llm:\n  implementation:\n    source: bedrock\n    model: some-model\n"
        )

        with pytest.raises(ValidationError):
            resolve_llm_config(config_path)

    def test_malformed_yaml_raises(self, tmp_path: Path):
        import yaml

        config_path = tmp_path / "config.yaml"
        config_path.write_text("llm: [this is not: a valid mapping\n")

        with pytest.raises(yaml.YAMLError):
            resolve_llm_config(config_path)

    def test_non_mapping_top_level_raises(self, tmp_path: Path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text("- just\n- a\n- list\n")

        with pytest.raises(ValueError):
            resolve_llm_config(config_path)

    def test_non_mapping_llm_key_raises(self, tmp_path: Path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text("llm: anthropic\n")

        with pytest.raises(ValueError):
            resolve_llm_config(config_path)

    def test_non_mapping_role_raises(self, tmp_path: Path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text("llm:\n  implementation: claude-sonnet-5\n")

        with pytest.raises(ValueError):
            resolve_llm_config(config_path)

    def test_strips_leading_utf8_bom(self, tmp_path: Path):
        config_path = tmp_path / "config.yaml"
        config_path.write_bytes(
            b"\xef\xbb\xbfllm:\n"
            b"  review:\n"
            b"    source: anthropic\n"
            b"    model: claude-opus-5-override\n"
        )

        config = resolve_llm_config(config_path)

        assert config.review.model == "claude-opus-5-override"


class TestDotenvParsing:
    def test_ignores_blank_lines_and_comments(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
        credentials_path = tmp_path / "credentials.env"
        credentials_path.write_text(
            "\n# a comment\n\nDIGITALOCEAN_TOKEN=dop_v1_xyz\n# trailing comment\n"
        )

        result = resolve_credentials("digitalocean", credentials_path)

        assert result == {"DIGITALOCEAN_TOKEN": "dop_v1_xyz"}

    def test_strips_whitespace_around_key_and_value(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
        credentials_path = tmp_path / "credentials.env"
        credentials_path.write_text("  DIGITALOCEAN_TOKEN  =   dop_v1_xyz  \n")

        result = resolve_credentials("digitalocean", credentials_path)

        assert result == {"DIGITALOCEAN_TOKEN": "dop_v1_xyz"}

    def test_strips_matching_double_quotes(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
        credentials_path = tmp_path / "credentials.env"
        credentials_path.write_text('DIGITALOCEAN_TOKEN="dop_v1_xyz"\n')

        result = resolve_credentials("digitalocean", credentials_path)

        assert result == {"DIGITALOCEAN_TOKEN": "dop_v1_xyz"}

    def test_strips_matching_single_quotes(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
        credentials_path = tmp_path / "credentials.env"
        credentials_path.write_text("DIGITALOCEAN_TOKEN='dop_v1_xyz'\n")

        result = resolve_credentials("digitalocean", credentials_path)

        assert result == {"DIGITALOCEAN_TOKEN": "dop_v1_xyz"}

    def test_skips_malformed_lines_without_raising(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
        credentials_path = tmp_path / "credentials.env"
        credentials_path.write_text("this line has no equals sign\nDIGITALOCEAN_TOKEN=dop_v1_xyz\n")

        result = resolve_credentials("digitalocean", credentials_path)

        assert result == {"DIGITALOCEAN_TOKEN": "dop_v1_xyz"}

    def test_strips_leading_utf8_bom(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
        credentials_path = tmp_path / "credentials.env"
        credentials_path.write_bytes(b"\xef\xbb\xbfDIGITALOCEAN_TOKEN=dop_v1_xyz\n")

        result = resolve_credentials("digitalocean", credentials_path)

        assert result == {"DIGITALOCEAN_TOKEN": "dop_v1_xyz"}
