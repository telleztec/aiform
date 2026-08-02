from pathlib import Path

import pytest

from aiform.config import DEFAULT_CREDENTIALS_PATH, PROVIDER_TOKEN_ENV_VARS, resolve_credentials


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
