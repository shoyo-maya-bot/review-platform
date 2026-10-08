"""日次の費用の予約と精算は同じ日付キーで行う（UTC の日付をまたいで LLM の呼び出しが終わっても、予約した日の集計で精算する）."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from conftest import FakeRunner
from review_platform import worker as worker_mod
from review_platform.adapters.llm import FakeLLM, LLMError
from review_platform.adapters.notify import NoNotifier
from review_platform.domain import JobStatus
from review_platform.worker import Worker

EMPTY = '```json\n{"findings": []}\n```'
DAY1, DAY2 = "2026-01-31", "2026-02-01"


class _Clock:
    """worker が見る現在時刻. 23:59:59 から始め、LLM の呼び出しの最中に日付をまたがせる."""

    def __init__(self):
        self.t = datetime(2026, 1, 31, 23, 59, 59, tzinfo=UTC)

    def __call__(self):
        return self.t

    def cross_midnight(self):
        self.t = datetime(2026, 2, 1, 0, 0, 5, tzinfo=UTC)


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(worker_mod, "now", c)
    return c


def _limits(settings):
    return replace(settings, budget_per_job_usd=5.0, budget_daily_usd=10.0, budget_daily_per_owner_usd=10.0)


def test_settlement_uses_reservation_day_across_midnight(settings, svc, clock):
    class SlowCall(FakeLLM):
        def complete(self, *a, **k):
            clock.cross_midnight()  # 呼び出しの最中に UTC の日付が変わる
            return super().complete(*a, **k)

    job = svc.submit("alice", "a.md", "# 設計\n本文".encode())
    Worker(_limits(settings), svc.blobs, svc.queue, svc.repo, SlowCall([EMPTY]), NoNotifier(), FakeRunner()).run_once(wait_seconds=1)
    done = svc.repo.get(job.job_id)
    assert done.status == JobStatus.SUCCEEDED and done.cost_usd > 0
    # 予約した日（DAY1）の集計が実額にそろい、次の日（DAY2）に負の差額が入らない
    assert svc.repo.spent(DAY1, "alice") == pytest.approx((done.cost_usd, done.cost_usd), abs=1e-4)
    assert svc.repo.spent(DAY2, "alice") == (0.0, 0.0)


def test_failed_call_settles_on_reservation_day(settings, svc, clock):
    class CutAfterMidnight(FakeLLM):
        def complete(self, *a, **k):
            clock.cross_midnight()
            raise LLMError("出力が上限で切れました（max_tokens）", cost_usd=0.03)

    job = svc.submit("alice", "a.md", "# 設計\n本文".encode())
    Worker(_limits(settings), svc.blobs, svc.queue, svc.repo, CutAfterMidnight(), NoNotifier(), FakeRunner()).run_once(wait_seconds=1)
    assert svc.repo.get(job.job_id).status == JobStatus.FAILED
    assert svc.repo.spent(DAY1, "alice") == pytest.approx((0.03, 0.03))
    assert svc.repo.spent(DAY2, "alice") == (0.0, 0.0)
