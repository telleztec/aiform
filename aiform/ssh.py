# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

"""Provider-agnostic SSH mechanics -- see specs/ssh.md. Zero DigitalOcean
(or any provider) knowledge: registering a key with a CSP account API,
resolving an id to an IP, and deciding what "shut down" means for a given
provider all stay in the calling driver."""

import hashlib
import logging
import subprocess
import time
from pathlib import Path

logger = logging.getLogger("aiform.ssh")

DEFAULT_SSH_DIR = Path(".aiform/ssh")

_PRIVATE_KEY_NAME = "aiform_managed_key"
_PUBLIC_KEY_NAME = "aiform_managed_key.pub"
_BACKUP_SCRIPT_KEYCHAIN_NAME = "backup_key_to_keychain.sh"
_BACKUP_SCRIPT_ONEPASSWORD_NAME = "backup_key_to_1password.sh"

_BACKUP_ITEM_NAME = "aiform-managed-ssh-key"

_SHUTDOWN_COMMAND = "sudo shutdown -h now"
_RETRY_DELAY_SECONDS = 5.0
_PER_ATTEMPT_TIMEOUT_SECONDS = 12.0


def managed_key_exists(ssh_dir: Path) -> bool:
    """Whether ensure_managed_key(ssh_dir) would find an existing private
    key rather than generating a fresh one. Lets a caller that's about to
    *use* the key (not just prepare it) decide whether it's worth trying
    at all -- a caller that unconditionally called ensure_managed_key
    would mint a brand-new, DigitalOcean-unauthorized keypair the first
    time this is missing and then burn its whole SSH retry budget
    authenticating with it, for a value only a future create() can
    actually register."""
    return (ssh_dir / _PRIVATE_KEY_NAME).exists()


def ensure_managed_key(ssh_dir: Path) -> tuple[Path, Path]:
    ssh_dir.mkdir(parents=True, exist_ok=True)
    # A belt-and-suspenders gitignore inside the directory itself, not
    # just the repo-root entry `aiform init`'s _GITIGNORE_ENTRIES writes:
    # a project initialized before this existed, or one that never
    # re-runs `init` after upgrading, would otherwise get a private key
    # written into an un-ignored path the first time create() (not
    # init) calls this -- one `git add -A` away from being committed.
    # Written here, not only from cli.py, so every caller of this
    # get-or-create is covered regardless of whether `init` ran first.
    gitignore_path = ssh_dir / ".gitignore"
    if not gitignore_path.exists():
        gitignore_path.write_text("*\n", encoding="utf-8")

    private_key_path = ssh_dir / _PRIVATE_KEY_NAME
    public_key_path = ssh_dir / _PUBLIC_KEY_NAME

    if not private_key_path.exists():
        # -N "": an empty passphrase. This key secures an operational SSH
        # session aiform drives non-interactively (from a resize call, or
        # a future apply); a passphrase would make it unusable there.
        subprocess.run(
            [
                "ssh-keygen",
                "-t",
                "ed25519",
                "-N",
                "",
                "-C",
                "aiform-managed-key",
                "-f",
                str(private_key_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    private_key_path.chmod(0o600)

    if not public_key_path.exists():
        # Re-derived from the existing private key, never regenerated as a
        # fresh pair -- a fresh pair would silently orphan every droplet
        # already carrying the original public key.
        result = subprocess.run(
            ["ssh-keygen", "-y", "-f", str(private_key_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        public_key_path.write_text(result.stdout, encoding="utf-8")

    return private_key_path, public_key_path


_BACKUP_SCRIPT_PREAMBLE = """#!/bin/sh
# Backs up aiform's managed SSH private key to {backend}. aiform never
# runs this -- read it, then run it yourself, the same way you'd
# hand-edit .aiform/credentials.env rather than have a tool write a
# secret for you.
#
# This key is an aiform-internal operational credential: aiform generated
# it, uses it to shut droplets down quickly over SSH instead of
# DigitalOcean's own slower power_off action, and it is the *only* copy
# of it that exists once this script has not been run -- losing
# .aiform/ssh/aiform_managed_key means every droplet aiform created stops
# being reachable through the fast path (aiform falls back to the normal
# DigitalOcean power_off action automatically, so this is inconvenient,
# not catastrophic).
#
# See also {other_script}, the {other_backend} equivalent -- run
# whichever matches the backend you actually use.
set -eu
"""

_BACKUP_SCRIPT_KEYCHAIN_TEMPLATE = (
    _BACKUP_SCRIPT_PREAMBLE
    + """
SERVICE="{service}"
ACCOUNT="{account}"
KEY_FILE="{private_key_path}"

security add-generic-password -U -a "$ACCOUNT" -s "$SERVICE" -w "$(cat "$KEY_FILE")"
echo "Backed up $KEY_FILE to Keychain (service '$SERVICE', account '$ACCOUNT')."
echo "Restore with:"
echo "  security find-generic-password -a \\"$ACCOUNT\\" -s \\"$SERVICE\\" -w > \\"$KEY_FILE\\""
"""
)

_BACKUP_SCRIPT_ONEPASSWORD_TEMPLATE = (
    _BACKUP_SCRIPT_PREAMBLE
    + """
SERVICE="{service}"
KEY_FILE="{private_key_path}"

if op item get "$SERVICE" --vault=Private >/dev/null 2>&1; then
  op item edit "$SERVICE" --vault=Private "private key[password]=$(cat "$KEY_FILE")"
else
  op item create --category="SSH Key" --title="$SERVICE" --vault=Private \\
    "private key[password]=$(cat "$KEY_FILE")"
fi
echo "Backed up $KEY_FILE to 1Password (item '$SERVICE')."
echo "Restore with:"
echo "  op read \\"op://Private/$SERVICE/private key\\" > \\"$KEY_FILE\\""
"""
)


def _onepassword_item_name(ssh_dir: Path) -> str:
    # Scoped per project the same way the Keychain script's ACCOUNT field
    # is, so two aiform projects on this machine (or a re-run after a key
    # rotation) don't collide on one 1Password item -- but NOT by folding
    # the resolved path directly into the title: op:// secret references
    # are slash-delimited, and 1Password's own docs say a component
    # containing an unsupported character (a resolved path always has
    # "/") must be addressed by item ID instead of title, which would
    # break the very "op read op://..." restore command this script
    # prints. A short deterministic hash keeps the scoping without one.
    project_tag = hashlib.sha256(str(ssh_dir.resolve()).encode()).hexdigest()[:12]
    return f"{_BACKUP_ITEM_NAME}-{project_tag}"


def generate_backup_script(ssh_dir: Path, private_key_path: Path) -> tuple[Path, Path]:
    # Resolved, not left relative: the script is meant to be read and run
    # by a human, who may not be sitting in the project root when they do
    # -- a relative KEY_FILE would fail with a confusing `cat:` error, and
    # the restore line it echoes would write the key back to the wrong
    # place.
    resolved_private_key_path = private_key_path.resolve()

    keychain_path = ssh_dir / _BACKUP_SCRIPT_KEYCHAIN_NAME
    keychain_path.write_text(
        _BACKUP_SCRIPT_KEYCHAIN_TEMPLATE.format(
            backend="the macOS Keychain",
            other_script=_BACKUP_SCRIPT_ONEPASSWORD_NAME,
            other_backend="1Password CLI",
            service=_BACKUP_ITEM_NAME,
            account=str(ssh_dir.resolve()),
            private_key_path=resolved_private_key_path,
        ),
        encoding="utf-8",
    )
    keychain_path.chmod(0o700)

    onepassword_path = ssh_dir / _BACKUP_SCRIPT_ONEPASSWORD_NAME
    onepassword_path.write_text(
        _BACKUP_SCRIPT_ONEPASSWORD_TEMPLATE.format(
            backend="1Password",
            other_script=_BACKUP_SCRIPT_KEYCHAIN_NAME,
            other_backend="macOS Keychain",
            service=_onepassword_item_name(ssh_dir),
            private_key_path=resolved_private_key_path,
        ),
        encoding="utf-8",
    )
    onepassword_path.chmod(0o700)

    return keychain_path, onepassword_path


def _ssh_argv(ip: str, private_key_path: Path, known_hosts_path: Path, command: str) -> list[str]:
    return [
        "ssh",
        "-i",
        str(private_key_path),
        # Try only the managed key, never anything already loaded in an
        # ssh-agent -- with -i alone, ssh also offers agent identities
        # first, and an operator with several keys loaded can exhaust the
        # server's MaxAuthTries before the managed key is ever tried.
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts_path}",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ConnectTimeout=8",
        "-o",
        "BatchMode=yes",
        f"root@{ip}",
        command,
    ]


def shutdown_via_ssh(
    ip: str,
    private_key_path: Path,
    known_hosts_path: Path,
    *,
    connect_timeout_budget: float,
) -> bool:
    # Wall-clock bounded, not attempt-count bounded: no attempt after the
    # first is ever started unless there's still enough of the budget
    # left to give it the FULL _PER_ATTEMPT_TIMEOUT_SECONDS ceiling --
    # never a truncated one. That's what makes a `subprocess.TimeoutExpired`
    # below a reliable signal: it means ssh ran for the full ceiling and
    # still didn't return (the expected shape of a successful in-guest
    # shutdown), never an artifact of this function cutting an attempt
    # short.
    argv = _ssh_argv(ip, private_key_path, known_hosts_path, _SHUTDOWN_COMMAND)
    deadline = time.monotonic() + connect_timeout_budget
    first_attempt = True

    while True:
        remaining = deadline - time.monotonic()
        # At least one real attempt always happens, even for a budget of
        # 0 or one already spent computing the deadline -- refusing to
        # try at all would unfairly fail a connection that was about to
        # succeed. This is the only way total wall time can exceed
        # connect_timeout_budget, and only by up to one ceiling's worth,
        # for a budget smaller than one.
        if not first_attempt and remaining < _PER_ATTEMPT_TIMEOUT_SECONDS:
            return False
        first_attempt = False

        try:
            result = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=_PER_ATTEMPT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            # The expected, successful shape: the guest tears the
            # connection down as it shuts down before ssh can read a
            # clean exit status -- see specs/ssh.md. Only a meaningful
            # signal because this attempt ran for the full ceiling, not
            # a truncated one -- see the comment above.
            return True
        if result.returncode == 0:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(_RETRY_DELAY_SECONDS, remaining))


def forget_host(ip: str, known_hosts_path: Path) -> None:
    # DigitalOcean recycles IPs across droplets, and shutdown_via_ssh's
    # StrictHostKeyChecking=accept-new means known_hosts_path gains one
    # entry per IP ever connected to with nothing pruning it -- a stale
    # entry for a destroyed droplet's IP would otherwise make a future
    # droplet at that same IP fail SSH with a host-key mismatch instead
    # of the intended accept-new behavior.
    if not known_hosts_path.exists():
        return
    try:
        subprocess.run(
            ["ssh-keygen", "-R", ip, "-f", str(known_hosts_path)],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        # Best-effort: a failed prune must never fail the delete() it
        # runs inside.
        logger.warning("failed to prune known_hosts entry", extra={"ip": ip}, exc_info=True)
