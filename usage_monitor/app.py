"""Async TUI application loop.

Wires together the cache, the data sources (API + ccusage fallback), and the
rich Live display. Keyboard input is read non-blocking from stdin so the
refresh task and the UI never stall each other.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from datetime import datetime, timedelta, timezone

from rich.console import Console
from rich.live import Live

from . import ccusage_fallback
from .api import AnthropicUsageClient, ApiError
from .cache import Cache
from .config import Config
from .models import UsageRecord
from .ui import UsageState, VIEWS, render

log = logging.getLogger("usage_monitor.app")


def setup_logging(config: Config) -> None:
    """Log to file only — the TUI owns stdout/stderr."""
    config.log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(config.log_path)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root = logging.getLogger("usage_monitor")
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    root.propagate = False


class DataService:
    """Fetches usage, persists it, and assembles :class:`UsageState`."""

    def __init__(self, config: Config, cache: Cache) -> None:
        self.config = config
        self.cache = cache
        self.last_error: str | None = None
        self.source: str = "—"
        self.last_updated: datetime | None = None

    async def _fetch_records(self) -> tuple[list[UsageRecord], str]:
        """Try the API first, then ccusage. Returns (records, source)."""
        if self.config.anthropic_api_key:
            try:
                async with AnthropicUsageClient(
                    self.config.anthropic_api_key, self.config.api_base_url
                ) as client:
                    records = await client.fetch_usage(days=8)
                return records, "Anthropic API"
            except ApiError as exc:
                log.warning("API fetch failed, will try ccusage: %s", exc)
                if not self.config.use_ccusage_fallback:
                    raise

        records = await asyncio.to_thread(
            ccusage_fallback.fetch_usage, self.config.ccusage_command
        )
        return records, "ccusage"

    async def refresh(self) -> None:
        """Fetch the latest data and store it. Errors are captured, not raised."""
        try:
            records, source = await self._fetch_records()
            if records:
                self.cache.insert_records(records)
            self.source = source
            self.last_error = None
            self.last_updated = datetime.now(timezone.utc)
            log.info("refresh ok via %s (%d records)", source, len(records))
        except Exception as exc:  # noqa: BLE001 — surface any failure in the footer
            self.last_error = str(exc)
            log.error("refresh failed: %s", exc)

    def seed_if_empty(self) -> None:
        """Populate the cache from ccusage on first run when it's empty."""
        if not self.cache.is_empty():
            return
        if not (self.config.use_ccusage_fallback and ccusage_fallback.is_available(
            self.config.ccusage_command
        )):
            return
        try:
            records = ccusage_fallback.fetch_usage(self.config.ccusage_command)
            self.cache.insert_records(records)
            log.info("seeded cache with %d records from ccusage", len(records))
        except ccusage_fallback.CcusageError as exc:
            log.warning("seed from ccusage failed: %s", exc)

    def build_state(self, next_refresh_in: int) -> UsageState:
        now = datetime.now(timezone.utc)
        week_start = now - timedelta(days=7)
        hour_start = now.replace(minute=0, second=0, microsecond=0)
        return UsageState(
            weekly=self.cache.summaries_between(week_start, now),
            last_hour=self.cache.summaries_between(hour_start, now + timedelta(hours=1)),
            daily_buckets=self.cache.daily_buckets(7, now),
            hourly_buckets=self.cache.hourly_buckets(24, now),
            recent=self.cache.recent_records(200),
            last_updated=self.last_updated,
            last_error=self.last_error,
            source=self.source,
            next_refresh_in=next_refresh_in,
        )


@contextlib.contextmanager
def raw_terminal():
    """Put stdin into cbreak mode so single keypresses arrive immediately."""
    if not sys.stdin.isatty():
        yield None
        return
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield fd
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


class App:
    """Top-level application coordinating refresh, input, and rendering."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.console = Console()
        self.cache = Cache(config.db_path)
        self.service = DataService(config, self.cache)
        self.view = "dashboard"
        self.running = True
        self._refresh_event = asyncio.Event()
        self._seconds_to_refresh = config.refresh_interval

    def handle_key(self, key: str) -> None:
        key = key.lower()
        if key == "q":
            self.running = False
        elif key == "r":
            self._refresh_event.set()
        elif key in ("d", "h", "w") or key in ("j", "k", "l"):
            mapping = {"d": "dashboard", "w": "weekly", "h": "hourly"}
            if key in mapping:
                self.view = mapping[key]
            else:
                # j/k/l cycle through views
                idx = VIEWS.index(self.view)
                step = 1 if key in ("l", "j") else -1
                self.view = VIEWS[(idx + step) % len(VIEWS)]
        elif key == "\x1b":  # arrow keys send escape sequences; cycle forward
            idx = VIEWS.index(self.view)
            self.view = VIEWS[(idx + 1) % len(VIEWS)]

    async def _input_loop(self) -> None:
        if not sys.stdin.isatty():
            return
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[str] = asyncio.Queue()
        loop.add_reader(sys.stdin.fileno(), lambda: queue.put_nowait(sys.stdin.read(1)))
        try:
            while self.running:
                key = await queue.get()
                self.handle_key(key)
        finally:
            with contextlib.suppress(Exception):
                loop.remove_reader(sys.stdin.fileno())

    async def _refresh_loop(self, live: Live) -> None:
        await self.service.refresh()
        live.update(render(self.service.build_state(self._seconds_to_refresh),
                           self.view, self.config))
        while self.running:
            try:
                await asyncio.wait_for(self._refresh_event.wait(),
                                       timeout=self.config.refresh_interval)
            except asyncio.TimeoutError:
                pass
            self._refresh_event.clear()
            self._seconds_to_refresh = self.config.refresh_interval
            await self.service.refresh()

    async def _tick_loop(self, live: Live) -> None:
        """Re-render once a second so the countdown and view changes show."""
        while self.running:
            live.update(
                render(
                    self.service.build_state(self._seconds_to_refresh),
                    self.view,
                    self.config,
                )
            )
            await asyncio.sleep(1)
            self._seconds_to_refresh = max(0, self._seconds_to_refresh - 1)

    async def run(self) -> None:
        self.cache.prune_older_than(30)
        await asyncio.to_thread(self.service.seed_if_empty)
        with raw_terminal(), Live(
            console=self.console, screen=True, auto_refresh=True,
            refresh_per_second=4, transient=True
        ) as live:
            tasks = [
                asyncio.create_task(self._refresh_loop(live)),
                asyncio.create_task(self._tick_loop(live)),
                asyncio.create_task(self._input_loop()),
            ]
            try:
                while self.running:
                    await asyncio.sleep(0.1)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        self.cache.close()


async def run_app(config: Config) -> None:
    setup_logging(config)
    app = App(config)
    await app.run()
