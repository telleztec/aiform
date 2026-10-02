# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

"""Cut an in-process aiform run off at a chosen point -- see
specs/system_test_interrupt.md.

Everything here raises `InjectedInterrupt`, a `KeyboardInterrupt`: a
`BaseException`, so it passes through every `except Exception` in the
orchestrator and the drivers, exactly as a real Ctrl-C does. No live
dependency is imported, so tests/test_system_fault_injection.py can exercise
it offline against a fake `urlopen`.
"""

import contextlib
import io
import json
import re
import threading
import time
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from aiform import state


class InjectedInterrupt(KeyboardInterrupt):
    pass


@dataclass
class Fault:
    fired: bool = False
    response: dict | None = None
    seen: list[tuple[str, str]] = field(default_factory=list)
    worker_error: BaseException | None = None
    _workers: list[threading.Thread] = field(default_factory=list, repr=False)

    def join(self, timeout: float = 10.0) -> None:
        """Wait for every late provider call a `timeout` fault started."""
        for worker in self._workers:
            worker.join(timeout)
            if worker.is_alive():
                raise RuntimeError(f"a late provider call is still running after {timeout}s")


class _ReplayedResponse:
    """What a non-firing match hands back: the body was consumed to inspect
    it, so the caller gets the same bytes again. Everything else (status,
    headers) is the real response's."""

    def __init__(self, real: Any, body: bytes):
        self._real = real
        self._body = io.BytesIO(body)

    def read(self, *args: Any) -> bytes:
        return self._body.read(*args)

    def close(self) -> None:
        self._real.close()

    def __enter__(self) -> "_ReplayedResponse":
        return self

    def __exit__(self, *exc: Any) -> bool:
        self.close()
        return False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def _describe(request: Any) -> tuple[str, str]:
    if isinstance(request, str):
        return "GET", request
    return request.get_method(), request.full_url


def _parse(body: bytes) -> dict | None:
    try:
        parsed = json.loads(body)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


@contextlib.contextmanager
def _patched_urlopen(wrapper_factory: Callable[[Callable], Callable]) -> Iterator[None]:
    real = urllib.request.urlopen
    urllib.request.urlopen = wrapper_factory(real)
    try:
        yield
    finally:
        urllib.request.urlopen = real


@contextlib.contextmanager
def interrupt_after_request(
    method: str,
    url_pattern: str,
    *,
    occurrence: int = 1,
    response_predicate: Callable[[dict | None], bool] | None = None,
) -> Iterator[Fault]:
    """Let the provider act, then raise before the caller sees the answer."""
    fault = Fault()
    pattern = re.compile(url_pattern)
    matches = 0

    def factory(real: Callable) -> Callable:
        def urlopen(request: Any, *args: Any, **kwargs: Any) -> Any:
            nonlocal matches
            request_method, url = _describe(request)
            fault.seen.append((request_method, url))
            response = real(request, *args, **kwargs)
            if fault.fired or request_method != method or not pattern.search(url):
                return response
            with response:
                body = response.read()
            parsed = _parse(body)
            if response_predicate is not None and not response_predicate(parsed):
                return _ReplayedResponse(response, body)
            matches += 1
            if matches < occurrence:
                return _ReplayedResponse(response, body)
            fault.fired = True
            fault.response = parsed
            raise InjectedInterrupt(f"injected after {method} {url}")

        return urlopen

    with _patched_urlopen(factory):
        yield fault


@contextlib.contextmanager
def interrupt_before_request(
    method: str, url_pattern: str, *, occurrence: int = 1
) -> Iterator[Fault]:
    """Raise before the provider is reached, so it never acts."""
    fault = Fault()
    pattern = re.compile(url_pattern)
    matches = 0

    def factory(real: Callable) -> Callable:
        def urlopen(request: Any, *args: Any, **kwargs: Any) -> Any:
            nonlocal matches
            request_method, url = _describe(request)
            fault.seen.append((request_method, url))
            if not fault.fired and request_method == method and pattern.search(url):
                matches += 1
                if matches >= occurrence:
                    fault.fired = True
                    raise InjectedInterrupt(f"injected before {method} {url}")
            return real(request, *args, **kwargs)

        return urlopen

    with _patched_urlopen(factory):
        yield fault


@contextlib.contextmanager
def interrupt_after_state_save(
    predicate: Callable[[Any], bool], *, occurrence: int = 1
) -> Iterator[Fault]:
    """Let the save reach disk, then raise, on the save whose state satisfies
    `predicate`."""
    fault = Fault()
    real = state.save
    matches = 0

    def save(st: Any, *args: Any, **kwargs: Any) -> None:
        nonlocal matches
        real(st, *args, **kwargs)
        fault.seen.append(("state.save", ",".join(sorted(st.resources))))
        if fault.fired or not predicate(st):
            return
        matches += 1
        if matches >= occurrence:
            fault.fired = True
            raise InjectedInterrupt("injected after state.save")

    state.save = save
    try:
        yield fault
    finally:
        state.save = real


DEFAULT_DELAY_SECONDS = 1.5
DEFAULT_DEADLINE_SECONDS = 1.0
DEFAULT_JOIN_TIMEOUT_SECONDS = 10.0


def _timeout(
    fault: Fault,
    real: Callable,
    request: Any,
    args: tuple,
    kwargs: dict,
    *,
    provider_acts: bool,
    delay: float,
    deadline: float,
) -> Any:
    """The caller's deadline passes before the (slow) provider call returns."""

    def late_call() -> None:
        time.sleep(delay)
        try:
            with real(request, *args, **kwargs) as response:
                fault.response = _parse(response.read())
        except BaseException as exc:
            fault.worker_error = exc

    if provider_acts:
        worker = threading.Thread(target=late_call, daemon=True)
        fault._workers.append(worker)
        worker.start()
    time.sleep(deadline)
    method, url = _describe(request)
    raise TimeoutError(f"injected: {method} {url} timed out after {deadline}s")


_FAILURES: dict[str, Callable[..., Any]] = {"timeout": _timeout}


def fail_request(
    method: str,
    url_pattern: str,
    failure: str,
    *,
    provider_acts: bool = True,
    occurrence: int = 1,
    delay: float = DEFAULT_DELAY_SECONDS,
    deadline: float = DEFAULT_DEADLINE_SECONDS,
    join_timeout: float = DEFAULT_JOIN_TIMEOUT_SECONDS,
) -> contextlib.AbstractContextManager[Fault]:
    """Make the caller see `failure` on the `occurrence`th match, while the
    provider (when `provider_acts`) still does the work. Arguments are checked
    here, so a bad call fails at the call and not on entering the block."""
    if failure not in _FAILURES:
        raise ValueError(f"unknown failure {failure!r}; known: {', '.join(sorted(_FAILURES))}")
    if delay <= deadline:
        raise ValueError(f"delay ({delay}s) must exceed deadline ({deadline}s)")
    return _fail_request(
        method,
        url_pattern,
        _FAILURES[failure],
        provider_acts=provider_acts,
        occurrence=occurrence,
        delay=delay,
        deadline=deadline,
        join_timeout=join_timeout,
    )


@contextlib.contextmanager
def _fail_request(
    method: str,
    url_pattern: str,
    failure: Callable[..., Any],
    *,
    provider_acts: bool,
    occurrence: int,
    delay: float,
    deadline: float,
    join_timeout: float,
) -> Iterator[Fault]:
    fault = Fault()
    pattern = re.compile(url_pattern)
    matches = 0

    def factory(real: Callable) -> Callable:
        def urlopen(request: Any, *args: Any, **kwargs: Any) -> Any:
            nonlocal matches
            request_method, url = _describe(request)
            fault.seen.append((request_method, url))
            if fault.fired or request_method != method or not pattern.search(url):
                return real(request, *args, **kwargs)
            matches += 1
            if matches < occurrence:
                return real(request, *args, **kwargs)
            fault.fired = True
            return failure(
                fault,
                real,
                request,
                args,
                kwargs,
                provider_acts=provider_acts,
                delay=delay,
                deadline=deadline,
            )

        return urlopen

    try:
        with _patched_urlopen(factory):
            yield fault
    finally:
        fault.join(join_timeout)
