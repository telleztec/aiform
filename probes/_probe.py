# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

"""Shared machinery for an API probe session.

An *API probe* asks what a provider endpoint actually does. It is not
`aiform init`'s credential probe (aiform/config.py's
PROVIDER_ACCOUNT_PROBES, cli.py's _probe), which asks only whether a
token works at all -- the word is reused deliberately, since both are
"send one request and find out", but they answer different questions.

The session records every probe call as a JSON transcript. Those transcripts
are the point: they become the payloads a driver's unit tests mock with,
so a fixture cannot encode a belief nobody checked.
"""

import argparse
import datetime
import json
import os
import re
import secrets
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

BASE_URL = "https://api.digitalocean.com/v2"
REQUEST_TIMEOUT_SECONDS = 30
TOKEN_ENV_VAR = "DIGITALOCEAN_TOKEN"
# Mirrors aiform/config.py's PROVIDER_TOKEN_ENV_VARS rather than importing
# it: a probe must exercise the same auth path a driver does, but it is
# ops tooling that has to run from a bare checkout, so it does not depend
# on the package under study being importable.

REDACTED = f"Bearer <{TOKEN_ENV_VAR}>"
_SENSITIVE_HEADERS = frozenset({"authorization", "cookie", "set-cookie"})
_SLUG_RE = re.compile(r"[^a-z0-9]+")

TRANSCRIPTS_ROOT = Path(__file__).parent / "transcripts"
# A session never legitimately runs for more than a couple of minutes, so
# an hour cannot race a healthy concurrent run. Same value and same
# reasoning as specs/system_test.md's droplet sweep.
SWEEP_MIN_AGE_MINUTES = 60


class ProbeError(RuntimeError):
    """A probe could not run -- missing credentials, or a refused redirect."""


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect on a token-bearing request.

    urllib re-sends the Authorization header to a redirect target,
    including a cross-host one, so a redirect is a credential leak.
    aiform has fixed this exact class of bug twice (#97, #101); a new
    token-bearing tool does not get to reintroduce it.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProbeError(f"refused a {code} redirect to {newurl!r} on a token-bearing request")


_OPENER = urllib.request.build_opener(_RejectRedirects)


def slugify(note: str) -> str:
    return _SLUG_RE.sub("-", note.lower()).strip("-")[:60]


def unique_name(base: str) -> str:
    """A name whose age is readable from the name alone.

    Same shape as tests/system/conftest.py's unique_name, deliberately
    reimplemented rather than imported: specs/system_test.md requires a
    cleanup backstop not to depend on the code under test, and scripts/
    importing tests/ would be backwards.
    """
    now = datetime.datetime.now(datetime.UTC)
    return f"{base}-{now:%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"


class Recorded:
    def __init__(self, status: int, body: Any, matched: bool | None):
        self.status = status
        self.body = body
        self.matched = matched

    def __repr__(self) -> str:
        return f"<Recorded status={self.status} matched={self.matched}>"


class Probe:
    def __init__(self, session: str, *, mutate: bool = False, dry_run: bool = False):
        self.session = session
        self.mutate = mutate
        self.dry_run = dry_run
        self.dir = TRANSCRIPTS_ROOT / session
        self._seq = 0
        self._cleanups: list[tuple[str, str]] = []
        self.contradictions: list[str] = []
        self._token = os.environ.get(TOKEN_ENV_VAR, "")
        if not self._token and not dry_run:
            raise ProbeError(
                f"{TOKEN_ENV_VAR} is not set; export it (never on a command line) or pass --dry-run"
            )

    def __enter__(self) -> "Probe":
        if not self.dry_run:
            self.dir.mkdir(parents=True, exist_ok=True)
        return self

    def __exit__(self, *exc_info) -> None:
        # Registered teardown runs whatever happened above, so a probe
        # that raises mid-session still cannot leak a live resource.
        # --dry-run created nothing, so it must delete nothing: without
        # this guard a dry run would send real DELETEs for the
        # placeholder ids it never created.
        if self.dry_run:
            return
        for method, path in reversed(self._cleanups):
            try:
                status, _ = self._send(method, path, None)
                print(f"  cleanup {method} {path} -> {status}")
            except Exception as exc:  # noqa: BLE001 - teardown must not mask the real error
                print(f"  cleanup {method} {path} FAILED: {exc}")

    def cleanup(self, method: str, path: str) -> None:
        # Only reachable after a successful mutating call, but asserted
        # rather than merely true-in-practice: this is the one path that
        # sends a request without going through call()'s --mutate guard.
        assert self.mutate or self.dry_run, "cleanup() registered without --mutate"
        self._cleanups.append((method, path))

    def _send(self, method: str, path: str, body: Any):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Bearer {self._token}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(BASE_URL + path, data=data, headers=headers, method=method)
        try:
            with _OPENER.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                raw = response.read()
                return response.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                return exc.code, raw[:400].decode("utf-8", "replace")

    def call(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        note: str,
        predict: dict[str, Any] | None = None,
        record: bool = True,
    ) -> Recorded:
        """Send one request and record it.

        `predict` is written into the transcript alongside the result, so
        the artifact records both what was believed and what was true.
        That is the durable form of the finding -- a prediction can be
        refuted; a confidence label cannot.
        """
        self._seq += 1
        seq = self._seq
        if method not in ("GET", "HEAD") and not self.mutate and not self.dry_run:
            raise ProbeError(f"{method} {path} needs --mutate (read-only by default)")
        if self.dry_run:
            print(f"[{seq:02d}] DRY-RUN {method} {path} -- {note}")
            return Recorded(0, None, None)

        started = datetime.datetime.now(datetime.UTC)
        status, response_body = self._send(method, path, body)
        elapsed_ms = int((datetime.datetime.now(datetime.UTC) - started).total_seconds() * 1000)

        matched = None
        if predict and "status" in predict:
            matched = predict["status"] == status
            if not matched:
                self.contradictions.append(f"{note}: predicted {predict['status']}, got {status}")

        flag = "" if matched is None else ("  [as predicted]" if matched else "  [CONTRADICTED]")
        print(f"[{seq:02d}] {method} {path} -> {status}  {note}{flag}")

        if not record:
            # Housekeeping (a --sweep listing) shares Probe's HTTP path
            # but is not a finding, and writing it would collide with the
            # run's own seq numbering in the same directory.
            return Recorded(status, response_body, matched)
        self._write(
            seq,
            note,
            {
                "seq": seq,
                "note": note,
                "credentials_key": TOKEN_ENV_VAR,
                "prediction": predict,
                "prediction_matched": matched,
                "request": {
                    "method": method,
                    "url": BASE_URL + path,
                    "headers": {"Authorization": REDACTED},
                    "body": body,
                },
                "response": {"status": status, "body": response_body},
                "elapsed_ms": elapsed_ms,
                "probed_at": started.isoformat(),
            },
        )
        return Recorded(status, response_body, matched)

    def _write(self, seq: int, note: str, payload: dict[str, Any]) -> None:
        serialized = json.dumps(payload, indent=1, sort_keys=True) + "\n"
        # Mechanical, not best-effort: the token is never written above,
        # and this refuses to write at all if it appears anyway -- a
        # response body echoing it back would otherwise land in a
        # committed file.
        if self._token and self._token in serialized:
            raise ProbeError(f"refusing to write transcript {seq:02d}: it contains the token")
        (self.dir / f"{seq:02d}-{slugify(note)}.json").write_text(serialized, encoding="utf-8")


def base_arg_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--mutate", action="store_true", help="allow non-GET calls")
    group.add_argument("--dry-run", action="store_true", help="print the calls, send nothing")
    parser.add_argument(
        "--sweep", action="store_true", help="delete leftovers past the age floor, and exit"
    )
    return parser
