# specs/config.md — `aiform/config.py`

## Purpose

Resolve the DigitalOcean API token per `PLAN.md` §7's resolution order
(env var, then `.aiform/credentials.env`), so `orchestrator.py` can
assemble the `credentials: dict[str, str]` every `ResourceDriver` method
takes. Pure resolution — no scaffolding, no prompting, no writing. This
is the module `CLAUDE.md`'s Credentials rules are about: the fallback
file is hand-edited by the user, never generated or filled in by
`aiform` itself.

## Interface

```python
DEFAULT_CREDENTIALS_PATH = Path(".aiform/credentials.env")

PROVIDER_TOKEN_ENV_VARS: dict[str, str] = {
    "digitalocean": "DIGITALOCEAN_TOKEN",
}


def resolve_credentials(
    provider: str, credentials_path: Path = DEFAULT_CREDENTIALS_PATH
) -> dict[str, str]: ...
```

### `resolve_credentials(provider, credentials_path=DEFAULT_CREDENTIALS_PATH) -> dict[str, str]`

- `provider` must be a key in `PROVIDER_TOKEN_ENV_VARS` — MVP has
  exactly one entry, `"digitalocean"` → `"DIGITALOCEAN_TOKEN"`. Any
  other provider raises `RuntimeError` immediately — same exception
  type as the "neither present" case below, so callers can catch one
  type for "credentials configuration problem" — there's no known env
  var name to look for, so there's nothing to resolve.
- Resolution order for the provider's token (`PLAN.md` §7):
  1. The matching environment variable (`os.environ`), checked first.
  2. Fallback: a `KEY=value` line in `credentials_path`, if the file
     exists.
  3. Neither present (or present but empty) → `RuntimeError` naming
     both the env var name and `credentials_path`, so the error message
     alone tells the user exactly what to set and where.
- Returns `{"<ENV_VAR_NAME>": "<token>"}` — e.g.
  `{"DIGITALOCEAN_TOKEN": "dop_v1_..."}` — matching the `credentials`
  shape every `ResourceDriver` method expects (`PLAN.md` §4).
- Raises a plain `RuntimeError`, not a new custom exception type —
  consistent with `specs/state.md`'s choice to leave custom exception
  types to `exceptions.py`, which isn't built yet.

## Behavior

- Env var set (non-empty) → returned as-is; the credentials file is
  never even read, let alone required to exist.
- Env var unset, `credentials_path` contains a matching `KEY=value`
  line → that value is returned.
- Env var set to `""` (empty string) is treated as not-set and falls
  through to the file check — an empty env var is not a usable token.
- Neither the env var nor a matching file entry is present →
  `RuntimeError`.
- `credentials_path` doesn't exist at all → silently skipped as a
  source (not an error by itself); resolution still succeeds if the env
  var is set, or fails with the same "neither present" error otherwise.
  Implemented by attempting the read and catching `FileNotFoundError`,
  not a separate `.exists()` check beforehand — avoids a TOCTOU gap
  where the file could be removed between an existence check and the
  read.
- The file is read with `encoding="utf-8-sig"`, not plain `"utf-8"` —
  an editor that saves `credentials.env` with a leading UTF-8 BOM (e.g.
  Windows Notepad) must not mangle the first key on the file into
  `"﻿DIGITALOCEAN_TOKEN"`, which would silently fail to match and
  produce a misleading "not found" error despite the value being
  present and visibly correct in the file.
- Dotenv parsing: blank lines and lines starting with `#` are ignored;
  whitespace around the key and value is stripped; a value wrapped in
  matching single or double quotes has them stripped.
- A malformed line in the credentials file (no `=`) is skipped, not
  fatal — this is a small hand-edited text file, and a typo on an
  unrelated line shouldn't break resolution of a token that *is*
  present on a valid line elsewhere in the same file.
- `resolve_credentials("aws")` (or any provider not in
  `PROVIDER_TOKEN_ENV_VARS`) raises immediately, regardless of what
  environment variables or files happen to exist.

## Edge cases / errors

- `credentials_path` pointing at something that isn't a regular file
  (e.g. a directory) is not specially handled — whatever `OSError` the
  filesystem raises propagates as-is. Not worth guarding against; this
  only happens from a deliberately broken `--state-file`-style override,
  and the resulting error is already clear.
- An empty `credentials_path` file (zero bytes) behaves identically to
  a missing one: no keys found, falls through to the "neither present"
  error if the env var is also unset.

## Out of scope

- **`ANTHROPIC_API_KEY`** — resolved directly by the `anthropic` SDK
  client at construction time in `llm.py` (`PLAN.md` §7: "resolved
  automatically by `anthropic.Anthropic()`"). `config.py` never touches
  it.
- **Any CLI flag for supplying credentials** — deliberately never
  built, for either token (`CLAUDE.md`: `ANTHROPIC_API_KEY` "never a
  CLI flag"; DO token has no `--token` flag in `PLAN.md` §6's command
  surface either).
- **Scaffolding or writing `.aiform/credentials.env`** — `aiform init`
  (`cli.py`) prints instructions and the expected filename but never
  writes a value into it or prompts interactively (`CLAUDE.md`,
  non-negotiable).
- **The `--verbose` output redaction helper** (`PLAN.md` §7's `_redact(d)`,
  blanking `*_TOKEN`/`*_KEY`/`credentials` keys before printing
  request/response payloads) — that operates on arbitrary payload
  dicts for verbose logging, which doesn't exist yet in this codebase.
  Belongs to whichever module ends up doing that logging (likely
  `cli.py` or `orchestrator.py`), not `config.py`.
- **Providers beyond `digitalocean`** in `PROVIDER_TOKEN_ENV_VARS` —
  MVP is single-provider; adding an `aws`/`vmware` entry later is a
  small, isolated addition to the dict, not a redesign of this module.
