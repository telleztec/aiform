import os
from pathlib import Path
from typing import Any

import yaml

from aiform.models import LLMConfig, LLMRoleConfig, ModelSource

DEFAULT_CREDENTIALS_PATH = Path(".aiform/credentials.env")

PROVIDER_TOKEN_ENV_VARS: dict[str, str] = {
    "digitalocean": "DIGITALOCEAN_TOKEN",
}

DEFAULT_LLM_CONFIG_PATH = Path(".aiform/config.yaml")

DEFAULT_LLM_CONFIG = LLMConfig(
    intent_orchestration=LLMRoleConfig(
        source=ModelSource.ANTHROPIC, model="claude-sonnet-5", max_tokens=4096
    ),
    code_generator=LLMRoleConfig(
        source=ModelSource.ANTHROPIC, model="claude-sonnet-5", max_tokens=4096
    ),
    code_review=LLMRoleConfig(source=ModelSource.ANTHROPIC, model="claude-opus-5", max_tokens=8192),
    review_orchestration=LLMRoleConfig(
        source=ModelSource.ANTHROPIC, model="claude-opus-5", max_tokens=8192
    ),
)


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


def _merge_role(default: LLMRoleConfig, override: dict) -> LLMRoleConfig:
    source = override.get("source")
    model = override.get("model")
    max_tokens = override.get("max_tokens")
    return LLMRoleConfig(
        source=source if source is not None else default.source,
        model=model if model is not None else default.model,
        max_tokens=max_tokens if max_tokens is not None else default.max_tokens,
    )


def _require_mapping(value: Any, key: str, config_path: Path) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{config_path}: {key!r} must be a mapping, got {type(value).__name__}")
    return value


def resolve_llm_config(config_path: Path = DEFAULT_LLM_CONFIG_PATH) -> LLMConfig:
    try:
        content = config_path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return DEFAULT_LLM_CONFIG

    data = yaml.safe_load(content)
    if data is None:
        data = {}
    data = _require_mapping(data, "<top level>", config_path)

    llm_data = data.get("llm")
    if llm_data is None:
        llm_data = {}
    llm_data = _require_mapping(llm_data, "llm", config_path)

    roles: dict[str, LLMRoleConfig] = {}
    for role_name in LLMConfig.model_fields:
        role_value = llm_data.get(role_name)
        if role_value is None:
            role_value = {}
        role_override = _require_mapping(role_value, f"llm.{role_name}", config_path)
        roles[role_name] = _merge_role(getattr(DEFAULT_LLM_CONFIG, role_name), role_override)

    return LLMConfig(**roles)
