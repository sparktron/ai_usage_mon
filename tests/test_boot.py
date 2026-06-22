"""End-to-end boot tests.

These start the *real* application through ``run_app`` inside a pseudo-terminal,
let it complete startup + its first refresh + first render, and assert it is
still running (i.e. it did not crash on boot) with no traceback in its output.

Why a PTY subprocess rather than a unit test? The original startup crash (a
cross-thread SQLite access) only surfaced once the real event loop and worker
threads ran — calling the startup helpers directly would not have reproduced
it. The PTY also gives the app a real terminal, so the termios/``raw_terminal``
and ``rich.Live`` screen paths are exercised.

The test deliberately does NOT try to quit the app cleanly. A boot test only
needs to answer "did it survive startup?", so once the app has clearly booted
it is killed with SIGKILL (which is unconditional). A crash, by contrast, makes
the process exit early — on its own — with a traceback, which is what we detect.
"""

from __future__ import annotations

import os
import select
import signal
import time

import pytest

# pty / termios are POSIX-only.
pty = pytest.importorskip("pty")
pytest.importorskip("termios")


def _drain(fd: int, seconds: float = 0.3) -> bytes:
    """Read whatever is available from the master fd for a short while."""
    out = b""
    end = time.time() + seconds
    while time.time() < end:
        ready, _, _ = select.select([fd], [], [], 0.1)
        if not ready:
            continue
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        out += chunk
    return out


def boot_app_in_pty(
    db_path: str, *, use_ccusage: bool, settle: float = 1.5, deadline: float = 10.0
) -> tuple[bool, int | None, str]:
    """Boot run_app in a forked PTY.

    Returns ``(survived, early_exit_code, output)``:
      * survived is True if the app was still running after ``settle`` seconds
        (booted successfully) and was then killed.
      * If the app exited on its own before ``settle`` (a crash or premature
        exit), survived is False and early_exit_code is its exit code.
    """

    def child() -> None:
        import sys

        # pty.fork() puts the slave terminal on fds 0/1/2. Rebind the Python
        # stream objects to it so isatty() is true and the app takes its real
        # terminal code paths (cbreak setup, Live screen mode).
        sys.stdin = os.fdopen(0, "r")
        sys.stdout = os.fdopen(1, "w")
        sys.stderr = os.fdopen(2, "w")

        os.environ.pop("ANTHROPIC_API_KEY", None)
        import asyncio

        from usage_monitor.app import run_app
        from usage_monitor.config import Config

        cfg = Config(db_path=db_path, refresh_interval=5, use_ccusage_fallback=use_ccusage)
        try:
            asyncio.run(run_app(cfg))
            os._exit(0)
        except BaseException:  # noqa: BLE001 — any boot crash must be visible
            import traceback

            traceback.print_exc()
            sys.stderr.flush()
            os._exit(17)

    pid, fd = pty.fork()
    if pid == 0:
        child()
        os._exit(0)  # unreachable

    out = b""
    start = time.time()
    while time.time() - start < settle:
        ready, _, _ = select.select([fd], [], [], 0.1)
        if ready:
            try:
                out += os.read(fd, 65536)
            except OSError:
                pass
        waited, status = os.waitpid(pid, os.WNOHANG)
        if waited == pid:
            # Exited on its own before settling — a crash or premature exit.
            out += _drain(fd)
            return False, os.waitstatus_to_exitcode(status), out.decode(errors="replace")

    # Still alive after settling -> it booted. Kill it unconditionally.
    os.kill(pid, signal.SIGKILL)
    out += _drain(fd)
    os.waitpid(pid, 0)
    return True, None, out.decode(errors="replace")


def test_boots_clean_with_empty_cache(tmp_path):
    """Cold start with an empty cache must not crash — this path runs the
    one-time seed (the original cross-thread SQLite crash site)."""
    survived, code, output = boot_app_in_pty(str(tmp_path / "boot.db"), use_ccusage=True)
    assert "Traceback" not in output, output[-1500:]
    assert survived, f"app exited during boot with code {code}; output:\n{output[-1500:]}"


def test_boots_clean_without_fallback(tmp_path):
    """Boot with the ccusage fallback disabled (API-only, no key) is also clean."""
    survived, code, output = boot_app_in_pty(str(tmp_path / "boot2.db"), use_ccusage=False)
    assert "Traceback" not in output, output[-1500:]
    assert survived, f"app exited during boot with code {code}; output:\n{output[-1500:]}"


def test_boot_paints_dashboard(tmp_path):
    """A successful boot should have painted the dashboard frame."""
    survived, _, output = boot_app_in_pty(str(tmp_path / "boot3.db"), use_ccusage=True)
    assert survived
    assert "USAGE MONITOR" in output, output[-1500:]
