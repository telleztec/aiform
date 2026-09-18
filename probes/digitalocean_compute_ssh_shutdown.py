# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

"""Two-part diagnostic, not a permanent test (not pytest-discovered).

Test 2: create a droplet WITH an ssh_key at creation time ("day 0") and
confirm key-based SSH login works immediately -- the question being
whether this is a good aiform default that sidesteps #150's plaintext-
root-password-email problem (DigitalOcean's own documented behavior is
to skip generating/mailing a root password entirely when ssh_keys is
provided at create time).

Test 1: on that same droplet, SSH in and run a graceful in-guest
shutdown, then measure how long DigitalOcean's own droplet-status API
takes to report it "off" -- compared against the two ~303-304s outliers
observed tonight when using the DO API's power_off action directly
(issue #154's "Learnings" section). If SSH-initiated shutdown is
reliably fast, that's a real, different resolution path from anything
in #154/#172's timeout-tuning discussion.

Uses a freshly generated, throwaway SSH keypair -- never the operator's
own key -- uploaded to the DO account for this run only and deleted
afterward, along with the droplet, regardless of outcome.

Run:  python probes/digitalocean_compute_ssh_shutdown.py

Results from the run this file's own docstring and issue #175's spec
(specs/ssh.md, specs/digitalocean_compute.md) cite: 9/9 successful
SSH-initiated shutdowns completed in 11.3-24.4s, and day-0 ssh_keys
login worked within 8.7-23.3s of the droplet reporting active, on every
attempt -- see the PR that introduced aiform/ssh.py for the full run
transcript.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REGION = "sfo3"
SIZE = "s-1vcpu-2gb"
IMAGE = "ubuntu-24-04-x64"
TAG = "aiform-diag-ssh-shutdown"

BASE_URL = "https://api.digitalocean.com/v2"


def _api(method, path, token, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


def _log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _generate_throwaway_keypair(key_dir: Path) -> tuple[Path, Path]:
    private_key = key_dir / "aiform_diag_throwaway"
    subprocess.run(
        [
            "ssh-keygen",
            "-t",
            "ed25519",
            "-N",
            "",
            "-C",
            "aiform-diag-throwaway",
            "-f",
            str(private_key),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return private_key, key_dir / "aiform_diag_throwaway.pub"


def _ssh(private_key: Path, ip, command, timeout=20):
    return subprocess.run(
        [
            "ssh",
            "-i",
            str(private_key),
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "ConnectTimeout=8",
            "-o",
            "BatchMode=yes",
            f"root@{ip}",
            command,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def main() -> int:
    token = os.environ.get("DIGITALOCEAN_TOKEN")
    if not token:
        print("DIGITALOCEAN_TOKEN not set", file=sys.stderr)
        return 2

    key_id = None
    droplet_id = None
    with tempfile.TemporaryDirectory(prefix="aiform-ssh-diag-") as tmp:
        private_key, public_key = _generate_throwaway_keypair(Path(tmp))
        try:
            # --- upload the throwaway public key ---
            pubkey_text = public_key.read_text().strip()
            key_name = f"aiform-diag-throwaway-{int(time.time())}"
            _log(f"uploading throwaway SSH key {key_name}...")
            key_resp = _api(
                "POST", "/account/keys", token, {"name": key_name, "public_key": pubkey_text}
            )
            key_id = key_resp["ssh_key"]["id"]
            _log(f"uploaded, id={key_id}")

            # --- create the droplet WITH the key at creation time (Test 2 setup) ---
            name = f"aiform-diag-sshoff-{int(time.time())}"
            _log(f"creating {name} with ssh_keys=[{key_id}] ...")
            create_resp = _api(
                "POST",
                "/droplets",
                token,
                {
                    "name": name,
                    "region": REGION,
                    "size": SIZE,
                    "image": IMAGE,
                    "ssh_keys": [key_id],
                    "tags": [TAG],
                },
            )
            droplet_id = create_resp["droplet"]["id"]
            _log(f"created id={droplet_id}, waiting for active + public IP...")

            ip = None
            for _ in range(60):
                d = _api("GET", f"/droplets/{droplet_id}", token)["droplet"]
                if d["status"] == "active":
                    for net in d.get("networks", {}).get("v4", []):
                        if net.get("type") == "public":
                            ip = net["ip_address"]
                    if ip:
                        break
                time.sleep(3)
            if not ip:
                print("droplet never got a public IP while active", file=sys.stderr)
                return 1
            _log(f"active, ip={ip}")

            # --- Test 2: does key-based SSH login work immediately? ---
            _log("Test 2: attempting SSH login with the day-0 key (retrying while sshd boots)...")
            login_ok = False
            login_attempts = 0
            t0 = time.monotonic()
            while time.monotonic() - t0 < 120:
                login_attempts += 1
                try:
                    result = _ssh(private_key, ip, "echo aiform-diag-ssh-ok")
                    if result.returncode == 0 and "aiform-diag-ssh-ok" in result.stdout:
                        login_ok = True
                        break
                except subprocess.TimeoutExpired:
                    pass
                time.sleep(5)
            login_elapsed = time.monotonic() - t0
            _log(
                f"Test 2 result: login_ok={login_ok} after {login_attempts} attempt(s), "
                f"{login_elapsed:.1f}s since active. Day-0 ssh_keys creation "
                f"{'DOES' if login_ok else 'does NOT'} give working key-only access "
                f"(no password needed, no password-reset prompt encountered)."
            )
            if not login_ok:
                print("could not SSH in at all -- stopping before Test 1", file=sys.stderr)
                return 1

            # --- Test 1: SSH in, shut down gracefully, time how long DO reports it off ---
            _log("Test 1: issuing in-guest graceful shutdown via SSH...")
            t1 = time.monotonic()
            try:
                _ssh(private_key, ip, "sudo shutdown -h now", timeout=10)
            except subprocess.TimeoutExpired:
                # Expected: the connection drops as the guest actually shuts down.
                pass
            _log("shutdown command sent, polling DO's own status API...")

            off_at = None
            for _attempt in range(1, 91):  # up to ~180s at 2s/attempt before falling back
                d = _api("GET", f"/droplets/{droplet_id}", token)["droplet"]
                if d["status"] == "off":
                    off_at = time.monotonic() - t1
                    break
                time.sleep(2)
            if off_at is not None:
                _log(f"Test 1 result: SSH-initiated shutdown -> status=off in {off_at:.1f}s")
            else:
                _log(
                    "Test 1: still not 'off' after ~180s via SSH shutdown alone -- "
                    "falling back to the DO API power_off action, per DO's own "
                    "recommended pattern..."
                )
                t2 = time.monotonic()
                _api("POST", f"/droplets/{droplet_id}/actions", token, {"type": "power_off"})
                for _attempt in range(1, 226):  # up to 450s
                    d = _api("GET", f"/droplets/{droplet_id}", token)["droplet"]
                    if d["status"] == "off":
                        off_at = time.monotonic() - t2
                        _log(f"Test 1 result: fallback power_off -> status=off in {off_at:.1f}s")
                        break
                    time.sleep(2)
                if off_at is None:
                    _log("Test 1: fallback power_off ALSO did not complete in time")

            return 0
        finally:
            _log("cleanup: deleting droplet and throwaway SSH key...")
            try:
                if droplet_id:
                    urllib.request.urlopen(
                        urllib.request.Request(
                            f"{BASE_URL}/droplets/{droplet_id}",
                            method="DELETE",
                            headers={"Authorization": f"Bearer {token}"},
                        ),
                        timeout=20,
                    )
            except Exception as exc:  # noqa: BLE001
                print(f"(droplet cleanup issue: {exc})", file=sys.stderr)
            try:
                if key_id:
                    urllib.request.urlopen(
                        urllib.request.Request(
                            f"{BASE_URL}/account/keys/{key_id}",
                            method="DELETE",
                            headers={"Authorization": f"Bearer {token}"},
                        ),
                        timeout=20,
                    )
            except Exception as exc:  # noqa: BLE001
                print(f"(key cleanup issue: {exc})", file=sys.stderr)
            _log("cleanup done")


if __name__ == "__main__":
    sys.exit(main())
