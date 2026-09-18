# specs/ssh.md — `aiform/ssh.py`

## Purpose

Provider-agnostic SSH mechanics — generate/reuse a local managed keypair,
write a one-time backup script for it, and attempt an in-guest shutdown
over SSH — used by `drivers/digitalocean/compute.py`'s resize path (issue
#175) to make droplet power-off fast and reliable, and by `aiform init` to
provision the key up front. Zero DigitalOcean (or any provider) knowledge:
everything CSP-specific — registering the public key with an account API,
injecting a key id into `create()`, the `power_off` action fallback —
stays in the driver. See the PR that introduced this module for the full
design rationale (issue #175's approved plan): the mechanism exists
because DigitalOcean's own `power_off` action attempts a graceful signal
first and only forces a hard stop after ~5 minutes (issues #152, #168),
and a live diagnostic found that SSHing in and running an in-guest
`shutdown` bypasses that entirely (9/9 successful attempts, 11.3-24.4s).

## Interface

```python
# aiform/ssh.py

DEFAULT_SSH_DIR = Path(".aiform/ssh")


def managed_key_exists(ssh_dir: Path) -> bool: ...
def ensure_managed_key(ssh_dir: Path) -> tuple[Path, Path]: ...
def generate_backup_script(ssh_dir: Path, private_key_path: Path) -> tuple[Path, Path]: ...
def shutdown_via_ssh(
    ip: str,
    private_key_path: Path,
    known_hosts_path: Path,
    *,
    connect_timeout_budget: float,
) -> bool: ...
def forget_host(ip: str, known_hosts_path: Path) -> None: ...
```

`DEFAULT_SSH_DIR` follows the same implicit-CWD convention as
`aiform/state.py`'s `DEFAULT_STATE_PATH` and `aiform/log.py`'s
`.aiform/logs/` — a bare relative `Path`, resolved against wherever
`aiform` is invoked from, never an absolute path computed from this
module's own location.

## Behavior

- `managed_key_exists(ssh_dir)` — whether `ensure_managed_key(ssh_dir)`
  would find an existing private key rather than generating a fresh one.
  Lets a caller that's about to *use* the key, not just prepare it,
  decide whether trying is worth it at all: a caller that
  unconditionally called `ensure_managed_key` when none exists yet would
  mint a brand-new, not-yet-DigitalOcean-registered keypair on the spot,
  then spend its entire SSH connect budget authenticating with a key no
  droplet has ever heard of. `drivers/digitalocean/compute.py`'s
  `_power_off_droplet()` checks this first and skips straight to the API
  fallback when it's `False`, logging `power_off_path=no-key-fallback`.
- `ensure_managed_key(ssh_dir)` — get-or-create. Creates `ssh_dir` if
  missing, and writes `ssh_dir / ".gitignore"` containing a bare `*` the
  first time (idempotent past that) — belt-and-suspenders alongside
  `aiform init`'s repo-root `.gitignore` entry for `.aiform/ssh/`, since
  this function's caller isn't always `init`: `create()` calls it too,
  so a project that upgrades aiform and runs `apply` without re-running
  `init` still never gets a private key written into an un-ignored path.
  If `ssh_dir / "aiform_managed_key"` already exists, reuses it
  (never regenerates, never prompts `ssh-keygen`'s interactive overwrite
  question) and re-asserts `0o600` on it before returning. Otherwise runs
  `ssh-keygen -t ed25519 -N "" -C aiform-managed-key -f <path>`
  non-interactively (empty passphrase — this key secures an operational
  SSH session aiform itself drives non-interactively; a passphrase would
  make it unusable from an automated `apply`) and chmods the private key
  `0o600` immediately after. If the public half
  (`ssh_dir / "aiform_managed_key.pub"`) is separately missing (e.g. an
  operator deleted just that file, or restored only the private key from
  a backup), regenerates it from the private key via
  `ssh-keygen -y -f <private_key_path>` rather than treating a missing
  `.pub` as a reason to regenerate the whole pair — a regenerated pair
  would silently orphan every droplet already carrying the old public
  key. Returns `(private_key_path, public_key_path)`.
- `generate_backup_script(ssh_dir, private_key_path)` — writes **two**
  separately executable scripts, `0o700`, each overwriting any existing
  copy (idempotent, no get-or-create semantics needed: unlike the keypair
  itself, re-generating these files is harmless and keeps them in sync if
  a template ever changes), and returns `(keychain_path, onepassword_path)`:
  - `ssh_dir / "backup_key_to_keychain.sh"` uses macOS
    `security add-generic-password -U -a "<account>" -s
    aiform-managed-ssh-key -w "$(cat <private_key_path>)"` — mirroring
    this repo's own `.envrc` Keychain pattern (`-U` upserts, so re-running
    the script after a key rotation just updates the stored value).
    `<account>` is `str(ssh_dir.resolve())` — the resolved absolute path
    to the project's own `.aiform/ssh` directory, not a bare constant —
    a machine with more than one aiform project (this one has two
    DigitalOcean accounts across projects, per `.envrc`'s own preamble)
    would otherwise have every project's backup script collide on the
    same Keychain entry and overwrite each other's key. Includes a
    restore command as an echoed comment
    (`security find-generic-password ... -w > <private_key_path>`).
  - `ssh_dir / "backup_key_to_1password.sh"` runs the equivalent
    `op item create --category="SSH Key" ...` / `op read` pair against
    the 1Password CLI, for an operator not using Keychain. Scoped and
    upserted the same way the Keychain script is, for the same reason:
    the item title is `"<constant> (<account>)"` (`<account>` again
    `str(ssh_dir.resolve())`), not a bare constant, so two projects don't
    collide on one item; the script checks `op item get` first and runs
    `op item edit` instead of `op item create` when the item already
    exists, since the 1Password CLI has no single-command upsert
    equivalent to Keychain's `-U`.
  - Both: `set -eu`, comments explaining what the script does and why it
    exists (since the human is expected to read it before running it),
    and a pointer to the other script so a reader who opens the wrong one
    can find the one for their backend.
  - aiform never executes either script itself — same "prepare, don't
    run" property `_cmd_init` already has for `.aiform/credentials.env`.
  - `aiform init` prints both paths so the human can choose which backend
    to use, rather than picking one for them.
- `shutdown_via_ssh(ip, private_key_path, known_hosts_path, *,
  connect_timeout_budget)` — attempts `ssh -i <private_key_path> -o
  IdentitiesOnly=yes -o UserKnownHostsFile=<known_hosts_path> -o
  StrictHostKeyChecking=accept-new -o ConnectTimeout=8 -o
  BatchMode=yes root@<ip> "sudo shutdown -h now"`, retrying on a
  connection failure (non-zero exit) roughly every 5 seconds.
  `IdentitiesOnly=yes` matters here specifically: without it `ssh` also
  offers any identity already loaded in an agent before trying `-i`'s
  key, and an operator with several keys loaded can exhaust the server's
  `MaxAuthTries` before the managed key is ever tried.
  **Wall-clock bounded, not attempt-count bounded, and never truncated**:
  a shared `deadline = time.monotonic() + connect_timeout_budget`
  decides whether another attempt is allowed to *start*, but every
  attempt that does start always gets the full, untruncated
  `_PER_ATTEMPT_TIMEOUT_SECONDS` (12s) as its own subprocess timeout —
  no attempt after the first is started unless the remaining budget can
  fit that full ceiling. This went through two prior, both-flawed
  versions before landing here, each caught by a separate `/code-review`
  pass:
  1. The original derived a fixed attempt count from
     `connect_timeout_budget / 5s` alone. Since each attempt's own
     subprocess timeout was a *separate*, unbounded-by-the-budget
     ceiling, a 45s budget could in the worst case spend upwards of 150s
     of real wall time before giving up — silently regressing the exact
     fallback-latency problem #171/#172/#174 were rejected for.
  2. The fix for that truncated an attempt's own timeout to whatever
     budget remained (`min(ceiling, remaining)`) to keep total wall time
     bounded. That broke the meaning of `subprocess.TimeoutExpired`
     below: it stopped reliably meaning "ssh had the full ceiling and
     still didn't return" and could fire just because this function cut
     an attempt off early — misreporting a still-connecting, unreached
     attempt as a successful shutdown, and costing the caller a wasted
     poll on the strength of that false signal.
  Refusing to *start* an attempt that can't fit the full ceiling (rather
  than truncating one that already started) fixes both: total wall time
  can only exceed `connect_timeout_budget` via the mandatory first
  attempt, and only by up to one ceiling's worth (for a budget smaller
  than one) — and every `TimeoutExpired` still means what it always
  meant. At least one real attempt always happens regardless of the
  budget, for the same reason as before: refusing to try at all would
  unfairly fail a connection that was about to succeed. Returns `True`
  as soon as either:
  - the command exits `0` (rare in practice — the guest usually tears
    the connection down as it shuts down before `ssh` can read a clean
    exit status), or
  - the subprocess call raises `subprocess.TimeoutExpired` — this is the
    *expected*, successful shape: `probes/digitalocean_compute_ssh_shutdown.py`
    observed exactly this on every one of its 9 successful runs, so a
    timeout here means the connection was accepted and the shutdown
    command was very likely issued before the guest tore the session
    down, not that the attempt failed. Only a meaningful signal because
    the attempt ran for the full ceiling, never a truncated one.
  Returns `False` once `connect_timeout_budget` is exhausted with every
  attempt failing outright (non-zero exit, no timeout) — e.g. the guest
  refuses the connection, the managed key isn't authorized, or `sshd`
  isn't up. **Does not poll DigitalOcean's own API for `status == "off"`**
  — that is a provider-specific question the caller (the driver, via its
  own `_poll_until`) answers.
- `BatchMode=yes` guarantees a non-interactive failure (no password
  prompt hang) if key auth doesn't work for any reason.
  `StrictHostKeyChecking=accept-new` pins the host key on first contact
  and rejects a later *change* to it, safer than the diagnostic's own
  throwaway `=no` + `/dev/null` combination — see Edge cases.
- `forget_host(ip, known_hosts_path)` — removes any entry for `ip` from
  `known_hosts_path` via `ssh-keygen -R <ip> -f <known_hosts_path>`. A
  no-op (no subprocess call) if `known_hosts_path` doesn't exist yet.
  Best-effort: never raises, logs a warning on an unexpected failure
  instead. Exists because DigitalOcean recycles IPs across droplets —
  `StrictHostKeyChecking=accept-new` (above) means `known_hosts_path`
  gains one entry per IP `shutdown_via_ssh` ever connects to, and nothing
  else prunes it; a stale entry for a destroyed droplet's IP would make a
  later droplet at that same IP fail SSH with a host-key mismatch instead
  of the intended accept-new behavior.
  `drivers/digitalocean/compute.py`'s `delete()` calls this after a
  successful DELETE, for whatever IP the droplet last had.

## Edge cases / errors

- `shutdown_via_ssh` distinguishes "connected but the shutdown likely
  fired" (a `TimeoutExpired`) from "never connected" (every attempt
  returns non-zero and the budget runs out) — it does **not** try to
  further distinguish "guest refused the connection" from "guest
  rejected the command" (e.g. a key that authenticates but whose
  account can't run `sudo`); both look identical from the caller's
  side (a non-zero exit with no timeout) and both correctly fall back
  to the caller's own resolution.
- `ensure_managed_key` never overwrites an existing private key file —
  `ssh-keygen -f` on an existing path prompts interactively, which would
  hang under `subprocess.run` (no stdin attached); the existence check
  happens before ever invoking `ssh-keygen` for exactly this reason.
- `known_hosts_path` is the caller's responsibility to place under
  `.aiform/ssh/` (project-scoped, not `~/.ssh/known_hosts`) — this
  module only ever passes it through to `ssh -o UserKnownHostsFile=...`
  and never reads or writes it directly. `ssh` itself creates the file
  on first use if it doesn't exist, given the parent directory does
  (which `ensure_managed_key` already guarantees by the time a caller
  would have a private key to shut down with).
- `ensure_managed_key` raises `subprocess.CalledProcessError` unmodified
  if `ssh-keygen` itself fails (e.g. the binary is missing) — no local
  error-classification layer, since a broken local OpenSSH install is
  not a recoverable condition this module can work around.
  `generate_backup_script` runs no subprocess at all (it only formats
  and writes a template), so this does not apply to it.
- `forget_host` never raises — an unexpected `ssh-keygen -R` failure (a
  permissions problem, a corrupt `known_hosts_path`) is logged as a
  warning and swallowed, since a failed best-effort prune must not fail
  the `delete()` it runs inside.

## Out of scope

- Any DigitalOcean (or other provider) API call — registering the
  public key with an account, resolving a droplet id to an IP, deciding
  whether SSH succeeded means "powered off" on that provider's terms.
  All of that stays in `drivers/digitalocean/compute.py`.
- A real keystore, cloud KMS, or cross-machine key sync. Deliberately
  scoped to a single operator on one persistent machine — see the
  issue #175 plan's §1 for the full reasoning; the escape hatch for a
  team is sharing `.aiform/ssh/` out-of-band, the same as
  `credentials.env` already relies on.
- Running `backup_key_to_keychain.sh` automatically. aiform prepares it;
  the human runs it, deliberately, the same way `credentials.env` is
  hand-edited rather than aiform-written.
