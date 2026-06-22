"""CLI entry point for usage-monitor."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import click

from .app import run_app, setup_logging
from .config import load_config


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Path to config.json (default: ~/.config/usage-monitor/config.json).",
)
@click.option(
    "--refresh",
    type=click.IntRange(5, 3600),
    default=None,
    help="Refresh interval in seconds (overrides config).",
)
@click.option("--debug", is_flag=True, help="Log every refresh cycle to the log file.")
@click.version_option(package_name="usage-monitor")
def main(config_path: Path | None, refresh: int | None, debug: bool) -> None:
    """Real-time TUI for monitoring Claude and Codex API usage."""
    config = load_config(config_path)
    if refresh is not None:
        config.refresh_interval = refresh

    if debug:
        setup_logging(config)
        logging.getLogger("usage_monitor").setLevel(logging.DEBUG)

    try:
        asyncio.run(run_app(config))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
