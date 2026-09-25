# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

"""Timing diagnostic, not a permanent test (not pytest-discovered).

Issue #207: two live system-test runs nine minutes apart both exhausted
`_poll_until`'s shared 150s default waiting for a resized droplet's
`size_slug`, at ~182.6s and ~183.4s, blocking `system-test` for every
runtime-path PR. The issue's own proposed fix was to widen the resize
budget, and its own text asked for a probe first rather than "another
round of doubling" -- #154's standing complaint about hand-tuned
per-driver timeouts.

What this measures, and why it is two clocks rather than one:

  A. the resize ACTION reaching "completed"  (GET /v2/actions/{id})
  B. `size_slug` flipping on the droplet     (GET /v2/droplets/{id})

`drivers/digitalocean/compute.py`'s resize step polls (B). If (A) were
fast and (B) slow, the right fix would be to poll the action instead of
widening anything -- so the two have to be timed separately to tell those
cases apart.

It also runs each resize behind both shutdown paths, because the driver
has both and #175 made one of them much faster:

  * DO's hard `power_off` action -- the API fallback path
  * DO's graceful `shutdown` action -- the closest API analogue to the
    in-guest `shutdown -h now` that `_power_off_droplet` attempts over SSH

The hypothesis that motivated the second mode was that #175 had *moved*
the wait rather than removing it: if the time before DO will complete a
resize were roughly constant from the shutdown request, a fast in-guest
shutdown would leave the remainder to be paid inside the resize poll.
The measurements below refute that -- a fast shutdown is followed by a
fast resize, so the two are independent.

Observed (sfo3, `disk: false`, ubuntu-24-04-x64, smallest sizes):

  * (B) size_slug flip: 12.2 / 21.5 / 22.6 / 23.3 / 23.9s  (n=5)
  * (A) action completed: 45.4 / 45.9 / 46.8s              (n=3)
  * graceful `shutdown` -> status=off: 10.2 / 10.3s        (n=2)
  * hard `power_off` -> status=off: 303.5s                 (n=1 here)

The last one corroborates the two ~303-304s `power_off` outliers already
recorded in #154's "Learnings" and cited by
`probes/digitalocean_compute_ssh_shutdown.py`, and that probe's own
11.3-24.4s SSH-shutdown range brackets the 10.2-10.3s seen here.

The conclusion #207 needed: resize is not slow. The old 150s budget was
already ~6x the median, so it was never set below typical latency -- it
just failed to absorb a provider-side stall roughly 8x the median. And
because **neither production failure ever completed**, nothing here can
say what budget *would* have sufficed; only that 183s did not. The 420s
the driver now uses is a policy choice about tail coverage, which is why
it is commented as a tail guard rather than an estimate.

Creates ONE droplet, tagged, and always destroys it in a `finally` --
then re-lists the account to prove it is gone. Never touches anything it
did not create.

Usage:  DIGITALOCEAN_TOKEN=... python3 probes/digitalocean_compute_resize.py
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE_URL = "https://api.digitalocean.com/v2"
REQUEST_TIMEOUT_SECONDS = 30
TOKEN_ENV_VAR = "DIGITALOCEAN_TOKEN"

REGION = "sfo3"
IMAGE = "ubuntu-24-04-x64"
# tests/system/conftest.py's SIZE and ALTERNATE_SIZE -- the exact pair the
# failing system tests resize between, so this measures that resize and not
# a differently-shaped one.
SIZE_A = "s-1vcpu-512mb-10gb"
SIZE_B = "s-1vcpu-1gb"
TAG = "aiform-probe-resize"

POLL_EVERY_SECONDS = 2.0
# Deliberately far past any budget under discussion: this probe exists to
# observe a duration, so it must not itself time out at the number being
# questioned.
HARD_CAP_SECONDS = 900.0

# A freshly-booted guest has not finished starting services, and an in-guest
# shutdown of one is not what the system test exercises.
SETTLE_SECONDS = 60


def say(message: str) -> None:
    print(f"{time.strftime('%H:%M:%SZ', time.gmtime())} {message}", flush=True)


def call(token: str, method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    for attempt in range(5):
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < 4:
                time.sleep(2**attempt)
                continue
            detail = ""
            try:
                detail = exc.read().decode()[:400]
            except Exception:
                pass
            raise RuntimeError(f"{method} {path} -> {exc.code} {detail}") from exc
    raise RuntimeError(f"{method} {path} exhausted retries")


def droplet(token: str, droplet_id: int) -> dict:
    return call(token, "GET", f"/droplets/{droplet_id}")["droplet"]


def wait_for(label: str, check, cap: float = HARD_CAP_SECONDS) -> tuple[float | None, int]:
    start = time.monotonic()
    attempts = 0
    while True:
        attempts += 1
        if check():
            seconds = time.monotonic() - start
            say(f"  {label}: {seconds:.1f}s / {attempts} attempts")
            return seconds, attempts
        elapsed = time.monotonic() - start
        if elapsed > cap:
            say(f"  {label}: NOT REACHED in {elapsed:.1f}s / {attempts} attempts")
            return None, attempts
        time.sleep(POLL_EVERY_SECONDS)


def measure(token: str, droplet_id: int, target: str, stop_action: str, label: str) -> dict:
    """Stop the droplet with `stop_action`, resize to `target`, and time both
    the action clock and the size_slug clock from the resize POST."""
    say(f"--- {label}: {stop_action} then resize -> {target}")

    stop_start = time.monotonic()
    if droplet(token, droplet_id)["status"] != "off":
        call(token, "POST", f"/droplets/{droplet_id}/actions", {"type": stop_action})
        stop_seconds, _ = wait_for(
            f"{stop_action} (status==off)",
            lambda: droplet(token, droplet_id)["status"] == "off",
        )
    else:
        stop_seconds = 0.0
        say("  already off")

    action = call(
        token,
        "POST",
        f"/droplets/{droplet_id}/actions",
        {"type": "resize", "disk": False, "size": target},
    )["action"]
    action_id = action["id"]
    say(f"  resize action id={action_id} status={action['status']}")

    resize_start = time.monotonic()
    action_seconds: float | None = None
    size_seconds: float | None = None
    while True:
        if action_seconds is None:
            status = call(token, "GET", f"/actions/{action_id}")["action"]["status"]
            if status in ("completed", "errored"):
                action_seconds = time.monotonic() - resize_start
                say(f"  (A) action {status}: {action_seconds:.1f}s")
        if size_seconds is None and droplet(token, droplet_id)["size_slug"] == target:
            size_seconds = time.monotonic() - resize_start
            say(f"  (B) size_slug flipped: {size_seconds:.1f}s")
        if action_seconds is not None and size_seconds is not None:
            break
        if time.monotonic() - resize_start > HARD_CAP_SECONDS:
            say(f"  gave up: A={action_seconds} B={size_seconds}")
            break
        time.sleep(POLL_EVERY_SECONDS)

    return {
        "label": label,
        "stop_action": stop_action,
        "target": target,
        "stop_to_off_seconds": stop_seconds,
        "action_completed_seconds": action_seconds,
        "size_slug_flipped_seconds": size_seconds,
        "size_slug_attempts_at_2s": None if size_seconds is None else int(size_seconds / 2) + 1,
        "total_from_stop_seconds": time.monotonic() - stop_start,
    }


def main() -> int:
    token = os.environ.get(TOKEN_ENV_VAR)
    if not token:
        print(f"{TOKEN_ENV_VAR} is not set", file=sys.stderr)
        return 2

    name = f"aiform-probe-resize-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    droplet_id = None
    results: list[dict] = []
    try:
        say(f"creating {name} ({SIZE_A}, {REGION}, tag {TAG})")
        droplet_id = call(
            token,
            "POST",
            "/droplets",
            {
                "name": name,
                "region": REGION,
                "size": SIZE_A,
                "image": IMAGE,
                "tags": [TAG],
                "monitoring": False,
                "backups": False,
            },
        )["droplet"]["id"]
        say(f"created id={droplet_id}")
        wait_for("droplet active", lambda: droplet(token, droplet_id)["status"] == "active")

        # Hard power_off first: this is the API fallback path, and the one
        # whose ~303s outliers #154 already recorded.
        results.append(measure(token, droplet_id, SIZE_B, "power_off", "hard power_off"))

        # Then the graceful path twice, for n=2 rather than an anecdote.
        for index, target in enumerate((SIZE_A, SIZE_B), start=1):
            say("  powering back on")
            call(token, "POST", f"/droplets/{droplet_id}/actions", {"type": "power_on"})
            wait_for("droplet active", lambda: droplet(token, droplet_id)["status"] == "active")
            say(f"  settling {SETTLE_SECONDS}s so the guest is fully booted")
            time.sleep(SETTLE_SECONDS)
            results.append(
                measure(token, droplet_id, target, "shutdown", f"graceful shutdown {index}")
            )
    finally:
        if droplet_id is not None:
            say(f"destroying id={droplet_id}")
            for attempt in range(6):
                try:
                    call(token, "DELETE", f"/droplets/{droplet_id}")
                    say("  delete accepted")
                    break
                except RuntimeError as exc:
                    say(f"  delete attempt {attempt + 1} failed: {exc}")
                    time.sleep(5)
            else:
                say(f"  *** MANUAL CLEANUP NEEDED: droplet {droplet_id} ***")
            time.sleep(5)
            remaining = call(token, "GET", "/droplets?per_page=200")["droplets"]
            leaked = [d for d in remaining if TAG in (d.get("tags") or [])]
            say(f"verify: {len(remaining)} on account, {len(leaked)} still tagged {TAG}")

        say("")
        say("=== SUMMARY ===")
        print(json.dumps(results, indent=2), flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
