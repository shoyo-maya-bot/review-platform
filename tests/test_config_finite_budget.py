"""数値の設定（費用の上限など）は有限の値だけを受け付ける. inf・nan を通すと上限の確認が効かなくなる."""

from __future__ import annotations

import pytest

from review_platform.config import ConfigError, Settings


@pytest.mark.parametrize("name", ["RP_BUDGET_PER_JOB_USD", "RP_BUDGET_DAILY_USD", "RP_BUDGET_DAILY_PER_OWNER_USD"])
@pytest.mark.parametrize("raw", ["inf", "Infinity", "-inf", "nan", "NaN", "1e400"])
def test_non_finite_budget_is_a_config_error(monkeypatch, name, raw):
    monkeypatch.setenv(name, raw)
    with pytest.raises(ConfigError, match=name):
        Settings.from_env()


def test_finite_budget_is_accepted(monkeypatch):
    monkeypatch.setenv("RP_BUDGET_PER_JOB_USD", "0.5")
    monkeypatch.setenv("RP_BUDGET_DAILY_USD", "1e2")
    s = Settings.from_env()
    assert s.budget_per_job_usd == 0.5 and s.budget_daily_usd == 100.0
