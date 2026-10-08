# Plan amendment 2: #270 — `write_firewall_aiform_md` takes `vm_ids`

Amends `plans/270-neutral-vm-terms-amendment.md`. Owner decision of 2026-10-08: decision 4 is reversed.

## Change

- `write_firewall_aiform_md(..., droplet_ids=...)` in `tests/system/conftest.py` becomes `vm_ids=...`.
- The helper still writes DigitalOcean's own field, `droplet_ids`, into the firewall's frontmatter. The file format is the provider's; only the Python keyword is neutral.
- Every call that passed the keyword now passes `vm_ids=`: `test_cli_digitalocean.py` (1), `test_cli_firewall.py` (2), `test_cli_interrupt.py` (3), `test_cli_references.py` (2). Calls that omit it are unchanged.
- The helper's docstring says `vm_ids`.

## Test (red first)

`tests/test_system_conftest.py`, beside `TestFirewallWaitHelperNamesNoProviderField`:

1. The helper's parameters contain no name with `droplet`. Red: `droplet_ids` is a parameter.
2. Given `vm_ids=[7]`, the written file's frontmatter holds `params.droplet_ids == [7]`. Red before the rename (TypeError); it guards the file format.
3. Given no `vm_ids`, `params.droplet_ids == []` (green before and after).

## Not changing

- `provider_ledger.Ledger.droplet_ids`, `droplet_ids_to_delete`, and the other `droplet` mentions in `tests/system/`. They name DigitalOcean resources in DigitalOcean-specific test code.
- `plans/270-neutral-vm-terms.md` and the first amendment. They are point-in-time; where they say the helper keeps `droplet_ids`, this note supersedes them.

## Done when

No `write_firewall_aiform_md` call passes `droplet_ids=`. Offline suite green, `ruff` clean.
