"""Settings. Priority (low -> high): built-in defaults -> ~/.yi/.env -> ./.env ->
process env vars.

~/.yi/.env is the home for secrets like the API key, so the agent finds them from any
directory. ./.env (current directory) overrides it per checkout.

Only settings the code reads live here. YI_ES_HEAP is read by docker compose, not by
Python, so it is not a field.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError, field_validator

ENV_PREFIX = "YI_"
# Read from .env too, so the key can live there instead of the shell profile.
_UNPREFIXED = {"ANTHROPIC_API_KEY": "anthropic_api_key"}


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    anthropic_api_key: SecretStr | None = None
    # Benchmark data, outside the repo so it is never committed.
    data_dir: Path = Path("~/yi-data")
    es_url: str = "http://127.0.0.1:9200"

    @field_validator("data_dir")
    @classmethod
    def _expand_home(cls, v: Path) -> Path:
        # python-dotenv leaves "~" as is; expand it here so every caller gets a real path.
        return v.expanduser()

    @field_validator("es_url")
    @classmethod
    def _no_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")


class ConfigError(Exception):
    pass


def _from_env(env: Mapping[str, str | None]) -> dict[str, str]:
    fields = Settings.model_fields
    out: dict[str, str] = {}
    for key, value in env.items():
        if not value:  # unset and empty both mean "use the default"
            continue
        if key in _UNPREFIXED:
            out[_UNPREFIXED[key]] = value
            continue
        if not key.startswith(ENV_PREFIX):
            continue
        name = key.removeprefix(ENV_PREFIX).lower()
        if name in fields and name != "anthropic_api_key":
            out[name] = value
    return out


def default_dotenv_paths() -> list[Path]:
    """Lowest priority first."""
    return [Path.home() / ".yi" / ".env", Path(".env")]


def _read_dotenv(path: Path) -> dict[str, str]:
    try:
        return _from_env(dotenv_values(path, encoding="utf-8-sig"))  # -sig: tolerate a BOM
    except UnicodeDecodeError as e:
        # Typically `echo ... > .env` in Windows PowerShell 5.1, which writes UTF-16.
        raise ConfigError(f"{path} is not UTF-8 text; re-save it as UTF-8") from e


def load_settings(
    env: Mapping[str, str] | None = None,
    dotenv_path: Path | list[Path] | None = None,
    *,
    use_default_dotenv: bool = True,
) -> Settings:
    """Merge .env files and environment variables over defaults.

    `dotenv_path` replaces the default .env locations (tests pass explicit files);
    `use_default_dotenv=False` with no path reads no .env at all.
    Raises ConfigError on unreadable files or invalid values.
    """
    if dotenv_path is None:
        paths = default_dotenv_paths() if use_default_dotenv else []
    else:
        paths = dotenv_path if isinstance(dotenv_path, list) else [dotenv_path]
    merged: dict[str, str] = {}
    for path in paths:
        if path.is_file():
            merged |= _read_dotenv(path)
    merged |= _from_env(os.environ if env is None else env)
    try:
        return Settings.model_validate(merged)
    except ValidationError as e:
        raise ConfigError(str(e)) from e
