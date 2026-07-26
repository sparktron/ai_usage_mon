# AGENTS.md

Guidance for coding agents working in this repository. Single source of truth;
`CLAUDE.md` imports it.

## What this is

`usage-monitor` — a standalone Python CLI that polls Claude and Codex API usage
and renders weekly/hourly breakdowns in a Rich TUI. Package name on disk is
`usage_monitor`; distribution name is `usage-monitor`.

## Layout

| Module | Role |
|---|---|
| `usage_monitor/__main__.py` | Entry point — `usage-monitor = "usage_monitor.__main__:main"` |
| `usage_monitor/app.py` | Application loop / orchestration |
| `usage_monitor/ui.py` | Rich TUI rendering |
| `usage_monitor/api.py` | HTTP client (httpx) |
| `usage_monitor/oauth_usage.py` | OAuth-based usage source |
| `usage_monitor/ccusage_fallback.py` | Fallback source when the API path is unavailable |
| `usage_monitor/cache.py` | Response caching |
| `usage_monitor/models.py` | pydantic models |
| `usage_monitor/config.py` | pydantic-settings config |

Two data sources exist — the OAuth/API path and the `ccusage` fallback. The TUI
header reports which one produced the current frame. Changes to one source
should keep the other's contract intact.

## Commands

```bash
pip install -e ".[dev]"
pytest                    # coverage is on by default via addopts
pytest -q                 # what CI runs
```

`pyproject.toml` sets `asyncio_mode = "auto"` — async tests need no decorator.
Coverage is wired through `addopts` with `--cov=usage_monitor`;
`__main__.py` and `app.py` are omitted from the coverage report.

`respx` is the HTTP mocking layer. Mock at the transport level rather than
monkeypatching `api.py` internals.

## CI

`.github/workflows/test.yml` runs `pytest -q` on Python **3.10, 3.11, 3.12**.
`requires-python = ">=3.10"`. There is no lint job — ruff is not wired up here
(unlike `dsync`), so don't assume formatting is enforced.

## Secrets

This tool reads API credentials. Never log token values, never write them to
the cache layer in plaintext, and keep them out of TUI output and error
messages — the screen is frequently screenshotted for the README.
