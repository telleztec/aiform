# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

import http.client
import importlib
import inspect
import io
import json
import re
import socket
import subprocess
import sys
import tomllib
import types
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from aiform import cli, config, llm, orchestrator, parser, state
from aiform.models import DriverInfo, DriverReview, KeyCheck, KeyState, StateEntry


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
        self.closed = False

    def close(self):
        self.closed = True


class FakeHttpxClient:
    def __init__(self, **kwargs):
        self.follow_redirects = kwargs.get("follow_redirects", True)


FAKE_DRIVER_SOURCE = """\
from aiform.driver import DriverUpdateNotSupported, ResourceDriver
from aiform.exceptions import ResourceNotFoundError


class Driver(ResourceDriver):
    PARAM_SCHEMA = {"type": "object", "properties": {}}
    LIKELY_REPLACE_FIELDS = ["image"]

    def create(self, name, params, credentials):
        return {"id": "new-id-1", "name": name, **params}

    def read(self, id, credentials):
        if id == "MISSING":
            raise ResourceNotFoundError(f"resource {id} not found")
        return {"id": id, "region": "sfo3", "size": "s-1vcpu-2gb"}

    def update(self, id, current, desired, credentials):
        return {"id": id, **{**current, **desired}}

    def delete(self, id, credentials):
        pass
"""


def approve_response() -> str:
    return json.dumps({"approved": True, "concerns": [], "blocking_issues": []})


def categorization_response(action="create", rationale="new resource", likely_replace=False) -> str:
    return json.dumps({"action": action, "rationale": rationale, "likely_replace": likely_replace})


def plan_review_response(safe_to_proceed=True, flags=None) -> str:
    return json.dumps({"safe_to_proceed": safe_to_proceed, "flags": flags or []})


def write_driver(drivers_dir: Path, provider: str, resource_type: str) -> Path:
    provider_dir = drivers_dir / provider
    provider_dir.mkdir(parents=True, exist_ok=True)
    path = provider_dir / f"{resource_type}.py"
    path.write_text(FAKE_DRIVER_SOURCE)
    return path


def write_aiform_md(
    path: Path,
    *,
    provider: str = "digitalocean",
    resource: str = "compute",
    name: str = "telleztec-app-01",
    params: dict | None = None,
) -> None:
    if params is None:
        params = {"region": "sfo3", "size": "s-1vcpu-2gb"}
    lines = ["---", f"resource: {resource}", f"name: {name}", f"provider: {provider}", "params:"]
    lines += [f"  {key}: {json.dumps(value)}" for key, value in params.items()]
    lines.append("---")
    path.write_text("\n".join(lines) + "\n")


def make_driver_info(sha256: str, *, path: str = "drivers/digitalocean/compute.py") -> DriverInfo:
    return DriverInfo(
        path=path,
        sha256=sha256,
        generated_at=datetime(2026, 7, 30, 18, 22, 11, tzinfo=UTC),
        code_review=DriverReview(
            approved=True,
            concerns=[],
            blocking_issues=[],
            reviewed_at=datetime(2026, 7, 30, 18, 22, 40, tzinfo=UTC),
            model="claude-opus-5",
        ),
    )


@pytest.fixture
def prompts_dir(tmp_path: Path, monkeypatch) -> Path:
    directory = tmp_path / "prompts"
    directory.mkdir()
    (directory / "diff_plan.md").write_text("Categorize the diff into a plan action.\n")
    (directory / "review_driver.md").write_text("Review the driver source for correctness.\n")
    (directory / "review_plan.md").write_text("Review the plan for safety.\n")
    monkeypatch.setattr(llm, "PROMPTS_DIR", directory)
    return directory


@pytest.fixture
def drivers_dir(tmp_path: Path, monkeypatch) -> Path:
    directory = tmp_path / "drivers"
    directory.mkdir()
    monkeypatch.setattr(orchestrator, "DRIVERS_DIR", directory)
    return directory


@pytest.fixture
def project_dir(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


class FakeHTTPResponse:
    """Duck-typed like urllib's response context manager."""

    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self, amt: int | None = None) -> bytes:
        return json.dumps(self._payload).encode("utf-8")[:amt]


class FakeRawResponse:
    """A 200 whose body is not JSON at all -- a captive portal."""

    def __init__(self, raw: bytes):
        self._raw = raw

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self, amt: int | None = None) -> bytes:
        return self._raw[:amt]


def fake_http_error(code: int, payload: dict) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://api.digitalocean.com/v2/account",
        code=code,
        msg="error",
        hdrs=None,
        fp=io.BytesIO(json.dumps(payload).encode("utf-8")),
    )


# Captured before the autouse stub below can replace it, so the tests that
# exercise the real probe can still reach it.
_REAL_CHECK_PROVIDER_TOKEN = cli._check_provider_token
_REAL_VERIFY_API_KEY = llm.verify_api_key


@pytest.fixture(autouse=True)
def offline_preflight(monkeypatch):
    """Stub both credential probes for every test in this module.

    Autouse, not opt-in: `init`'s preflight reaches the network, and
    `_guarded` swallows whatever comes back, so a test that forgets to
    stub still *passes* while firing live requests with the developer's
    real tokens and printing the account email into test output. That is
    exactly what happened before this was made autouse -- the only
    symptom was the suite getting slower.
    """
    monkeypatch.setattr(llm, "verify_api_key", lambda **kwargs: KeyCheck(state=KeyState.MISSING))
    monkeypatch.setattr(
        cli, "_check_provider_token", lambda provider: KeyCheck(state=KeyState.MISSING)
    )


def _block_sockets(monkeypatch) -> list:
    """Make every outbound connection fail, and record that it was tried."""
    attempts = []

    def record(target, *args, **kwargs):
        attempts.append(target)
        raise OSError("network disabled for this test")

    monkeypatch.setattr(socket, "create_connection", record)
    monkeypatch.setattr(socket.socket, "connect", record)
    monkeypatch.setattr(socket, "getaddrinfo", record)
    return attempts


class FakeStdinTTY:
    def isatty(self) -> bool:
        return True


class FakeStdinNotTTY:
    def isatty(self) -> bool:
        return False


def patch_client(monkeypatch, responses: list[str]) -> list[FakeClient]:
    """Stand in for the SDK client `_CountingClient` builds on its first call.

    Takes `**kwargs` rather than being a bare `lambda:` -- `llm.build_client()`
    constructs with `http_client=`, and `cli.anthropic` is the same module
    object `llm.anthropic` is, so this intercepts that construction. Returns
    the list it appends each built client to, so a caller can assert on how
    many were built and whether they were closed.
    """
    built: list[FakeClient] = []

    def _build(**kwargs):
        client = FakeClient(responses)
        client.build_kwargs = kwargs
        built.append(client)
        return client

    monkeypatch.setattr(cli.anthropic, "Anthropic", _build)
    # And the httpx client build_client hands it: nothing here needs a real
    # transport or SSL context, and nothing closes the ones the real class
    # would open. tests/test_llm.py keeps the real one, where whether
    # DefaultHttpxClient honours follow_redirects=False is the actual claim.
    monkeypatch.setattr(cli.anthropic, "DefaultHttpxClient", FakeHttpxClient)
    return built


def fail_if_anthropic_constructed(monkeypatch) -> None:
    def _boom(**kwargs):
        raise AssertionError("should not construct a real anthropic.Anthropic() client")

    monkeypatch.setattr(cli.anthropic, "Anthropic", _boom)


class TestArgParsing:
    def test_no_command_exits_nonzero(self):
        with pytest.raises(SystemExit):
            cli.main([])

    def test_unknown_command_exits_nonzero(self):
        with pytest.raises(SystemExit):
            cli.main(["bogus"])


class TestInit:
    def test_creates_aiform_dir(self, project_dir: Path):
        code = cli.main(["init"])
        assert code == 0
        assert (project_dir / ".aiform").is_dir()

    def test_never_writes_credentials_env(self, project_dir: Path):
        cli.main(["init"])
        assert not (project_dir / ".aiform" / "credentials.env").exists()

    def test_appends_gitignore_entries_once(self, project_dir: Path):
        cli.main(["init"])
        cli.main(["init"])
        lines = (project_dir / ".gitignore").read_text().splitlines()
        assert lines.count(".aiform/credentials.env") == 1
        assert lines.count(".aiform/state.json") == 1
        assert lines.count(".aiform/state.json.backup") == 1
        assert lines.count(".aiform/logs/") == 1
        assert "__pycache__/" in lines

    def test_does_not_gitignore_trash(self, project_dir: Path):
        cli.main(["init"])
        content = (project_dir / ".gitignore").read_text()
        assert "trash" not in content

    def test_creates_example_starter_file(self, project_dir: Path):
        cli.main(["init"])
        example = project_dir / "examples" / "compute.aiform.md"
        assert example.exists()
        assert "resource: compute" in example.read_text()
        assert "provider: digitalocean" in example.read_text()

    def test_does_not_overwrite_existing_example_file(self, project_dir: Path):
        cli.main(["init"])
        example = project_dir / "examples" / "compute.aiform.md"
        example.write_text("custom content")
        cli.main(["init"])
        assert example.read_text() == "custom content"

    def test_unsupported_provider_exits_2(self, project_dir: Path, capsys):
        code = cli.main(["init", "--provider", "aws"])
        assert code == 2
        err = capsys.readouterr().err
        assert "Error:" in err
        assert "aws" in err

    def test_never_fails_on_missing_credentials(self, project_dir: Path, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
        code = cli.main(["init"])
        assert code == 0

    def test_prints_credential_check_marks(self, project_dir: Path, monkeypatch, capsys):
        monkeypatch.setattr(llm, "verify_api_key", lambda **kw: KeyCheck(state=KeyState.MISSING))
        monkeypatch.setattr(
            cli, "_check_provider_token", lambda provider: KeyCheck(state=KeyState.MISSING)
        )
        cli.main(["init"])
        out = capsys.readouterr().out
        assert "✗" in out
        assert "ANTHROPIC_API_KEY" in out
        assert "DIGITALOCEAN_TOKEN" in out

        monkeypatch.setattr(llm, "verify_api_key", lambda **kw: KeyCheck(state=KeyState.OK))
        monkeypatch.setattr(
            cli, "_check_provider_token", lambda provider: KeyCheck(state=KeyState.OK)
        )
        cli.main(["init"])
        out = capsys.readouterr().out
        assert "✓" in out


class TestInitMakesNoNetworkCalls:
    """specs/cli.md claims the default pytest run makes no network calls.

    That claim was false when written: init's probes ran for real against
    the developer's tokens, and passed anyway because _guarded swallows
    everything. This asserts the claim instead of restating it, so a new
    unstubbed probe fails here rather than silently phoning home.
    """

    def test_stubbing_covers_every_path_init_takes(self, project_dir: Path, monkeypatch):
        """With the autouse stub active, init must touch no socket.

        This catches a *new* network call added to _cmd_init that nobody
        remembered to stub -- not a regression inside the two probes
        themselves, which are replaced wholesale here. The next test
        covers those.
        """
        attempts = _block_sockets(monkeypatch)

        assert cli.main(["init"]) == 0
        assert attempts == []

    def test_real_probes_degrade_instead_of_crashing_offline(
        self, project_dir: Path, monkeypatch, capsys
    ):
        """The real probes, un-stubbed, against a dead network.

        Credentials must be set or both probes short-circuit on absence and
        never reach a socket, which would make this pass for the wrong
        reason.
        """
        monkeypatch.setattr(llm, "verify_api_key", _REAL_VERIFY_API_KEY)
        monkeypatch.setattr(cli, "_check_provider_token", _REAL_CHECK_PROVIDER_TOKEN)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        attempts = _block_sockets(monkeypatch)

        assert cli.main(["init"]) == 0
        assert attempts, "probes must have actually tried, or this proves nothing"

        out = capsys.readouterr().out
        assert out.count("?") >= 2
        assert "✗" not in out


class TestInitGuidance:
    """init's printed next-step guidance (specs/cli.md).

    The scaffold lands in examples/, which discover_files() deliberately
    cannot see, so the instruction connecting the two is the only thing
    standing between a new user and an unexplained `Plan: 0 to create`.
    """

    def test_announces_the_credential_check_before_running_it(
        self, project_dir: Path, offline_preflight, capsys
    ):
        # Three sequential probes run after this line; without it the
        # command looks hung on a black-holed network.
        cli.main(["init"])
        out = capsys.readouterr().out

        assert "Checking credentials..." in out
        assert out.index("Checking credentials...") < out.index("ANTHROPIC_API_KEY -- ")

    def test_names_the_copy_step(self, project_dir: Path, offline_preflight, capsys):
        cli.main(["init"])
        out = capsys.readouterr().out

        assert "examples/compute.aiform.md" in out
        assert "cp " in out

    def test_states_that_discovery_is_cwd_only(self, project_dir: Path, offline_preflight, capsys):
        cli.main(["init"])
        out = capsys.readouterr().out

        assert "current directory" in out.lower()

    def test_scaffold_stays_out_of_discovery_range(
        self, project_dir: Path, offline_preflight, capsys
    ):
        cli.main(["init"])

        assert orchestrator.discover_files(None, cwd=project_dir) == []

    def test_names_an_invocation_that_exists(self, project_dir: Path, offline_preflight, capsys):
        cli.main(["init"])
        out = capsys.readouterr().out

        assert "`aiform plan create`" in out or "python -m aiform plan create" in out


class TestConsoleScript:
    def test_pyproject_declares_the_entry_point(self):
        pyproject = tomllib.loads(
            (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8")
        )

        assert pyproject["project"]["scripts"]["aiform"] == "aiform.cli:main"

    def test_entry_point_target_resolves_and_takes_no_required_args(self):
        # A console script is invoked as `main()` with no arguments, so
        # every parameter must have a default or the command dies on
        # startup for everyone who installed it.
        module_path, _, attr = "aiform.cli:main".partition(":")
        entry = getattr(importlib.import_module(module_path), attr)

        assert callable(entry)
        required = [
            name
            for name, param in inspect.signature(entry).parameters.items()
            if param.default is inspect.Parameter.empty
        ]
        assert required == []


class TestScaffoldSshKeys:
    """DO's POST /v2/droplets takes key IDs or fingerprints, never names --
    a name-shaped placeholder teaches a shape that 422s."""

    def test_placeholder_is_not_a_key_name(self, project_dir: Path, offline_preflight):
        cli.main(["init"])
        scaffold = (project_dir / "examples" / "compute.aiform.md").read_text()

        assert "your-ssh-key-name" not in scaffold

    def test_placeholder_is_fingerprint_shaped(self, project_dir: Path, offline_preflight):
        cli.main(["init"])
        scaffold = (project_dir / "examples" / "compute.aiform.md").read_text()
        spec = yaml.safe_load(scaffold.split("---")[1])

        for key in spec["params"]["ssh_keys"]:
            assert re.fullmatch(r"(?:[0-9a-f]{2}:){15}[0-9a-f]{2}", key), key

    def test_points_at_how_to_find_a_real_key(self, project_dir: Path, offline_preflight):
        cli.main(["init"])
        scaffold = (project_dir / "examples" / "compute.aiform.md").read_text()

        assert "doctl compute ssh-key list" in scaffold

    def test_scaffold_still_parses_as_a_resource_spec(self, project_dir: Path, offline_preflight):
        # The comment lines added above the placeholder sit inside the YAML
        # frontmatter, so this guards against breaking the scaffold while
        # fixing its content.
        cli.main(["init"])
        scaffold = (project_dir / "examples" / "compute.aiform.md").read_text()

        spec = parser.parse_frontmatter(scaffold)

        assert spec.resource == "compute"
        assert spec.provider == "digitalocean"
        assert spec.params["ssh_keys"]


class TestInitPreflight:
    """The four-state credential check (specs/cli.md).

    Collapsing these into set/unset is what let an identity-linked key
    that 400s on every endpoint report a green check.
    """

    def test_unset_key_reports_not_set(self, project_dir: Path, monkeypatch, capsys):
        monkeypatch.setattr(llm, "verify_api_key", lambda **kw: KeyCheck(state=KeyState.MISSING))
        monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)

        cli.main(["init"])
        out = capsys.readouterr().out

        assert "✗" in out
        assert "not set" in out

    def test_accepted_key_reports_a_check(self, project_dir: Path, monkeypatch, capsys):
        monkeypatch.setattr(llm, "verify_api_key", lambda **kw: KeyCheck(state=KeyState.OK))
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        monkeypatch.setattr(cli, "_check_provider_token", lambda p: KeyCheck(state=KeyState.OK))

        cli.main(["init"])
        out = capsys.readouterr().out

        assert "✓" in out
        assert "✗" not in out

    def test_rejected_key_is_a_cross_carrying_the_api_error(
        self, project_dir: Path, monkeypatch, capsys
    ):
        monkeypatch.setattr(
            llm,
            "verify_api_key",
            lambda **kw: KeyCheck(state=KeyState.REJECTED, detail="workspace is not accessible"),
        )
        monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)

        cli.main(["init"])
        out = capsys.readouterr().out

        assert "✗" in out
        assert "workspace is not accessible" in out

    def test_a_set_but_rejected_key_is_distinguishable_from_an_unset_one(
        self, project_dir: Path, monkeypatch, capsys
    ):
        monkeypatch.setattr(
            llm, "verify_api_key", lambda **kw: KeyCheck(state=KeyState.REJECTED, detail="nope")
        )
        monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
        cli.main(["init"])
        rejected = capsys.readouterr().out

        monkeypatch.setattr(llm, "verify_api_key", lambda **kw: KeyCheck(state=KeyState.MISSING))
        cli.main(["init"])
        missing = capsys.readouterr().out

        assert rejected != missing

    def test_unverifiable_key_is_neither_a_check_nor_a_cross(
        self, project_dir: Path, monkeypatch, capsys
    ):
        # Offline must not be reported as an invalid key.
        monkeypatch.setattr(
            llm,
            "verify_api_key",
            lambda **kw: KeyCheck(state=KeyState.UNVERIFIED, detail="Connection error."),
        )
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        monkeypatch.setattr(
            cli, "_check_provider_token", lambda p: KeyCheck(state=KeyState.UNVERIFIED)
        )

        cli.main(["init"])
        out = capsys.readouterr().out

        assert "?" in out
        assert "✗" not in out

    @pytest.mark.parametrize("key_state", list(KeyState))
    def test_exit_is_zero_whatever_the_probe_says(self, project_dir: Path, monkeypatch, key_state):
        check = KeyCheck(state=key_state)
        monkeypatch.setattr(llm, "verify_api_key", lambda **kwargs: check)
        monkeypatch.setattr(cli, "_check_provider_token", lambda provider: check)

        assert cli.main(["init"]) == 0

    def test_probe_failure_never_propagates(self, project_dir: Path, monkeypatch):
        def explode(**kwargs):
            raise RuntimeError("probe blew up")

        monkeypatch.setattr(llm, "verify_api_key", explode)
        monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)

        assert cli.main(["init"]) == 0


class TestCheckProviderToken:
    """The DigitalOcean half of the preflight. GET /v2/account is free and
    read-only; the account email it returns is what distinguishes a token
    for the wrong one of several accounts from a working one."""

    @pytest.fixture(autouse=True)
    def no_sockets(self, monkeypatch):
        # Three tests here hand a malformed token to the real urllib stack,
        # which reaches no socket only because putheader rejects the header
        # before endheaders connects. Should that guard ever move after the
        # connect, they would talk to DigitalOcean for real -- and still pass,
        # since _probe turns a failed connection into UNVERIFIED and _guarded
        # swallows anything raised from inside the probe. So the assertion has
        # to outlive the call rather than be raised during it.
        attempts = _block_sockets(monkeypatch)
        yield
        assert not attempts, "this test must not reach the network"

    @pytest.fixture
    def token(self, monkeypatch):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")

    def _urlopen(self, monkeypatch, result):
        def fake_urlopen(request, timeout=None):
            if isinstance(result, Exception):
                raise result
            return result

        monkeypatch.setattr(cli, "_open", fake_urlopen)

    def test_unset_token_is_missing_without_a_request(self, project_dir, monkeypatch):
        monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)

        def explode(request, timeout=None):
            raise AssertionError("probe must not run without a token")

        monkeypatch.setattr(cli, "_open", explode)

        assert _REAL_CHECK_PROVIDER_TOKEN("digitalocean").state is KeyState.MISSING

    def test_accepted_token_reports_the_account_email(self, token, monkeypatch):
        self._urlopen_sequence(
            monkeypatch,
            [
                FakeHTTPResponse({"account": {"email": "juan@example.com"}}),
                FakeHTTPResponse({"droplets": []}),
            ],
        )

        check = _REAL_CHECK_PROVIDER_TOKEN("digitalocean")

        assert check.state is KeyState.OK
        assert check.detail == "juan@example.com"

    def test_account_read_without_droplet_scope_is_rejected(self, token, monkeypatch):
        # The false green reached by the success path: a token granted
        # account:read but no droplet scope answers 200 on /v2/account and
        # then 403s on every apply.
        self._urlopen_sequence(
            monkeypatch,
            [
                FakeHTTPResponse({"account": {"email": "juan@example.com"}}),
                fake_http_error(403, {"message": "You are not authorized"}),
            ],
        )

        check = _REAL_CHECK_PROVIDER_TOKEN("digitalocean")

        assert check.state is KeyState.REJECTED
        assert "droplets" in check.detail

    def test_droplet_scope_is_checked_even_when_the_account_reads_fine(self, token, monkeypatch):
        urls = self._urlopen_sequence(
            monkeypatch,
            [
                FakeHTTPResponse({"account": {"email": "juan@example.com"}}),
                FakeHTTPResponse({"droplets": []}),
            ],
        )

        _REAL_CHECK_PROVIDER_TOKEN("digitalocean")

        assert urls == [
            config.PROVIDER_ACCOUNT_PROBES["digitalocean"],
            config.PROVIDER_DROPLET_PROBES["digitalocean"],
        ]

    def test_revoked_token_401_is_rejected(self, token, monkeypatch):
        self._urlopen(monkeypatch, fake_http_error(401, {"message": "Unable to authenticate you"}))

        check = _REAL_CHECK_PROVIDER_TOKEN("digitalocean")

        assert check.state is KeyState.REJECTED
        assert "Unable to authenticate you" in check.detail

    def _urlopen_sequence(self, monkeypatch, results):
        """Answer successive probes with successive results -- the account
        probe first, then the droplet-scope fallback."""
        remaining = list(results)
        urls = []

        def fake_urlopen(request, timeout=None):
            urls.append(request.full_url)
            result = remaining.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        monkeypatch.setattr(cli, "_open", fake_urlopen)
        return urls

    def test_scoped_token_with_droplet_access_is_accepted(self, token, monkeypatch):
        # A DigitalOcean token scoped to droplets but not account:read gets
        # 403 from /v2/account. It authenticated, and it can do what aiform
        # needs -- reporting it as rejected would send the user to replace a
        # working token. Observed against the real API with this repo's own
        # token, which is exactly this shape.
        forbidden = fake_http_error(403, {"message": "You are not authorized"})
        urls = self._urlopen_sequence(monkeypatch, [forbidden, FakeHTTPResponse({"droplets": []})])

        check = _REAL_CHECK_PROVIDER_TOKEN("digitalocean")

        assert check.state is KeyState.OK
        assert "scoped" in check.detail
        assert urls == [
            config.PROVIDER_ACCOUNT_PROBES["digitalocean"],
            config.PROVIDER_DROPLET_PROBES["digitalocean"],
        ]

    def test_token_without_droplet_scope_is_rejected(self, token, monkeypatch):
        # "Real token somewhere" is not the question. A token scoped without
        # droplet access is 403 on both probes and fails every apply.
        forbidden = fake_http_error(403, {"message": "You are not authorized"})
        self._urlopen_sequence(monkeypatch, [forbidden, forbidden])

        check = _REAL_CHECK_PROVIDER_TOKEN("digitalocean")

        assert check.state is KeyState.REJECTED
        assert "droplets" in check.detail

    def test_transient_droplet_failure_keeps_the_proven_account_result(self, token, monkeypatch):
        # The account probe already authenticated the token one request
        # earlier. A rate limit on the second must not throw that away and
        # report a working token as unverifiable.
        self._urlopen_sequence(
            monkeypatch,
            [
                FakeHTTPResponse({"account": {"email": "juan@example.com"}}),
                fake_http_error(429, {"message": "slow down"}),
            ],
        )

        check = _REAL_CHECK_PROVIDER_TOKEN("digitalocean")

        assert check.state is KeyState.OK
        assert "juan@example.com" in check.detail
        assert "unverified" in check.detail

    def test_droplet_redirect_is_distrusted_not_shrugged_off(self, token, monkeypatch):
        # A redirect on a token-bearing request is the one thing
        # _RejectRedirects exists to distrust; it must not fall into the
        # benign "inconclusive" bucket and print a green check.
        self._urlopen_sequence(
            monkeypatch,
            [
                FakeHTTPResponse({"account": {"email": "juan@example.com"}}),
                fake_http_error(302, {"message": "moved"}),
            ],
        )

        check = _REAL_CHECK_PROVIDER_TOKEN("digitalocean")

        assert check.state is KeyState.UNVERIFIED
        assert "redirect" in check.detail

    def test_malformed_droplet_body_keeps_the_proven_account_result(self, token, monkeypatch):
        # Same rule as a transient failure: a malformed second response is
        # not evidence against a token the first response authenticated.
        self._urlopen_sequence(
            monkeypatch,
            [
                FakeHTTPResponse({"account": {"email": "juan@example.com"}}),
                FakeHTTPResponse({"unexpected": True}),
            ],
        )

        check = _REAL_CHECK_PROVIDER_TOKEN("digitalocean")

        assert check.state is KeyState.OK
        assert "juan@example.com" in check.detail
        assert "unverified" in check.detail

    def test_account_read_without_email_is_not_labelled_a_scoped_token(self, token, monkeypatch):
        # "scoped token" means specifically the token that could NOT read
        # the account. One that read it and carried no email is not that.
        self._urlopen_sequence(
            monkeypatch,
            [
                FakeHTTPResponse({"account": {"status": "active"}}),
                FakeHTTPResponse({"droplets": []}),
            ],
        )

        check = _REAL_CHECK_PROVIDER_TOKEN("digitalocean")

        assert check.state is KeyState.OK
        assert "scoped" not in check.detail

    def test_droplet_401_outranks_a_good_account_probe(self, token, monkeypatch):
        # The token was revoked or rotated between the two requests. A 401
        # is unambiguous and must outrank what /v2/account said a moment
        # earlier -- otherwise a dead token prints a green check.
        self._urlopen_sequence(
            monkeypatch,
            [
                FakeHTTPResponse({"account": {"email": "juan@example.com"}}),
                fake_http_error(401, {"message": "Unable to authenticate you"}),
            ],
        )

        check = _REAL_CHECK_PROVIDER_TOKEN("digitalocean")

        assert check.state is KeyState.REJECTED

    def test_account_200_without_an_email_still_counts_as_authenticated(self, token, monkeypatch):
        # A 200 carrying no email still proves the token works. Collapsing
        # that to the same "no email" signal as a 403 would downgrade a
        # proven token on any transient droplet failure.
        self._urlopen_sequence(
            monkeypatch,
            [
                FakeHTTPResponse({"account": {"status": "active"}}),
                fake_http_error(429, {"message": "slow down"}),
            ],
        )

        check = _REAL_CHECK_PROVIDER_TOKEN("digitalocean")

        assert check.state is KeyState.OK
        assert "unverified" in check.detail

    def test_truncated_body_does_not_escape_as_an_exception(self, token, monkeypatch):
        # IncompleteRead descends from Exception, not OSError, so it would
        # otherwise escape a function specified to return a KeyCheck.
        class Truncated:
            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

            def read(self, amt=None):
                raise http.client.IncompleteRead(b"partial")

        monkeypatch.setattr(cli, "_open", lambda request, timeout=None: Truncated())

        assert _REAL_CHECK_PROVIDER_TOKEN("digitalocean").state is KeyState.UNVERIFIED

    @pytest.mark.parametrize("code", [400, 404])
    def test_bad_endpoint_is_unverified_not_a_token_verdict(self, token, monkeypatch, code):
        # The probe URLs are hardcoded, so a 404 is far likelier to be
        # aiform's problem than the token's. Calling it "rejected" sends the
        # user to rotate a working credential.
        self._urlopen(monkeypatch, fake_http_error(code, {"message": "not found"}))

        assert _REAL_CHECK_PROVIDER_TOKEN("digitalocean").state is KeyState.UNVERIFIED

    def test_forbidden_account_with_no_scope_probe_is_not_a_pass(self, token, monkeypatch):
        # A provider configured with an account probe but no scope probe:
        # a 403 proved nothing, so it must not read as a green check.
        monkeypatch.setitem(config.PROVIDER_DROPLET_PROBES, "digitalocean", None)
        monkeypatch.delitem(config.PROVIDER_DROPLET_PROBES, "digitalocean")
        self._urlopen(monkeypatch, fake_http_error(403, {"message": "You are not authorized"}))

        assert _REAL_CHECK_PROVIDER_TOKEN("digitalocean").state is KeyState.UNVERIFIED

    def test_body_read_is_capped(self, token, monkeypatch):
        # urllib's timeout bounds each socket read, not the total transfer.
        requested = []

        class Endless:
            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

            def read(self, amt=None):
                requested.append(amt)
                return json.dumps({"account": {"email": "j@example.com"}}).encode()

        monkeypatch.setattr(cli, "_open", lambda request, timeout=None: Endless())

        _REAL_CHECK_PROVIDER_TOKEN("digitalocean")

        assert requested and all(a == cli._MAX_PROBE_BODY for a in requested)

    @pytest.mark.parametrize("code", [408, 429])
    def test_rate_limited_or_timed_out_is_unverified_not_rejected(self, token, monkeypatch, code):
        # DO's limiter is shared with anything else using the token (doctl
        # included); a 429 says nothing about whether the token is good.
        self._urlopen(monkeypatch, fake_http_error(code, {"message": "slow down"}))

        assert _REAL_CHECK_PROVIDER_TOKEN("digitalocean").state is KeyState.UNVERIFIED

    def test_success_body_of_the_wrong_shape_is_unverified(self, token, monkeypatch):
        # A proxy or captive portal answering 200 with arbitrary JSON is not
        # evidence the token works -- and must not raise out of a function
        # specified to return a KeyCheck.
        self._urlopen(monkeypatch, FakeHTTPResponse(["not", "a", "dict"]))

        assert _REAL_CHECK_PROVIDER_TOKEN("digitalocean").state is KeyState.UNVERIFIED

    def test_success_body_missing_account_is_unverified(self, token, monkeypatch):
        self._urlopen(monkeypatch, FakeHTTPResponse({"something_else": 1}))

        assert _REAL_CHECK_PROVIDER_TOKEN("digitalocean").state is KeyState.UNVERIFIED

    def test_redirect_is_refused_not_followed(self, token, monkeypatch):
        # urllib re-sends Authorization to a redirect target, so following
        # one would leak the provider token to wherever it pointed.
        self._urlopen(monkeypatch, fake_http_error(302, {"message": "moved"}))

        check = _REAL_CHECK_PROVIDER_TOKEN("digitalocean")

        assert check.state is KeyState.UNVERIFIED
        assert "redirect" in check.detail

    def test_opener_does_not_follow_redirects(self):
        # The guard above only holds because the opener refuses redirects
        # rather than chasing them before we ever see a status.
        handlers = [type(h).__name__ for h in cli._PROBE_OPENER.handlers]

        assert "_RejectRedirects" in handlers
        assert (
            cli._RejectRedirects().redirect_request(
                None, None, 302, "Found", {}, "https://evil.example.com"
            )
            is None
        )

    def test_redirect_is_refused_before_its_location_is_parsed(self):
        # HTTPRedirectHandler.http_error_302 runs urlparse(newurl) before it
        # ever consults redirect_request, so an unparseable Location raised a
        # ValueError that _probe then blamed on the token (#91). The handler
        # refuses the 3xx itself rather than parsing a URL it has already
        # decided not to follow.
        headers = http.client.HTTPMessage()
        headers["Location"] = "http://[::1"
        request = urllib.request.Request("https://api.digitalocean.com/v2/account")

        for code in (302, 307):
            with pytest.raises(urllib.error.HTTPError) as raised:
                cli._RejectRedirects().http_error_302(
                    request, io.BytesIO(b""), code, "Found", headers
                )

            assert raised.value.code == code

    def test_opener_raises_an_unparseable_redirect_rather_than_a_value_error(self):
        # The composition is what broke in #91, not either half: _probe's
        # ValueError came out of _PROBE_OPENER's own handler chain. Asserting
        # only on the handler would stay green if the chain stopped routing
        # through it, or if a stock HTTPRedirectHandler were re-registered.
        headers = http.client.HTTPMessage()
        headers["Location"] = "http://[::1"
        request = urllib.request.Request("https://api.digitalocean.com/v2/account")

        with pytest.raises(urllib.error.HTTPError) as raised:
            cli._PROBE_OPENER.error("http", request, io.BytesIO(b""), 302, "Found", headers)

        assert raised.value.code == 302

    def test_unparseable_redirect_location_does_not_blame_the_token(self, token, monkeypatch):
        # Backstop for every other ValueError the opener can raise -- a
        # malformed https_proxy in the environment is the live one. A good
        # token must never be reported as malformed because of it.
        self._urlopen(monkeypatch, ValueError("Invalid IPv6 URL"))

        check = _REAL_CHECK_PROVIDER_TOKEN("digitalocean")

        assert check.state is KeyState.UNVERIFIED
        assert "whitespace" not in (check.detail or "")
        assert "token" not in (check.detail or "")

    def test_non_json_success_body_is_unverified_not_a_crash(self, token, monkeypatch):
        self._urlopen(monkeypatch, FakeRawResponse(b"<html>captive portal</html>"))

        assert _REAL_CHECK_PROVIDER_TOKEN("digitalocean").state is KeyState.UNVERIFIED

    def test_server_error_is_unverified_not_rejected(self, token, monkeypatch):
        self._urlopen(monkeypatch, fake_http_error(503, {"message": "unavailable"}))

        assert _REAL_CHECK_PROVIDER_TOKEN("digitalocean").state is KeyState.UNVERIFIED

    def test_offline_is_unverified_not_rejected(self, token, monkeypatch):
        self._urlopen(monkeypatch, urllib.error.URLError("name resolution failed"))

        check = _REAL_CHECK_PROVIDER_TOKEN("digitalocean")

        assert check.state is KeyState.UNVERIFIED
        assert check.detail

    def test_sends_the_token_as_a_bearer_header(self, token, monkeypatch):
        sent = {}

        bodies = [
            FakeHTTPResponse({"account": {"email": "x@example.com"}}),
            FakeHTTPResponse({"droplets": []}),
        ]

        def capture(request, timeout=None):
            sent.setdefault("auth", []).append(request.get_header("Authorization"))
            sent.setdefault("urls", []).append(request.full_url)
            return bodies.pop(0)

        monkeypatch.setattr(cli, "_open", capture)

        _REAL_CHECK_PROVIDER_TOKEN("digitalocean")

        assert sent["auth"] == ["Bearer dop_v1_test", "Bearer dop_v1_test"]
        assert sent["urls"][0] == config.PROVIDER_ACCOUNT_PROBES["digitalocean"]

    def test_malformed_token_never_reaches_output(self, monkeypatch, capsys):
        # A trailing newline is the usual malformation for a secret read
        # from a file, and .aiform/credentials.env is hand-edited. http.client
        # rejects the header and quotes the whole value -- including the
        # token -- in the exception message.
        secret = "dop_v1_supersecretvalue"
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", secret + "\n")

        check = _REAL_CHECK_PROVIDER_TOKEN("digitalocean")

        assert check.state is KeyState.UNVERIFIED
        assert secret not in (check.detail or "")
        assert secret not in capsys.readouterr().out

    def test_malformed_token_still_names_the_token(self, monkeypatch):
        # The complement of the test above: distinguishing the two ValueError
        # sources must not cost the one message that actually helps.
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_supersecretvalue\n")

        check = _REAL_CHECK_PROVIDER_TOKEN("digitalocean")

        assert check.state is KeyState.UNVERIFIED
        assert "whitespace" in (check.detail or "")

    def test_init_never_prints_a_malformed_token(self, project_dir: Path, monkeypatch, capsys):
        secret = "dop_v1_supersecretvalue"
        monkeypatch.setattr(llm, "verify_api_key", lambda **kw: KeyCheck(state=KeyState.MISSING))
        monkeypatch.setattr(cli, "_check_provider_token", _REAL_CHECK_PROVIDER_TOKEN)
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", secret + "\n")

        cli.main(["init"])

        captured = capsys.readouterr()
        assert secret not in captured.out
        assert secret not in captured.err

    def test_redaction_covers_any_detail_carrying_the_token(self, token, monkeypatch):
        # Defense in depth: every detail leaving _probe is redacted, not just
        # the one path known to quote the header.
        self._urlopen(monkeypatch, OSError("connect failed for Bearer dop_v1_test"))

        check = _REAL_CHECK_PROVIDER_TOKEN("digitalocean")

        assert "dop_v1_test" not in (check.detail or "")

    def test_never_prints_or_returns_the_token(self, token, monkeypatch, capsys):
        self._urlopen(monkeypatch, fake_http_error(401, {"message": "bad token"}))

        check = _REAL_CHECK_PROVIDER_TOKEN("digitalocean")

        assert "dop_v1_test" not in (check.detail or "")
        assert "dop_v1_test" not in capsys.readouterr().out


class TestPlanCreate:
    def test_new_resource_prints_create_plan(
        self, project_dir, drivers_dir, prompts_dir, monkeypatch, capsys
    ):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        write_aiform_md(project_dir / "app.aiform.md")
        patch_client(monkeypatch, [approve_response(), categorization_response(action="create")])

        code = cli.main(["plan", "create", "--state-file", str(project_dir / ".aiform/state.json")])

        out = capsys.readouterr().out
        assert code == 0
        assert "digitalocean.compute.telleztec-app-01" in out
        assert "create" in out

    def test_json_output_is_parseable(
        self, project_dir, drivers_dir, prompts_dir, monkeypatch, capsys
    ):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        write_aiform_md(project_dir / "app.aiform.md")
        patch_client(monkeypatch, [approve_response(), categorization_response(action="create")])
        state_file = project_dir / ".aiform" / "state.json"

        code = cli.main(["plan", "create", "--state-file", str(state_file), "--json"])

        out = capsys.readouterr().out
        assert code == 0
        payload = json.loads(out)
        assert payload["plan"][0]["resource_key"] == "digitalocean.compute.telleztec-app-01"
        assert payload["plan"][0]["action"] == "create"
        assert payload["warnings"] == []

    def test_missing_driver_exits_2_with_clean_error(
        self, project_dir, drivers_dir, prompts_dir, monkeypatch, capsys
    ):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_aiform_md(project_dir / "app.aiform.md")
        state_file = project_dir / ".aiform" / "state.json"

        code = cli.main(["plan", "create", "--state-file", str(state_file)])

        err = capsys.readouterr().err
        assert code == 2
        assert "Error:" in err

    def test_explicit_missing_file_exits_2(self, project_dir, capsys):
        state_file = project_dir / ".aiform" / "state.json"
        code = cli.main(
            ["plan", "create", "does-not-exist.aiform.md", "--state-file", str(state_file)]
        )
        err = capsys.readouterr().err
        assert code == 2
        assert "Error:" in err

    def test_operational_error_is_also_logged(self, project_dir, capsys):
        # log.configure() (called by cli.main()) sets propagate=False on
        # the "aiform" logger, which keeps caplog's root-attached handler
        # from seeing these records -- asserting against the real stderr
        # stream instead is what this feature actually promises, and
        # sidesteps that plumbing detail entirely.
        state_file = project_dir / ".aiform" / "state.json"

        cli.main(["plan", "create", "does-not-exist.aiform.md", "--state-file", str(state_file)])

        err = capsys.readouterr().err
        assert "ERROR" in err
        assert "aiform.cli" in err
        assert "exception_type=FileNotFoundError" in err

    def test_bad_logging_config_fails_cleanly_instead_of_a_raw_traceback(self, project_dir, capsys):
        # resolve_logging_config() runs before log.configure() -- it has
        # to, configure() needs its result -- so a bad logging: section
        # can't go through the normal logged-error path. It must still
        # exit the same clean way every other _HANDLED_EXCEPTIONS case
        # does, not crash with an uncaught ValidationError traceback.
        config_path = project_dir / ".aiform" / "config.yaml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("logging:\n  level: NOT_A_REAL_LEVEL\n")
        state_file = project_dir / ".aiform" / "state.json"

        code = cli.main(["plan", "show", "--state-file", str(state_file)])

        err = capsys.readouterr().err
        assert code == 2
        assert "Error:" in err
        assert "Traceback" not in err
        assert not (project_dir / ".aiform" / "logs").exists()

    def test_second_run_on_unchanged_project_makes_zero_llm_calls(
        self, project_dir, drivers_dir, prompts_dir, monkeypatch, capsys
    ):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        write_aiform_md(project_dir / "app.aiform.md")
        state_file = project_dir / ".aiform" / "state.json"

        patch_client(monkeypatch, [approve_response(), categorization_response(action="create")])
        code = cli.main(["plan", "apply", "--yes", "--state-file", str(state_file)])
        capsys.readouterr()
        assert code == 0

        fail_if_anthropic_constructed(monkeypatch)
        code = cli.main(["plan", "create", "--state-file", str(state_file), "--verbose"])
        out, err = capsys.readouterr()

        assert code == 0
        assert "no-op" in out or "no changes" in out.lower() or "0 to create" in out
        assert "[verbose] 0 Anthropic API call(s) made" in err

    def test_the_counting_client_refuses_redirects(
        self, project_dir, drivers_dir, prompts_dir, monkeypatch, capsys
    ):
        # _CountingClient is what every plan/apply/destroy actually calls
        # through -- llm._anthropic_call's own client-building branch is
        # never reached from the CLI -- so the #101 hardening has to hold
        # here or it holds nowhere that ships.
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        write_aiform_md(project_dir / "app.aiform.md")
        built = patch_client(
            monkeypatch, [approve_response(), categorization_response(action="create")]
        )

        code = cli.main(["plan", "create", "--state-file", str(project_dir / ".aiform/state.json")])
        capsys.readouterr()

        assert code == 0
        assert len(built) == 1
        assert built[0].build_kwargs["http_client"].follow_redirects is False

    def test_the_counting_client_is_closed_when_the_command_ends(
        self, project_dir, drivers_dir, prompts_dir, monkeypatch, capsys
    ):
        # http_client= costs the SDK wrapper's __del__, which is what closed
        # the pool; without an explicit close the socket outlives the run.
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        write_aiform_md(project_dir / "app.aiform.md")
        built = patch_client(
            monkeypatch, [approve_response(), categorization_response(action="create")]
        )

        code = cli.main(["plan", "create", "--state-file", str(project_dir / ".aiform/state.json")])
        capsys.readouterr()

        assert code == 0
        assert built[0].closed

    def test_the_counting_client_is_closed_even_when_the_command_fails(
        self, project_dir, drivers_dir, prompts_dir, monkeypatch, capsys
    ):
        # Same reason _report_verbose_calls sits in a finally: the error
        # exit path is not allowed to leak the socket either.
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        write_aiform_md(project_dir / "app.aiform.md")
        built = patch_client(
            monkeypatch,
            [
                json.dumps(
                    {
                        "approved": False,
                        "concerns": [],
                        "blocking_issues": ["reads ANTHROPIC_API_KEY"],
                    }
                )
            ],
        )

        code = cli.main(["plan", "create", "--state-file", str(project_dir / ".aiform/state.json")])
        capsys.readouterr()

        assert code == 2
        assert built[0].closed

    def test_a_zero_call_run_is_closed_without_building_a_client(
        self, project_dir, drivers_dir, prompts_dir, monkeypatch, capsys
    ):
        # The close must not undo _CountingClient's laziness: a run that
        # makes no model call must still never construct one, or a
        # zero-call plan newly requires ANTHROPIC_API_KEY to be set.
        #
        # Recorded rather than left to fail_if_anthropic_constructed, which
        # test_second_run_on_unchanged_project_makes_zero_llm_calls already
        # applies to this same scenario: `closed == [None]` says close() was
        # reached exactly once *and* found nothing to close, which is the
        # half of the property that is this change's to keep.
        closed = []

        class _RecordingCountingClient(cli._CountingClient):
            def close(self):
                closed.append(self._real)
                super().close()

        monkeypatch.setattr(cli, "_CountingClient", _RecordingCountingClient)
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        write_aiform_md(project_dir / "app.aiform.md")
        state_file = project_dir / ".aiform" / "state.json"

        patch_client(monkeypatch, [approve_response(), categorization_response(action="create")])
        assert cli.main(["plan", "apply", "--yes", "--state-file", str(state_file)]) == 0
        capsys.readouterr()
        closed.clear()

        fail_if_anthropic_constructed(monkeypatch)
        code = cli.main(["plan", "create", "--state-file", str(state_file), "--verbose"])
        err = capsys.readouterr().err

        assert code == 0
        assert "[verbose] 0 Anthropic API call(s) made" in err
        assert closed == [None]


class TestPlanApply:
    def test_apply_with_yes_creates_resource_and_persists_state(
        self, project_dir, drivers_dir, prompts_dir, monkeypatch, capsys
    ):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        write_aiform_md(project_dir / "app.aiform.md")
        state_file = project_dir / ".aiform" / "state.json"
        patch_client(monkeypatch, [approve_response(), categorization_response(action="create")])

        code = cli.main(["plan", "apply", "--yes", "--state-file", str(state_file)])

        out = capsys.readouterr().out
        assert code == 0
        assert "digitalocean.compute.telleztec-app-01" in out
        reloaded = state.load(state_file)
        assert "digitalocean.compute.telleztec-app-01" in reloaded.resources

    def test_apply_without_yes_and_no_tty_fails_cleanly(
        self, project_dir, drivers_dir, prompts_dir, monkeypatch, capsys
    ):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        write_aiform_md(project_dir / "app.aiform.md")
        state_file = project_dir / ".aiform" / "state.json"
        patch_client(monkeypatch, [approve_response(), categorization_response(action="create")])
        monkeypatch.setattr(cli.sys, "stdin", FakeStdinNotTTY())

        code = cli.main(["plan", "apply", "--state-file", str(state_file)])

        err = capsys.readouterr().err
        assert code == 2
        assert "Error:" in err
        assert "TTY" in err or "tty" in err

    def test_apply_declined_confirmation_aborts_without_applying(
        self, project_dir, drivers_dir, prompts_dir, monkeypatch, capsys
    ):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        write_aiform_md(project_dir / "app.aiform.md")
        state_file = project_dir / ".aiform" / "state.json"
        patch_client(monkeypatch, [approve_response(), categorization_response(action="create")])
        monkeypatch.setattr(cli.sys, "stdin", FakeStdinTTY())
        monkeypatch.setattr("builtins.input", lambda prompt="": "n")

        code = cli.main(["plan", "apply", "--state-file", str(state_file)])

        out = capsys.readouterr().out
        assert code == 1
        assert "abort" in out.lower()
        reloaded = state.load(state_file)
        assert reloaded.resources == {}

    def test_apply_verbose_reports_call_count(
        self, project_dir, drivers_dir, prompts_dir, monkeypatch, capsys
    ):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        write_aiform_md(project_dir / "app.aiform.md")
        state_file = project_dir / ".aiform" / "state.json"
        patch_client(monkeypatch, [approve_response(), categorization_response(action="create")])

        code = cli.main(["plan", "apply", "--yes", "--state-file", str(state_file), "--verbose"])

        err = capsys.readouterr().err
        assert code == 0
        assert "[verbose] 2 Anthropic API call(s) made" in err

    def test_verbose_promotes_structured_log_level_to_info(
        self, project_dir, drivers_dir, prompts_dir, monkeypatch, capsys
    ):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        write_aiform_md(project_dir / "app.aiform.md")
        state_file = project_dir / ".aiform" / "state.json"
        patch_client(monkeypatch, [approve_response(), categorization_response(action="create")])

        cli.main(["plan", "apply", "--yes", "--state-file", str(state_file), "--verbose"])

        err = capsys.readouterr().err
        assert "aiform.llm" in err
        assert "role=code_review" in err

    def test_without_verbose_structured_info_lines_are_suppressed(
        self, project_dir, drivers_dir, prompts_dir, monkeypatch, capsys
    ):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        write_aiform_md(project_dir / "app.aiform.md")
        state_file = project_dir / ".aiform" / "state.json"
        patch_client(monkeypatch, [approve_response(), categorization_response(action="create")])

        cli.main(["plan", "apply", "--yes", "--state-file", str(state_file)])

        err = capsys.readouterr().err
        assert "aiform.llm" not in err

    def test_log_file_captures_the_full_trail_even_without_verbose(
        self, project_dir, drivers_dir, prompts_dir, monkeypatch, capsys
    ):
        # The whole point of the file sink: it must not depend on -v at
        # all. Same scenario as test_without_verbose_structured_info_lines_are_suppressed
        # (stderr stays quiet), but the .aiform/logs/ file must have
        # captured everything anyway.
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        write_aiform_md(project_dir / "app.aiform.md")
        state_file = project_dir / ".aiform" / "state.json"
        patch_client(monkeypatch, [approve_response(), categorization_response(action="create")])

        cli.main(["plan", "apply", "--yes", "--state-file", str(state_file)])
        capsys.readouterr()

        log_files = list((project_dir / ".aiform" / "logs").glob("*.log"))
        assert len(log_files) == 1
        content = log_files[0].read_text()
        assert "aiform.llm" in content
        assert "role=code_review" in content

    def test_log_file_has_entry_and_exit_lines_for_every_invocation(
        self, project_dir, drivers_dir, prompts_dir, monkeypatch, capsys
    ):
        # Every invocation gets a bracketing entry/exit pair, even one
        # (like this successful apply) that already logs plenty on its
        # own -- the point is the guarantee holds unconditionally, not
        # just for the otherwise-empty-file cases.
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        write_aiform_md(project_dir / "app.aiform.md")
        state_file = project_dir / ".aiform" / "state.json"
        patch_client(monkeypatch, [approve_response(), categorization_response(action="create")])

        code = cli.main(["plan", "apply", "--yes", "--state-file", str(state_file)])
        capsys.readouterr()

        assert code == 0
        log_files = list((project_dir / ".aiform" / "logs").glob("*.log"))
        assert len(log_files) == 1
        lines = log_files[0].read_text().splitlines()
        assert "aiform.cli" in lines[0]
        assert "invoked: plan apply --yes --state-file" in lines[0]
        assert "aiform.cli" in lines[-1]
        assert "exit_code=0" in lines[-1]
        assert "outcome=success" in lines[-1]
        assert "INFO" in lines[-1]

    def test_log_file_exit_line_escalates_to_error_on_a_failed_exit_code(self, project_dir, capsys):
        state_file = project_dir / ".aiform" / "state.json"

        code = cli.main(
            ["plan", "create", "does-not-exist.aiform.md", "--state-file", str(state_file)]
        )
        capsys.readouterr()

        assert code == 2
        log_files = list((project_dir / ".aiform" / "logs").glob("*.log"))
        lines = log_files[0].read_text().splitlines()
        assert "exit_code=2" in lines[-1]
        assert "outcome=error" in lines[-1]
        assert lines[-1].split()[1] == "ERROR"

    def test_log_file_exit_line_reflects_an_error_exit_code(self, project_dir, capsys):
        state_file = project_dir / ".aiform" / "state.json"

        code = cli.main(
            ["plan", "create", "does-not-exist.aiform.md", "--state-file", str(state_file)]
        )
        capsys.readouterr()

        assert code == 2
        log_files = list((project_dir / ".aiform" / "logs").glob("*.log"))
        assert len(log_files) == 1
        lines = log_files[0].read_text().splitlines()
        assert "aiform.cli" in lines[0]
        assert "invoked: plan create does-not-exist.aiform.md" in lines[0]
        assert "aiform.cli" in lines[-1]
        assert "exit_code=2" in lines[-1]
        assert "outcome=error" in lines[-1]

    def test_apply_verbose_reports_call_count_even_when_blocked(
        self, project_dir, drivers_dir, prompts_dir, monkeypatch, capsys
    ):
        # The driver review + categorization calls already happened (real,
        # billable calls) before gate #2 blocks the apply -- --verbose must
        # still report that count on the error exit path, not just on
        # success.
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        aiform_md = project_dir / "app.aiform.md"
        write_aiform_md(aiform_md, params={"region": "sfo3", "size": "s-2vcpu-4gb"})
        state_file = project_dir / ".aiform" / "state.json"
        driver_hash = orchestrator.hashlib.sha256(
            (drivers_dir / "digitalocean" / "compute.py").read_bytes()
        ).hexdigest()
        entry = StateEntry(
            provider="digitalocean",
            resource_type="compute",
            name="telleztec-app-01",
            id="123",
            attributes={"region": "sfo3", "size": "s-1vcpu-2gb"},
            driver=make_driver_info(driver_hash),
            last_applied_at=datetime(2026, 7, 30, 18, 23, 5, tzinfo=UTC),
            last_refreshed_at=datetime(2026, 7, 31, 9, 10, 0, tzinfo=UTC),
            aiform_md_path=str(aiform_md),
            aiform_md_sha256="stale-hash-forces-categorization",
        )
        state.save(
            state.State(resources={"digitalocean.compute.telleztec-app-01": entry}), state_file
        )
        patch_client(
            monkeypatch,
            [
                categorization_response(action="update", likely_replace=True),
                plan_review_response(
                    safe_to_proceed=False,
                    flags=[
                        {
                            "resource_key": "digitalocean.compute.telleztec-app-01",
                            "concern": "do not resize production",
                            "severity": "block",
                        }
                    ],
                ),
            ],
        )

        code = cli.main(["plan", "apply", "--yes", "--state-file", str(state_file), "--verbose"])

        err = capsys.readouterr().err
        assert code == 2
        assert "Error:" in err
        assert "[verbose] 2 Anthropic API call(s) made" in err


class TestPlanDestroy:
    def test_destroy_all_tracked_moves_file_to_trash(
        self, project_dir, drivers_dir, prompts_dir, monkeypatch, capsys
    ):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        aiform_md = project_dir / "app.aiform.md"
        write_aiform_md(aiform_md)
        state_file = project_dir / ".aiform" / "state.json"
        driver_hash = orchestrator.hashlib.sha256(
            (drivers_dir / "digitalocean" / "compute.py").read_bytes()
        ).hexdigest()
        entry = StateEntry(
            provider="digitalocean",
            resource_type="compute",
            name="telleztec-app-01",
            id="123",
            attributes={"region": "sfo3", "size": "s-1vcpu-2gb"},
            driver=make_driver_info(driver_hash),
            last_applied_at=datetime(2026, 7, 30, 18, 23, 5, tzinfo=UTC),
            last_refreshed_at=datetime(2026, 7, 31, 9, 10, 0, tzinfo=UTC),
            aiform_md_path=str(aiform_md),
            aiform_md_sha256="abc123",
        )
        state.save(
            state.State(resources={"digitalocean.compute.telleztec-app-01": entry}), state_file
        )
        patch_client(monkeypatch, [plan_review_response()])

        code = cli.main(["plan", "destroy", "--yes", "--state-file", str(state_file)])

        out = capsys.readouterr().out
        assert code == 0
        assert "destroy" in out
        reloaded = state.load(state_file)
        assert reloaded.resources == {}
        assert not aiform_md.exists()
        trash_dir = project_dir / ".aiform" / "trash"
        assert any(trash_dir.iterdir())

    def test_destroy_blocked_by_gate2_exits_2(
        self, project_dir, drivers_dir, prompts_dir, monkeypatch, capsys
    ):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        aiform_md = project_dir / "app.aiform.md"
        write_aiform_md(aiform_md)
        state_file = project_dir / ".aiform" / "state.json"
        driver_hash = orchestrator.hashlib.sha256(
            (drivers_dir / "digitalocean" / "compute.py").read_bytes()
        ).hexdigest()
        entry = StateEntry(
            provider="digitalocean",
            resource_type="compute",
            name="telleztec-app-01",
            id="123",
            attributes={"region": "sfo3", "size": "s-1vcpu-2gb"},
            driver=make_driver_info(driver_hash),
            last_applied_at=datetime(2026, 7, 30, 18, 23, 5, tzinfo=UTC),
            last_refreshed_at=datetime(2026, 7, 31, 9, 10, 0, tzinfo=UTC),
            aiform_md_path=str(aiform_md),
            aiform_md_sha256="abc123",
        )
        state.save(
            state.State(resources={"digitalocean.compute.telleztec-app-01": entry}), state_file
        )
        patch_client(
            monkeypatch,
            [
                plan_review_response(
                    safe_to_proceed=False,
                    flags=[
                        {
                            "resource_key": "digitalocean.compute.telleztec-app-01",
                            "concern": "do not destroy production",
                            "severity": "block",
                        }
                    ],
                )
            ],
        )

        code = cli.main(["plan", "destroy", "--yes", "--state-file", str(state_file)])

        err = capsys.readouterr().err
        assert code == 2
        assert "Error:" in err
        assert "do not destroy production" in err
        reloaded = state.load(state_file)
        assert "digitalocean.compute.telleztec-app-01" in reloaded.resources
        assert aiform_md.exists()


class TestPlanRefresh:
    def test_refresh_updates_attributes(self, project_dir, drivers_dir, monkeypatch, capsys):
        monkeypatch.setenv("DIGITALOCEAN_TOKEN", "dop_v1_test")
        write_driver(drivers_dir, "digitalocean", "compute")
        state_file = project_dir / ".aiform" / "state.json"
        driver_hash = orchestrator.hashlib.sha256(
            (drivers_dir / "digitalocean" / "compute.py").read_bytes()
        ).hexdigest()
        entry = StateEntry(
            provider="digitalocean",
            resource_type="compute",
            name="telleztec-app-01",
            id="123",
            attributes={"region": "old-region", "size": "old-size"},
            driver=make_driver_info(driver_hash),
            last_applied_at=datetime(2026, 7, 30, 18, 23, 5, tzinfo=UTC),
            last_refreshed_at=datetime(2026, 7, 31, 9, 10, 0, tzinfo=UTC),
            aiform_md_path="app.aiform.md",
            aiform_md_sha256="abc123",
        )
        state.save(
            state.State(resources={"digitalocean.compute.telleztec-app-01": entry}), state_file
        )

        code = cli.main(["plan", "refresh", "--state-file", str(state_file)])

        out = capsys.readouterr().out
        assert code == 0
        assert "digitalocean.compute.telleztec-app-01" in out
        reloaded = state.load(state_file)
        assert reloaded.resources["digitalocean.compute.telleztec-app-01"].attributes == {
            "region": "sfo3",
            "size": "s-1vcpu-2gb",
        }


class TestPlanShow:
    def test_show_empty_state(self, project_dir, capsys):
        state_file = project_dir / ".aiform" / "state.json"
        code = cli.main(["plan", "show", "--state-file", str(state_file)])
        out = capsys.readouterr().out
        assert code == 0
        assert "no resources tracked" in out

    def test_show_prints_tracked_resource(self, project_dir, capsys):
        state_file = project_dir / ".aiform" / "state.json"
        entry = StateEntry(
            provider="digitalocean",
            resource_type="compute",
            name="telleztec-app-01",
            id="123456789",
            attributes={"region": "sfo3"},
            driver=make_driver_info(
                "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b8"
            ),
            last_applied_at=datetime(2026, 7, 30, 18, 23, 5, tzinfo=UTC),
            last_refreshed_at=datetime(2026, 7, 31, 9, 10, 0, tzinfo=UTC),
            aiform_md_path="examples/compute.aiform.md",
            aiform_md_sha256="abc123",
        )
        state.save(
            state.State(resources={"digitalocean.compute.telleztec-app-01": entry}), state_file
        )

        code = cli.main(["plan", "show", "--state-file", str(state_file)])

        out = capsys.readouterr().out
        assert code == 0
        assert "digitalocean.compute.telleztec-app-01" in out
        assert "123456789" in out

    def test_show_corrupt_state_file_exits_2_with_clean_error(self, project_dir, capsys):
        state_file = project_dir / ".aiform" / "state.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text("not valid json{{{")

        code = cli.main(["plan", "show", "--state-file", str(state_file)])

        err = capsys.readouterr().err
        assert code == 2
        assert "Error:" in err


class TestMainModuleEntryPoint:
    def test_python_dash_m_aiform_invokes_cli(self, tmp_path: Path):
        result = subprocess.run(
            [sys.executable, "-m", "aiform", "plan", "show"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        assert "no resources tracked" in result.stdout
