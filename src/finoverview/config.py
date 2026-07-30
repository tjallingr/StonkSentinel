"""Config loading. Everything tunable lives in TOML, nothing in code.

Projection assumptions in particular MUST be config, not constants — a projection
is only as meaningful as its assumptions, and unversioned assumptions are decoration.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]

# Populate os.environ from .env before anything reads it below. Real environment
# variables (e.g. set by systemd) still win — load_dotenv never overrides existing keys.
load_dotenv(REPO_ROOT / ".env")

DEFAULT_CONFIG_DIR = Path(os.environ.get("FINOVERVIEW_CONFIG", REPO_ROOT / "config"))


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Copy the .example.toml next to it and fill it in."
        )
    with path.open("rb") as fh:
        return tomllib.load(fh)


@dataclass(frozen=True)
class Settings:
    raw: dict[str, Any]
    config_dir: Path

    @property
    def db_path(self) -> Path:
        p = Path(self.raw["general"]["db_path"]).expanduser()
        return p if p.is_absolute() else (REPO_ROOT / p)

    @property
    def base_currency(self) -> str:
        return self.raw["general"]["base_currency"].upper()

    @property
    def stale_after_hours(self) -> dict[str, float]:
        return {k: float(v) for k, v in self.raw["general"].get("stale_after_hours", {}).items()}

    @property
    def secrets_dir(self) -> Path:
        p = Path(self.raw["general"].get("secrets_dir", "secrets")).expanduser()
        return p if p.is_absolute() else (REPO_ROOT / p)

    def section(self, name: str) -> dict[str, Any]:
        return self.raw.get(name, {})

    # --- enable banking -------------------------------------------------
    @property
    def eb_app_id(self) -> str:
        return self._env_or("ENABLEBANKING_APP_ID", "enablebanking", "app_id")

    @property
    def eb_private_key(self) -> Path:
        raw = self._env_or("ENABLEBANKING_KEY_PATH", "enablebanking", "private_key_path")
        p = Path(raw).expanduser()
        return p if p.is_absolute() else (REPO_ROOT / p)

    @property
    def eb_redirect_url(self) -> str:
        return self.raw["enablebanking"]["redirect_url"]

    @property
    def eb_banks(self) -> list[dict[str, str]]:
        return self.raw["enablebanking"].get("banks", [])

    # --- saxo -----------------------------------------------------------
    @property
    def saxo(self) -> dict[str, Any]:
        return self.raw["saxo"]

    @property
    def saxo_app_key(self) -> str:
        return self._env_or("SAXO_APP_KEY", "saxo", "app_key")

    @property
    def saxo_app_secret(self) -> str:
        return self._env_or("SAXO_APP_SECRET", "saxo", "app_secret")

    def _env_or(self, env: str, section: str, key: str) -> str:
        val = os.environ.get(env) or self.raw.get(section, {}).get(key, "")
        if not val:
            raise RuntimeError(f"Missing {env} (or [{section}].{key} in settings.toml)")
        return str(val)


@dataclass(frozen=True)
class AssetsConfig:
    raw: dict[str, Any]

    @property
    def assets(self) -> list[dict[str, Any]]:
        return self.raw.get("asset", [])

    @property
    def recurring(self) -> list[dict[str, Any]]:
        return self.raw.get("recurring", [])

    @property
    def cash_flows(self) -> list[dict[str, Any]]:
        """Manually recorded portfolio deposits/withdrawals, for when the broker
        API can't give you them. Needed for correct MWR."""
        return self.raw.get("cash_flow", [])

    @property
    def projection(self) -> dict[str, Any]:
        return self.raw.get("projection", {})

    @property
    def account_overrides(self) -> dict[str, dict[str, Any]]:
        """Keyed by 'provider:external_id'. Lets you mark the KBC collateral
        savings as encumbered without editing code."""
        return self.raw.get("account_override", {})


def load_settings(config_dir: Path | None = None) -> Settings:
    d = config_dir or DEFAULT_CONFIG_DIR
    return Settings(raw=_load(d / "settings.toml"), config_dir=d)


def load_assets(config_dir: Path | None = None) -> AssetsConfig:
    d = config_dir or DEFAULT_CONFIG_DIR
    return AssetsConfig(raw=_load(d / "assets.toml"))
