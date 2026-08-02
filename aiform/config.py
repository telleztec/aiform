import os
from pathlib import Path

DEFAULT_CREDENTIALS_PATH = Path(".aiform/credentials.env")

PROVIDER_TOKEN_ENV_VARS: dict[str, str] = {
    "digitalocean": "DIGITALOCEAN_TOKEN",
}


def _parse_dotenv(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def resolve_credentials(
    provider: str, credentials_path: Path = DEFAULT_CREDENTIALS_PATH
) -> dict[str, str]:
    if provider not in PROVIDER_TOKEN_ENV_VARS:
        raise RuntimeError(
            f"unsupported provider {provider!r}; no credential env var is known for it"
        )
    env_var = PROVIDER_TOKEN_ENV_VARS[provider]

    value = os.environ.get(env_var)
    if not value:
        try:
            content = credentials_path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            content = None
        if content is not None:
            value = _parse_dotenv(content).get(env_var)

    if not value:
        raise RuntimeError(
            f"{env_var} not found: set the {env_var} environment variable, "
            f"or add it to {credentials_path}"
        )

    return {env_var: value}
