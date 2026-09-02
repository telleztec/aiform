# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

import argparse
import http.client
import json
import logging
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

import anthropic

from aiform import config, llm, log, orchestrator, state
from aiform.exceptions import DriverExecutionError, PlanBlockedError
from aiform.models import KeyCheck, KeyState, PlanAction

logger = logging.getLogger(__name__)

_GITIGNORE_ENTRIES = [
    ".aiform/credentials.env",
    ".aiform/state.json",
    ".aiform/state.json.backup",
    ".aiform/logs/",
    ".env",
    "__pycache__/",
    "*.pyc",
]

_EXAMPLE_COMPUTE_AIFORM_MD = """\
---
resource: compute
name: telleztec-app-01
provider: digitalocean
params:
  region: sfo3
  size: s-1vcpu-2gb
  image: ubuntu-24-04-x64
  # DigitalOcean takes SSH key fingerprints or numeric IDs, never names.
  # List yours with: doctl compute ssh-key list
  ssh_keys:
    - "aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99"
  backups: false
  monitoring: true
  tags:
    - aiform
---

## Intent

This droplet runs the primary application server.
"""

_MARKERS = {
    PlanAction.CREATE: "+",
    PlanAction.UPDATE: "~",
    PlanAction.DESTROY: "-",
    PlanAction.NO_OP: "=",
}

_COLOR_CODES = {
    PlanAction.CREATE: "\033[32m",
    PlanAction.UPDATE: "\033[33m",
    PlanAction.DESTROY: "\033[31m",
    PlanAction.NO_OP: "\033[2m",
}
_RESET = "\033[0m"

_HANDLED_EXCEPTIONS = (
    PlanBlockedError,
    DriverExecutionError,
    ValueError,
    FileNotFoundError,
    RuntimeError,
)


class _CountingMessages:
    """Duck-typed like anthropic.Anthropic().messages -- the only shape
    llm._anthropic_call() actually calls. Defers constructing the real
    client until the first call actually happens, so a run that makes
    zero Anthropic API calls never requires ANTHROPIC_API_KEY to be set,
    matching llm._anthropic_call()'s own laziness."""

    def __init__(self, parent: "_CountingClient"):
        self._parent = parent

    def create(self, **kwargs):
        self._parent.call_count += 1
        if self._parent._real is None:
            # Through llm.build_client, not anthropic.Anthropic() -- this is
            # the client every plan/apply actually calls through, so it is
            # where the redirect refusal has to hold (#101).
            self._parent._real = llm.build_client()
        return self._parent._real.messages.create(**kwargs)


class _CountingClient:
    def __init__(self):
        self._real: anthropic.Anthropic | None = None
        self.call_count = 0
        self.messages = _CountingMessages(self)

    def close(self) -> None:
        # Never builds one just to close it: a zero-call run must stay a
        # run that constructed no client at all.
        if self._real is not None:
            self._real.close()
            self._real = None


def _confirm(prompt: str) -> bool:
    if not sys.stdin.isatty():
        raise RuntimeError(
            f"cannot prompt for confirmation ({prompt!r}): stdin is not a TTY -- "
            "pass --yes for non-interactive runs (a --yes run can still hit an "
            "unplanned confirmation it cannot skip -- see PLAN.md's replace-review rule)"
        )
    return input(f"{prompt} [y/N]: ").strip().lower() == "y"


def _format_error(exc: Exception) -> str:
    if isinstance(exc, PlanBlockedError):
        return exc.reason
    return str(exc)


def _resolve_paths(files: list[str]) -> list[Path] | None:
    return [Path(f) for f in files] if files else None


# _action_label (plan output, below) and _print_apply_result's inline
# equivalent (apply-result output) intentionally use different wording
# for the same underlying likely_replace flag, not duplicated by
# oversight: this one describes a plan-time *prediction* ("likely
# replace" -- may not happen), the other an apply-time *actual outcome*
# ("replaced" -- did happen, per orchestrator.apply_plan's own
# correction of this flag to reflect reality). Unifying them into one
# helper would blur that distinction rather than remove real duplication.
def _action_label(action: PlanAction, *, likely_replace: bool) -> str:
    label = action.value
    if action == PlanAction.UPDATE and likely_replace:
        label += " (likely replace)"
    return label


def _colorize(text: str, action: PlanAction, *, color: bool) -> str:
    if not color:
        return text
    return f"{_COLOR_CODES[action]}{text}{_RESET}"


def _print_plan(
    planned: list[orchestrator.PlannedResource], warnings: list[str], *, color: bool
) -> None:
    counts = dict.fromkeys(PlanAction, 0)
    for pr in planned:
        counts[pr.entry.action] += 1
        marker = _MARKERS[pr.entry.action]
        label = _action_label(pr.entry.action, likely_replace=pr.entry.likely_replace)
        line = _colorize(f"{marker} {pr.entry.resource_key}: {label}", pr.entry.action, color=color)
        print(line)
        print(f"    {pr.entry.rationale}")
    print(
        f"Plan: {counts[PlanAction.CREATE]} to create, {counts[PlanAction.UPDATE]} to update, "
        f"{counts[PlanAction.DESTROY]} to destroy, {counts[PlanAction.NO_OP]} no-op."
    )
    for warning in warnings:
        print(f"Warning: {warning}")


def _plan_to_json(planned: list[orchestrator.PlannedResource], warnings: list[str]) -> dict:
    return {
        "plan": [
            {
                "resource_key": pr.entry.resource_key,
                "action": pr.entry.action.value,
                "rationale": pr.entry.rationale,
                "likely_replace": pr.entry.likely_replace,
            }
            for pr in planned
        ],
        "warnings": list(warnings),
    }


def _print_apply_result(result: orchestrator.ApplyResult) -> None:
    for entry in result.executed:
        label = (
            "update (replaced)"
            if entry.action == PlanAction.UPDATE and entry.likely_replace
            else entry.action.value
        )
        print(f"{entry.resource_key}: {label}")
    for flag in result.review_flags:
        print(f"{flag.resource_key}: {flag.concern} [{flag.severity.value}]")
    if result.aborted:
        print("Apply aborted.")


def _print_state(st: state.State) -> None:
    if not st.resources:
        print("no resources tracked")
        return
    for key, entry in st.resources.items():
        print(key)
        print(f"    id: {entry.id}")
        print(f"    attributes: {json.dumps(entry.attributes, indent=2)}")
        print(f"    driver: {entry.driver.path} ({entry.driver.sha256[:12]})")
        print(f"    last_applied_at: {entry.last_applied_at}")
        print(f"    last_refreshed_at: {entry.last_refreshed_at}")
        print(f"    aiform_md_path: {entry.aiform_md_path}")


def _report_verbose_calls(args: argparse.Namespace, client: _CountingClient) -> None:
    if args.verbose:
        print(f"[verbose] {client.call_count} Anthropic API call(s) made", file=sys.stderr)


def _cmd_init(args: argparse.Namespace) -> int:
    provider = args.provider
    if provider not in config.PROVIDER_TOKEN_ENV_VARS:
        print(
            f"Error: unsupported provider {provider!r}; supported: "
            f"{sorted(config.PROVIDER_TOKEN_ENV_VARS)}",
            file=sys.stderr,
        )
        return 2

    Path(".aiform").mkdir(parents=True, exist_ok=True)

    gitignore_path = Path(".gitignore")
    existing_lines = (
        gitignore_path.read_text(encoding="utf-8").splitlines() if gitignore_path.exists() else []
    )
    new_lines = [line for line in _GITIGNORE_ENTRIES if line not in existing_lines]
    if new_lines:
        with gitignore_path.open("a", encoding="utf-8") as f:
            for line in new_lines:
                f.write(line + "\n")

    examples_dir = Path("examples")
    examples_dir.mkdir(parents=True, exist_ok=True)
    example_path = examples_dir / "compute.aiform.md"
    example_written = not example_path.exists()
    if example_written:
        example_path.write_text(_EXAMPLE_COMPUTE_AIFORM_MD, encoding="utf-8")

    token_env_var = config.PROVIDER_TOKEN_ENV_VARS[provider]
    print(f"Initialized aiform in {Path.cwd()}")
    if example_written:
        print(f"Wrote {example_path}")
    else:
        print(f"Kept your existing {example_path} (it may predate the current template)")
    print()
    print("Set the following before running `aiform plan create`:")
    print("  - ANTHROPIC_API_KEY: environment variable only, never a CLI flag or file")
    print(
        f"  - {token_env_var}: environment variable, or create {config.DEFAULT_CREDENTIALS_PATH} "
        f"by hand with a line '{token_env_var}=...' -- aiform never scaffolds this file with a "
        "value or prompts for one interactively"
    )
    print()

    _print_credential_checks(provider, token_env_var)

    # The scaffold is deliberately somewhere discover_files() cannot see
    # (specs/cli.md): init must never produce config that would make a
    # first `plan create` propose a real droplet. That makes this
    # instruction the only thing connecting the example to a usable plan.
    print()
    print("Next: copy an example here and edit it --")
    print(f"  cp {example_path} ./my-server.aiform.md")
    print()
    print("aiform discovers *.aiform.md in the current directory only.")
    return 0


# urllib's timeout bounds each socket operation, not the total transfer, so
# an endpoint that streams steadily would otherwise be read forever.
_MAX_PROBE_BODY = 65536

# Three probes run in sequence, so this is the per-request bound, not the
# worst case a user waits through.
_PROBE_TIMEOUT = 5.0

_STATE_MARKERS = {
    KeyState.OK: "✓",
    KeyState.MISSING: "✗",
    KeyState.REJECTED: "✗",
    KeyState.UNVERIFIED: "?",
}

_STATE_SUMMARIES = {
    KeyState.MISSING: "not set",
    KeyState.REJECTED: "set, but rejected",
    KeyState.UNVERIFIED: "set, but could not verify",
}


def _format_check(label: str, check: KeyCheck) -> str:
    line = f"  [{_STATE_MARKERS[check.state]}] {label}"
    summary = _STATE_SUMMARIES.get(check.state)
    if summary:
        line += f" -- {summary}"
    if check.detail:
        line += f": {check.detail}"
    return line


def _print_credential_checks(provider: str, token_env_var: str) -> None:
    # Up to three sequential probes run below; without this the command
    # looks hung on a black-holed network.
    print("Checking credentials...", flush=True)
    print(
        _format_check(
            "ANTHROPIC_API_KEY", _guarded(lambda: llm.verify_api_key(timeout=_PROBE_TIMEOUT))
        )
    )
    print(_format_check(token_env_var, _guarded(lambda: _check_provider_token(provider))))


def _guarded(probe: Callable[[], KeyCheck]) -> KeyCheck:
    """A preflight that crashes is worse than one that reports nothing:
    `init`'s job is to scaffold and inform, and the scaffold already
    happened by the time these run."""
    try:
        return probe()
    except Exception as exc:  # noqa: BLE001 -- see docstring
        return KeyCheck(state=KeyState.UNVERIFIED, detail=str(exc) or type(exc).__name__)


def _check_provider_token(provider: str, *, timeout: float = _PROBE_TIMEOUT) -> KeyCheck:
    """Probe the provider's free account endpoint (GET /v2/account for
    DigitalOcean) so a token that is present but wrong -- the other
    account's, or revoked -- is not reported as working."""
    try:
        creds = config.resolve_credentials(provider)
    except RuntimeError:
        return KeyCheck(state=KeyState.MISSING)

    probe_url = config.PROVIDER_ACCOUNT_PROBES.get(provider)
    if probe_url is None:
        return KeyCheck(state=KeyState.UNVERIFIED, detail="no account probe for this provider")

    token = creds[config.PROVIDER_TOKEN_ENV_VARS[provider]]
    account = _probe_account(probe_url, token, timeout)
    if account.check is not None:
        return account.check

    # Always, not only after a 403 on the account endpoint: a token granted
    # account:read without droplet scopes answers 200 above and then fails
    # every apply, which is the same false green reached by the other path.
    return _check_droplet_scope(provider, token, timeout, account)


class _AccountResult(NamedTuple):
    """What the account probe established.

    `authenticated` is tracked separately from `email` because a 200 whose
    body carries no email still proves the token works, and must not be
    mistaken for the 403 case that proves nothing."""

    authenticated: bool
    email: str | None
    check: KeyCheck | None


def _probe_account(url: str, token: str, timeout: float) -> _AccountResult:
    body, failure = _probe(url, token, timeout)
    if failure is not None:
        # A 403 ends nothing -- it means only that this token lacks
        # account:read, which DigitalOcean's scoped tokens routinely do.
        if failure.state is KeyState.REJECTED and failure.code == 403:
            return _AccountResult(authenticated=False, email=None, check=None)
        return _AccountResult(authenticated=False, email=None, check=failure.check)

    # A 200 that is not shaped like an account response did not come from the
    # provider (a proxy or captive portal answering anything), so it is not
    # evidence about the token.
    account = body.get("account") if isinstance(body, dict) else None
    if not isinstance(account, dict):
        return _AccountResult(
            authenticated=False,
            email=None,
            check=KeyCheck(
                state=KeyState.UNVERIFIED, detail="unexpected response from the provider"
            ),
        )
    # Which of several provider accounts a token belongs to is the failure
    # this probe is most likely to catch.
    return _AccountResult(authenticated=True, email=account.get("email"), check=None)


def _check_droplet_scope(
    provider: str, token: str, timeout: float, account: _AccountResult
) -> KeyCheck:
    """Confirm the token can read droplets.

    Read scope only -- it does not prove the token can create or destroy
    one, which no free probe can establish."""
    url = config.PROVIDER_DROPLET_PROBES.get(provider)
    if url is None:
        # A forbidden account probe proved nothing, and with no scope probe
        # to fall back on there is nothing left to call a pass.
        if not account.authenticated:
            return KeyCheck(state=KeyState.UNVERIFIED, detail="no scope probe for this provider")
        return KeyCheck(state=KeyState.OK, detail=account.email)

    body, failure = _probe(url, token, timeout)
    if failure is not None:
        if failure.code == 403:
            # Only claim the token is valid when the account probe actually
            # established that; on the 403/403 path nothing did, and telling
            # the user to add droplet scope points at the wrong fix.
            detail = (
                "token is valid but cannot read droplets"
                if account.authenticated
                else "token cannot read the account or droplets"
            )
            return KeyCheck(state=KeyState.REJECTED, detail=detail)
        # A 401 here is unambiguous -- the token was not accepted at all,
        # whatever the account endpoint said a moment ago -- so it outranks
        # anything the first probe established.
        if failure.code in config.PROVIDER_TOKEN_VERDICT_STATUSES:
            return failure.check
        # A redirect on a token-bearing request is the one signal
        # _RejectRedirects exists to distrust; it is not "inconclusive".
        if failure.code is not None and 300 <= failure.code < 400:
            return failure.check
        # Anything else is inconclusive, and must not discard what the first
        # probe already proved.
        if account.authenticated:
            return KeyCheck(state=KeyState.OK, detail=_unverified_scope_detail(account))
        return failure.check

    if not isinstance(body, dict) or "droplets" not in body:
        # Same rule as the failure branch above: a malformed second response
        # is not evidence against a token the first response authenticated.
        if account.authenticated:
            return KeyCheck(state=KeyState.OK, detail=_unverified_scope_detail(account))
        return KeyCheck(state=KeyState.UNVERIFIED, detail="unexpected response from the provider")

    if account.email:
        return KeyCheck(state=KeyState.OK, detail=account.email)
    # "scoped token" is specifically the token that could not read the
    # account. One that read it and simply carried no email is not that.
    if account.authenticated:
        return KeyCheck(state=KeyState.OK, detail="authenticated")
    return KeyCheck(state=KeyState.OK, detail="authenticated (scoped token)")


def _unverified_scope_detail(account: _AccountResult) -> str:
    return f"{account.email or 'authenticated'} (droplet scope unverified)"


class _ProbeFailure(NamedTuple):
    check: KeyCheck
    code: int | None

    @property
    def state(self) -> KeyState:
        return self.check.state


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    """urllib re-sends the Authorization header verbatim to a redirect
    target, including a cross-host one -- requests and httpx both drop it.
    These probes carry a provider token, so a redirect is refused rather
    than followed.

    The 3xx is raised here rather than left to the base class, which parses
    the Location header (urlparse) before it ever consults redirect_request:
    an unparseable one -- `Location: http://[::1` from a captive portal --
    raises ValueError out of the probe, about a URL already decided against
    following. Nothing here reads the Location at all.
    """

    def http_error_302(self, req, fp, code, msg, headers):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_PROBE_OPENER = urllib.request.build_opener(_RejectRedirects)


def _open(request: urllib.request.Request, timeout: float):
    return _PROBE_OPENER.open(request, timeout=timeout)


def _probe(url: str, token: str, timeout: float) -> tuple[Any, _ProbeFailure | None]:
    def failed(state: KeyState, detail: str, code: int | None = None) -> tuple[None, _ProbeFailure]:
        # Every detail leaving this function passes through here. An
        # exception raised while sending carries the request -- including the
        # Authorization header -- in its message, and CLAUDE.md's rule is
        # that the token never reaches the output of any command.
        return None, _ProbeFailure(KeyCheck(state=state, detail=_redact(detail, token)), code)

    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with _open(request, timeout) as response:
            raw = response.read(_MAX_PROBE_BODY)
    except urllib.error.HTTPError as exc:
        detail = _http_error_detail(exc)
        # A redirect is not a verdict on the token -- and following it would
        # have leaked the token to wherever it pointed.
        if 300 <= exc.code < 400:
            return failed(KeyState.UNVERIFIED, f"unexpected redirect (HTTP {exc.code})", exc.code)
        if exc.code in config.PROVIDER_TOKEN_VERDICT_STATUSES:
            return failed(KeyState.REJECTED, detail, exc.code)
        return failed(KeyState.UNVERIFIED, detail, exc.code)
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        # HTTPException (IncompleteRead, BadStatusLine) is not an OSError, so
        # a truncated chunked body would otherwise escape a function
        # specified to return a KeyCheck.
        return failed(KeyState.UNVERIFIED, str(exc) or type(exc).__name__)
    except ValueError:
        # http.client raises this for a malformed header value -- a token with
        # a stray newline is the usual cause -- and quotes the whole header in
        # the message, so it must never be echoed. Other handlers in the
        # opener raise ValueError too (a malformed https_proxy, say), and with
        # the text unusable the only safe way to tell them apart is to ask
        # whether the token could have been the cause.
        if _cannot_be_a_header_value(token):
            return failed(
                KeyState.UNVERIFIED,
                "the token is not a valid HTTP header value (check for stray whitespace)",
            )
        # Names the variables, never their values: _parse_proxy quotes the
        # proxy URL in its own message, and that URL can carry credentials.
        return failed(
            KeyState.UNVERIFIED,
            "the request could not be sent (check any https_proxy setting)",
        )

    try:
        return json.loads(raw.decode("utf-8")), None
    except ValueError as exc:
        # A captive portal or proxy answering 200 with non-JSON. Decoding
        # happens after the request, so this message cannot carry the token.
        return failed(KeyState.UNVERIFIED, f"unreadable response: {exc}")


def _cannot_be_a_header_value(token: str) -> bool:
    # Deliberately wider than http.client's rule, which permits a newline that
    # continues a folded line: this only chooses between two canned messages
    # once a ValueError has already been raised, so erring wide costs nothing
    # and keeps a private regex out of this file.
    value = f"Bearer {token}"
    if "\r" in value or "\n" in value:
        return True
    try:
        value.encode("latin-1")
    except UnicodeEncodeError:
        # putheader encodes latin-1 first, and UnicodeEncodeError is a
        # ValueError -- so this lands in the same branch.
        return True
    return False


def _redact(detail: str, token: str) -> str:
    return detail.replace(token.strip(), "***") if token.strip() else detail


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        body = json.loads(exc.read(_MAX_PROBE_BODY).decode("utf-8"))
    except (ValueError, OSError):
        return f"HTTP {exc.code}"
    message = body.get("message") if isinstance(body, dict) else None
    return f"HTTP {exc.code}: {message}" if message else f"HTTP {exc.code}"


def _cmd_plan_create(args: argparse.Namespace, client: _CountingClient) -> int:
    planned, warnings = orchestrator.build_create_plan(
        _resolve_paths(args.files), state_path=args.state_file, client=client
    )
    if args.json:
        print(json.dumps(_plan_to_json(planned, warnings), indent=2))
    else:
        _print_plan(planned, warnings, color=not args.no_color)
    return 0


def _plan_apply_and_report(
    args: argparse.Namespace,
    client: _CountingClient,
    planned: list[orchestrator.PlannedResource],
    warnings: list[str],
) -> int:
    _print_plan(planned, warnings, color=not args.no_color)
    result = orchestrator.apply_plan(
        planned, state_path=args.state_file, yes=args.yes, confirm=_confirm, client=client
    )
    _print_apply_result(result)
    return 1 if result.aborted else 0


def _cmd_plan_apply(args: argparse.Namespace, client: _CountingClient) -> int:
    planned, warnings = orchestrator.build_create_plan(
        _resolve_paths(args.files), state_path=args.state_file, client=client
    )
    return _plan_apply_and_report(args, client, planned, warnings)


def _cmd_plan_destroy(args: argparse.Namespace, client: _CountingClient) -> int:
    planned = orchestrator.build_destroy_plan(
        _resolve_paths(args.files), state_path=args.state_file
    )
    return _plan_apply_and_report(args, client, planned, [])


def _cmd_plan_refresh(args: argparse.Namespace) -> int:
    st = orchestrator.refresh_state(state_path=args.state_file)
    _print_state(st)
    return 0


def _cmd_plan_show(args: argparse.Namespace) -> int:
    st = state.load(args.state_file)
    _print_state(st)
    return 0


# create/apply/destroy may call an LLM (via the shared client, so --verbose
# reports one running total per invocation); refresh/show never do
# (PLAN.md §7: refresh is "no LLM calls at all", show is a direct
# state.load()) and so take no client at all.
_LLM_PLAN_DISPATCH = {
    "create": _cmd_plan_create,
    "apply": _cmd_plan_apply,
    "destroy": _cmd_plan_destroy,
}
_PLAIN_PLAN_DISPATCH = {
    "refresh": _cmd_plan_refresh,
    "show": _cmd_plan_show,
}


def _build_parser() -> argparse.ArgumentParser:
    global_parent = argparse.ArgumentParser(add_help=False)
    global_parent.add_argument("-v", "--verbose", action="store_true")
    global_parent.add_argument("--no-color", action="store_true")

    state_parent = argparse.ArgumentParser(add_help=False)
    state_parent.add_argument("--state-file", type=Path, default=state.DEFAULT_STATE_PATH)

    parser = argparse.ArgumentParser(prog="aiform", parents=[global_parent])
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", parents=[global_parent])
    init_parser.add_argument("--provider", default="digitalocean")

    plan_parser = subparsers.add_parser("plan", parents=[global_parent])
    plan_sub = plan_parser.add_subparsers(dest="plan_command", required=True)

    create_parser = plan_sub.add_parser("create", parents=[global_parent, state_parent])
    create_parser.add_argument("files", nargs="*")
    create_parser.add_argument("--json", action="store_true")

    apply_parser = plan_sub.add_parser("apply", parents=[global_parent, state_parent])
    apply_parser.add_argument("files", nargs="*")
    apply_parser.add_argument("--yes", action="store_true")

    destroy_parser = plan_sub.add_parser("destroy", parents=[global_parent, state_parent])
    destroy_parser.add_argument("files", nargs="*")
    destroy_parser.add_argument("--yes", action="store_true")

    plan_sub.add_parser("refresh", parents=[global_parent, state_parent])
    plan_sub.add_parser("show", parents=[global_parent, state_parent])

    return parser


def _dispatch(args: argparse.Namespace) -> int:
    try:
        if args.command == "init":
            return _cmd_init(args)
        if args.plan_command in _LLM_PLAN_DISPATCH:
            client = _CountingClient()
            try:
                return _LLM_PLAN_DISPATCH[args.plan_command](args, client)
            finally:
                # In a `finally` so a real, billable call count is still
                # reported even when the command goes on to raise (e.g. a
                # gate #2 PlanBlockedError after the driver-review/
                # categorization calls already happened) -- the case where
                # a user most wants to know what was actually spent. The
                # close belongs here for its own reason: llm.build_client
                # passes http_client=, which costs the SDK wrapper's
                # __del__, so the pool is this function's to release.
                _report_verbose_calls(args, client)
                client.close()
        return _PLAIN_PLAN_DISPATCH[args.plan_command](args)
    except _HANDLED_EXCEPTIONS as exc:
        message = _format_error(exc)
        logger.error(message, extra={"exception_type": type(exc).__name__})
        print(f"Error: {message}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        logging_config = config.resolve_logging_config()
    except _HANDLED_EXCEPTIONS as exc:
        # log.configure() hasn't run yet -- the very config that would
        # tell it what to do is what failed to resolve -- so this one
        # narrow failure mode can't produce a log file. It can still
        # exit the same clean way every other _HANDLED_EXCEPTIONS case
        # does, instead of an uncaught traceback.
        message = _format_error(exc)
        print(f"Error: {message}", file=sys.stderr)
        return 2
    log.configure(verbose=args.verbose, logging_config=logging_config)
    logger.info("invoked: %s", " ".join(argv if argv is not None else sys.argv[1:]))

    code = _dispatch(args)
    level = logging.INFO if code == 0 else logging.ERROR
    logger.log(level, "", extra={"exit_code": code, "outcome": "success" if code == 0 else "error"})
    return code
