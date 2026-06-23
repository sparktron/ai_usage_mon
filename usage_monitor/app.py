"""Async TUI application loop.

Wires together the cache, the data sources (API + ccusage fallback), and the
rich Live display. Keyboard input is read non-blocking from stdin so the
refresh task and the UI never stall each other.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone

from rich.console import Console
from rich.live import Live

from . import ccusage_fallback, oauth_usage
from .api import AnthropicUsageClient, ApiError
from .cache import Cache
from .config import Config
from .models import UsageRecord
from .oauth_usage import LimitWindow, OAuthUsageError, RateLimited, TokenExpired
from .ui import UsageState, VIEW_KEYS, VIEWS, render

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
        # Official plan-limit windows (Path A-lite).
        self.windows: list[LimitWindow] = []
        self.plan: str | None = None
        self.windows_error: str | None = None
        # Monotonic deadline before which we skip the windows fetch entirely,
        # so a 429 doesn't make us keep hammering a rate-limited endpoint.
        self._windows_backoff_until: float = 0.0
        self._windows_backoff: float = 0.0

    def _ratelimit_message(self, wait: int) -> str:
        """Rate-limit notice. Only promises last-known values when we actually
        have some (a cold start that 429s has nothing cached to show)."""
        if self.windows:
            return (
                f"Plan usage rate-limited — retrying in {wait}s "
                "(showing last-known values)."
            )
        return f"Plan usage rate-limited — no data yet, retrying in {wait}s."

    async def _fetch_windows(self) -> None:
        """Fetch the official 5h/weekly plan-limit windows (read-only token).
        Failures are captured into windows_error, not raised, and never clear
        the last-known windows."""
        now = time.monotonic()
        if now < self._windows_backoff_until:
            # Still rate-limited; keep last-known windows, don't poll.
            wait = int(self._windows_backoff_until - now) + 1
            self.windows_error = self._ratelimit_message(wait)
            return
        try:
            windows = await asyncio.to_thread(
                oauth_usage.fetch_usage, self.config.credentials_path
            )
            self.windows = windows
            self.plan = oauth_usage.read_plan(self.config.credentials_path)
            self.windows_error = None
            self._windows_backoff = 0.0
        except TokenExpired:
            self.windows_error = (
                "Claude session token expired — run any Claude Code command to "
                "refresh it, then this updates automatically."
            )
            log.info("oauth usage: token expired")
        except RateLimited as exc:
            # Exponential backoff capped at 10 minutes, floored so we never
            # busy-retry a throttled endpoint. Retry-After is honored only when
            # it asks us to wait *longer* than our own backoff.
            self._windows_backoff = min(
                max(self._windows_backoff * 2, float(self.config.refresh_interval)),
                600.0,
            )
            delay = self._windows_backoff
            if exc.retry_after is not None:
                delay = max(delay, exc.retry_after)
            self._windows_backoff_until = time.monotonic() + delay
            self.windows_error = self._ratelimit_message(int(delay))
            log.info("oauth usage rate-limited; backing off %ss", int(delay))
        except OAuthUsageError as exc:
            self.windows_error = f"Plan usage unavailable: {exc}"
            log.warning("oauth usage failed: %s", exc)

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
        # The official plan-limit windows are the headline; fetch them first
        # and independently so a ccusage hiccup can't hide them (or vice versa).
        await self._fetch_windows()
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
        hour_start = now - timedelta(hours=1)
        return UsageState(
            weekly=self.cache.summaries_between(week_start, now),
            last_hour=self.cache.summaries_between(hour_start, now),
            daily_buckets=self.cache.daily_buckets(7, now),
            hourly_buckets=self.cache.hourly_buckets(24, now),
            recent=self.cache.recent_records(200),
            last_updated=self.last_updated,
            last_error=self.last_error,
            source=self.source,
            next_refresh_in=next_refresh_in,
            windows=self.windows,
            plan=self.plan,
            windows_error=self.windows_error,
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
        self._view_by_key = {key: name for name, key in VIEW_KEYS.items()}

    def handle_key(self, key: str) -> None:
        key = key.lower()
        if key == "q":
            self.running = False
        elif key == "r":
            self._refresh_event.set()
        elif key in self._view_by_key:
            self.view = self._view_by_key[key]
        elif key in ("j", "k", "l"):
            # j/k/l cycle through views
            idx = VIEWS.index(self.view)
            step = 1 if key in ("l", "j") else -1
            self.view = VIEWS[(idx + step) % len(VIEWS)]

    async def _input_loop(self) -> None:
        if not sys.stdin.isatty():
            return
        fd = sys.stdin.fileno()
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes] = asyncio.Queue()
        # Read whole chunks so multi-byte escape sequences (arrow keys, mouse
        # wheel) arrive together and can be ignored as a unit rather than having
        # their leading 0x1b or trailing letters misread as hotkeys.
        loop.add_reader(fd, lambda: queue.put_nowait(os.read(fd, 1024)))
        try:
            while self.running:
                chunk = await queue.get()
                # Ignore escape sequences entirely; only literal keypresses
                # change the view.
                if chunk.startswith(b"\x1b"):
                    continue
                for byte in chunk.decode(errors="ignore"):
                    self.handle_key(byte)
        finally:
            with contextlib.suppress(Exception):
                loop.remove_reader(sys.stdin.fileno())

    def _paint(self, live: Live) -> None:
        """Render the current state and flush it to the screen immediately.
        ``live.update`` alone only stages the frame for the next auto-refresh
        tick; ``refresh`` forces it out now so the very first frame can't be
        lost to a slow startup or an early exit."""
        live.update(render(self.service.build_state(self._seconds_to_refresh),
                           self.view, self.config))
        live.refresh()

    async def _refresh_loop(self, live: Live) -> None:
        await self.service.refresh()
        self._paint(live)
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
        with raw_terminal(), Live(
            console=self.console, screen=True, auto_refresh=True,
            refresh_per_second=4, transient=True
        ) as live:
            # Paint an initial frame at once, before the (potentially slow)
            # ccusage seed and the first network refresh, so the dashboard
            # appears immediately rather than blocking on a subprocess.
            self._paint(live)
            # Seeding touches the cache, so it must run on this (the
            # connection's) thread; it runs once, after the first paint.
            self.service.seed_if_empty()
            self._paint(live)
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
