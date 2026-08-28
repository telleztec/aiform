# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

import argparse
import json
import logging
import os
import sys
from pathlib import Path

import anthropic

from aiform import config, log, orchestrator, state
from aiform.exceptions import DriverExecutionError, PlanBlockedError
from aiform.models import PlanAction

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
  ssh_keys:
    - "your-ssh-key-name"
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
            self._parent._real = anthropic.Anthropic()
        return self._parent._real.messages.create(**kwargs)


class _CountingClient:
    def __init__(self):
        self._real: anthropic.Anthropic | None = None
        self.call_count = 0
        self.messages = _CountingMessages(self)


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
    if not example_path.exists():
        example_path.write_text(_EXAMPLE_COMPUTE_AIFORM_MD, encoding="utf-8")

    token_env_var = config.PROVIDER_TOKEN_ENV_VARS[provider]
    print(f"Initialized aiform in {Path.cwd()}")
    print()
    print("Set the following before running `aiform plan create`:")
    print("  - ANTHROPIC_API_KEY: environment variable only, never a CLI flag or file")
    print(
        f"  - {token_env_var}: environment variable, or create {config.DEFAULT_CREDENTIALS_PATH} "
        f"by hand with a line '{token_env_var}=...' -- aiform never scaffolds this file with a "
        "value or prompts for one interactively"
    )
    print()

    anthropic_ok = bool(os.environ.get("ANTHROPIC_API_KEY"))
    try:
        config.resolve_credentials(provider)
        provider_ok = True
    except RuntimeError:
        provider_ok = False
    print(f"  [{'✓' if anthropic_ok else '✗'}] ANTHROPIC_API_KEY")
    print(f"  [{'✓' if provider_ok else '✗'}] {token_env_var}")
    return 0


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
                # a user most wants to know what was actually spent.
                _report_verbose_calls(args, client)
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
