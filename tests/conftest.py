# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

import logging
import os
from pathlib import Path

import pytest

from aiform import llm
from aiform import log as aiform_log

_SECRET_ENV_VARS = ("DIGITALOCEAN_TOKEN", "ANTHROPIC_API_KEY")

# Snapshotted at import, before any test can monkeypatch these -- a test
# that sets a fake value over one of these vars (e.g. a bad-token system
# test) must not blind this check to the *real* value for its duration.
# Mirrors tests/system/conftest.py's identical _SECRETS snapshot.
_SECRETS = {var: os.environ.get(var) for var in _SECRET_ENV_VARS}


def find_leaked_credential(secrets: dict[str, str | None], haystacks: list[str]) -> str | None:
    for var, value in secrets.items():
        if value and any(value in haystack for haystack in haystacks):
            return var
    return None


def _log_file_haystacks() -> list[str]:
    # aiform.log._installed_file_handler's baseFilename is resolved to an
    # absolute path by the stdlib FileHandler at configure()-call time --
    # deliberately not a Path(".aiform/logs") glob relative to cwd here,
    # since this fixture (autouse, no deps) is torn down *after*
    # tests/system/conftest.py's project_dir fixture, whose chdir has
    # already been reverted by the time this code runs. See
    # specs/conftest.md.
    #
    # Consumes (resets to None) the handler it reads, mirroring
    # capsys.readouterr()'s own drain-on-read behavior: _reset_aiform_logger
    # clears the *logger's* handler list every test but never touches this
    # module-level global, so without this reset a test that never calls
    # configure() would still have this function re-read -- and
    # potentially misattribute a leak to -- the previous test's already-
    # checked log directory.
    file_handler = aiform_log._installed_file_handler
    if file_handler is None:
        return []
    aiform_log._installed_file_handler = None
    log_dir = Path(file_handler.baseFilename).parent
    return [path.read_text(encoding="utf-8", errors="replace") for path in log_dir.glob("*.log")]


@pytest.fixture(autouse=True)
def _scan_for_leaked_credentials(capsys):
    yield
    captured = capsys.readouterr()
    haystacks = [captured.out, captured.err, *_log_file_haystacks()]
    leaked = find_leaked_credential(_SECRETS, haystacks)
    assert leaked is None, f"{leaked} value leaked into test output or .aiform/logs/*.log"


@pytest.fixture
def forbid_llm_client(monkeypatch):
    """Make any attempt to construct an LLM client fail loudly.

    Several tests assert a code path makes zero LLM calls by simply not
    passing a client, on the stated rationale that a real
    anthropic.Anthropic() would "blow up on a missing API key". That
    rationale is false, and every test resting on it was silently
    unsound: anthropic constructs a keyless client without complaint
    (verified on 0.120.2), the autouse credential scan below only
    *reads* ANTHROPIC_API_KEY to check for leaks rather than unsetting
    it, and .envrc exports a real key into every pytest process here.
    So on a regression those tests issued a real, billed API call --
    and then usually still passed, because the model's answer to an
    empty or no-op diff is "no-op". A green test that costs money and
    proves nothing.

    Patching llm.build_client covers every path: _anthropic_call()
    resolves it as a module global at call time, and it is the only
    construction site outside llm.py itself.
    """

    def _forbidden(*args, **kwargs):
        raise AssertionError("an LLM client was constructed on a path asserted to make zero calls")

    monkeypatch.setattr(llm, "build_client", _forbidden)


@pytest.fixture(autouse=True)
def _reset_aiform_logger():
    yield
    logger = logging.getLogger("aiform")
    # close(), not just clear() -- a FileHandler holds a real OS file
    # descriptor open until closed; leaving hundreds of them open across
    # a full test run (one FileHandler per configure() call) risks
    # exhausting the process's fd limit and can block tmp_path cleanup
    # on platforms that refuse to delete a file still held open.
    for handler in logger.handlers:
        handler.close()
    logger.handlers.clear()
    logger.setLevel(logging.NOTSET)
    logger.propagate = True
