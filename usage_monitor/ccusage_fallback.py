"""Fallback data source: parse the ``ccusage`` CLI's JSON output.

``ccusage`` reads local Claude Code logs and emits daily aggregates. We shell
out to it only when the API is unavailable or no key is configured. The parser
is defensive about ccusage's evolving schema.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from datetime import datetime, timezone

from .models import UsageRecord

log = logging.getLogger("usage_monitor.ccusage")


class CcusageError(RuntimeError):
    """Raised when ccusage cannot be run or its output cannot be parsed."""


def is_available(command: str = "ccusage") -> bool:
    """True if the ccusage executable is on PATH."""
    return shutil.which(command) is not None


def run_ccusage(command: str = "ccusage", timeout: float = 30.0) -> str:
    """Run ``ccusage daily --json`` and return raw stdout."""
    if not is_available(command):
        raise CcusageError(f"{command!r} not found on PATH")
    try:
        proc = subprocess.run(
            [command, "daily", "--json"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CcusageError(f"failed to run {command}: {exc}") from exc
    if proc.returncode != 0:
        raise CcusageError(f"{command} exited {proc.returncode}: {proc.stderr.strip()}")
    return proc.stdout


def _parse_date(value: str) -> datetime:
    """Parse a ccusage date (``YYYY-MM-DD`` or ISO) into a UTC datetime."""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        dt = datetime.strptime(value[:10], "%Y-%m-%d")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _records_from_entry(entry: dict) -> list[UsageRecord]:
    """Convert one daily entry (with optional model breakdown) to records."""
    date_str = entry.get("date") or entry.get("timestamp") or entry.get("day")
    if not date_str:
        return []
    ts = _parse_date(str(date_str))

    breakdowns = entry.get("modelBreakdowns") or entry.get("models") or []
    records: list[UsageRecord] = []
    for item in breakdowns:
        if isinstance(item, str):
            # Bare model-name list with no per-model numbers; skip.
            continue
        model = item.get("modelName") or item.get("model") or "unknown"
        records.append(
            UsageRecord(
                timestamp=ts,
                model=model,
                input_tokens=int(item.get("inputTokens", 0) or 0),
                output_tokens=int(item.get("outputTokens", 0) or 0),
                cost=float(item.get("cost", 0.0) or 0.0),
            )
        )

    if not records:
        # No usable breakdown — fall back to the day-level aggregate.
        models = entry.get("modelsUsed") or entry.get("models") or []
        model = models[0] if isinstance(models, list) and models else "claude-unknown"
        records.append(
            UsageRecord(
                timestamp=ts,
                model=str(model),
                input_tokens=int(entry.get("inputTokens", 0) or 0),
                output_tokens=int(entry.get("outputTokens", 0) or 0),
                cost=float(entry.get("totalCost", entry.get("cost", 0.0)) or 0.0),
            )
        )
    return records


def parse_ccusage_json(raw: str) -> list[UsageRecord]:
    """Parse ccusage JSON output into usage records."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CcusageError(f"invalid JSON from ccusage: {exc}") from exc

    if isinstance(data, dict):
        entries = data.get("daily") or data.get("data") or []
    elif isinstance(data, list):
        entries = data
    else:
        entries = []

    records: list[UsageRecord] = []
    for entry in entries:
        if isinstance(entry, dict):
            records.extend(_records_from_entry(entry))
    return records


def fetch_usage(command: str = "ccusage") -> list[UsageRecord]:
    """Run ccusage and return parsed usage records."""
    raw = run_ccusage(command)
    records = parse_ccusage_json(raw)
    log.info("ccusage returned %d usage records", len(records))
    return records
