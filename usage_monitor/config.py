"""Configuration loading for usage-monitor.

Settings come from (in order of precedence): explicit init kwargs, the
``ANTHROPIC_API_KEY`` env var (for the key only), the JSON config file at
``~/.config/usage-monitor/config.json``, then the defaults defined here.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def default_config_path() -> Path:
    return Path.home() / ".config" / "usage-monitor" / "config.json"


def default_db_path() -> Path:
    return Path.home() / ".cache" / "usage-monitor" / "usage.db"


def default_log_path() -> Path:
    return Path.home() / ".cache" / "usage-monitor" / "usage-monitor.log"


class Config(BaseSettings):
    """Runtime configuration. Env vars use the ``USAGE_MONITOR_`` prefix
    except for ``ANTHROPIC_API_KEY`` which is read as-is."""

    model_config = SettingsConfigDict(
        env_prefix="USAGE_MONITOR_", extra="ignore", populate_by_name=True
    )

    # Auth — the key is read from the bare ANTHROPIC_API_KEY env var.
    anthropic_api_key: str | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    api_base_url: str = "https://api.anthropic.com"

    # Refresh / behaviour.
    refresh_interval: int = Field(default=30, ge=5, le=3600)
    use_ccusage_fallback: bool = True
    ccusage_command: str = "ccusage"

    # Quotas (USD).
    weekly_cost_cap: float = Field(default=100.0, gt=0)
    hourly_cost_cap: float | None = None

    # Quotas (tokens) per provider, weekly.
    weekly_token_cap_claude: int = Field(default=500_000, gt=0)
    weekly_token_cap_codex: int = Field(default=200_000, gt=0)

    # Paths (stringified so JSON config is straightforward).
    db_path: Path = Field(default_factory=default_db_path)
    log_path: Path = Field(default_factory=default_log_path)
    # Claude Code OAuth credentials (read-only) for official plan-limit windows.
    credentials_path: Path = Field(
        default_factory=lambda: Path.home() / ".claude" / ".credentials.json"
    )

    @property
    def effective_hourly_cost_cap(self) -> float:
        """Hourly cap, derived from the weekly cap if not set explicitly.

        7 days * 24 hours == 168 hours per week.
        """
        if self.hourly_cost_cap is not None:
            return self.hourly_cost_cap
        return self.weekly_cost_cap / 168.0

    def hourly_token_cap(self, weekly: int) -> float:
        return weekly / 168.0


def load_config(config_path: Path | None = None) -> Config:
    """Load configuration, merging the JSON file with env vars and defaults."""
    path = config_path or default_config_path()
    file_values: dict = {}
    if path.exists():
        try:
            file_values = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            file_values = {}

    # The env var always wins for the secret, so drop any file-supplied key.
    if os.getenv("ANTHROPIC_API_KEY"):
        file_values.pop("anthropic_api_key", None)

    return Config(**file_values)
