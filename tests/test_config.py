import json

import pytest

from usage_monitor.config import Config, load_config


def test_defaults():
    cfg = Config()
    assert cfg.refresh_interval == 30
    assert cfg.weekly_cost_cap == 100.0
    assert cfg.use_ccusage_fallback is True


def test_hourly_cap_derived_from_weekly():
    cfg = Config(weekly_cost_cap=168.0)
    assert cfg.effective_hourly_cost_cap == pytest.approx(1.0)


def test_hourly_cap_explicit_override():
    cfg = Config(weekly_cost_cap=168.0, hourly_cost_cap=5.0)
    assert cfg.effective_hourly_cost_cap == 5.0


def test_load_from_json_file(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"refresh_interval": 45, "weekly_cost_cap": 250.0}))
    cfg = load_config(path)
    assert cfg.refresh_interval == 45
    assert cfg.weekly_cost_cap == 250.0


def test_load_missing_file_uses_defaults(tmp_path):
    cfg = load_config(tmp_path / "nope.json")
    assert cfg.refresh_interval == 30


def test_load_invalid_json_uses_defaults(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{ not valid json")
    cfg = load_config(path)
    assert cfg.refresh_interval == 30


def test_env_key_wins_over_file(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"anthropic_api_key": "from-file"}))
    cfg = load_config(path)
    assert cfg.anthropic_api_key == "from-env"


def test_file_key_used_when_no_env(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"anthropic_api_key": "from-file"}))
    cfg = load_config(path)
    assert cfg.anthropic_api_key == "from-file"


def test_refresh_interval_bounds():
    with pytest.raises(ValueError):
        Config(refresh_interval=1)


def test_hourly_token_cap_helper():
    cfg = Config()
    assert cfg.hourly_token_cap(168) == pytest.approx(1.0)
