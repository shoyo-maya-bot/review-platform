"""費用の上限の既定は 0. 0 は「有料の LLM（anthropic・bedrock）を一切呼ばない」（上限を明示的に設定したときだけ呼ぶ）.

疑似 LLM（fake）は費用 0 なので、上限 0 のままでも今まで通り動く. 本物の窓口は使わない（名前だけ有料の LLM にした FakeLLM で試す）.
"""

from __future__ import annotations

import logging
from dataclasses import replace

import pytest

from conftest import FakeRunner
from review_platform.adapters.llm import FakeLLM
from review_platform.adapters.notify import NoNotifier
from review_platform.config import PAID_LLMS, Settings
from review_platform.domain import JobStatus
from review_platform.worker import Worker, _today

DIFF = """diff --git a/src/main/java/x/MemberService.java b/src/main/java/x/MemberService.java
--- a/src/main/java/x/MemberService.java
+++ b/src/main/java/x/MemberService.java
@@ -1,2 +1,2 @@
-    void a() {}
+    @Transactional void a() {}
"""
EMPTY = '```json\n{"findings": []}\n```'
BUDGETS = ("RP_BUDGET_PER_JOB_USD", "RP_BUDGET_DAILY_USD", "RP_BUDGET_DAILY_PER_OWNER_USD")
ALL_SET = {"budget_per_job_usd": 1.0, "budget_daily_usd": 10.0, "budget_daily_per_owner_usd": 2.0}


class PaidStub(FakeLLM):
    """名前だけ有料の LLM. 呼ばれたら記録する（通信はしない）."""

    def __init__(self, name: str = "anthropic", replies: list[str] | None = None):
        super().__init__(replies or [EMPTY])
        self.name = name


DOC = "# 設計\n本文".encode()


def _run(settings, svc, llm, filename="a.md", body=DOC):
    job = svc.submit("alice", filename, body)
    Worker(settings, svc.blobs, svc.queue, svc.repo, llm, NoNotifier(), FakeRunner()).run_once(wait_seconds=1)
    return svc.repo.get(job.job_id)


def test_defaults_are_zero(monkeypatch):
    for n in BUDGETS:
        monkeypatch.delenv(n, raising=False)
    for s in (Settings(), Settings.from_env()):
        assert (s.budget_per_job_usd, s.budget_daily_usd, s.budget_daily_per_owner_usd) == (0.0, 0.0, 0.0)
        assert s.unset_budgets() == list(BUDGETS)


def test_paid_llms():
    assert {"anthropic", "bedrock"} == PAID_LLMS


@pytest.mark.parametrize("name", sorted(PAID_LLMS))
@pytest.mark.parametrize(("filename", "body"), [("a.md", DOC), ("pr.diff", DIFF.encode())])
def test_paid_llm_is_not_called_with_default_budgets(settings, svc, name, filename, body):
    """既定（上限がすべて 0）では、有料の LLM を一度も呼ばずに failed にする（設計書・差分の両方）."""
    llm = PaidStub(name)
    done = _run(settings, svc, llm, filename, body)
    assert llm.calls == []
    assert done.status == JobStatus.FAILED and done.cost_usd == 0
    assert all(n in done.error for n in BUDGETS) and "有料の LLM" in done.error
    assert svc.repo.spent(_today(), "alice") == (0, 0)


@pytest.mark.parametrize("unset", ["budget_per_job_usd", "budget_daily_usd", "budget_daily_per_owner_usd"])
def test_paid_llm_needs_every_budget_set(settings, svc, unset):
    """1 つでも 0 のままなら呼ばない（0 を「上限なし」と扱わない）."""
    s = replace(settings, **{**ALL_SET, unset: 0.0})
    llm = PaidStub()
    done = _run(s, svc, llm)
    env = "RP_" + unset.upper()
    assert llm.calls == [] and done.status == JobStatus.FAILED and env in done.error
    assert [n for n in BUDGETS if n in done.error] == [env]


def test_paid_llm_runs_when_all_budgets_are_set(settings, svc):
    s = replace(settings, **ALL_SET)
    llm = PaidStub()
    done = _run(s, svc, llm, "pr.diff", DIFF.encode())
    assert len(llm.calls) == 1 and done.status == JobStatus.SUCCEEDED and done.cost_usd > 0


@pytest.mark.parametrize(("filename", "body"), [("a.md", DOC), ("pr.diff", DIFF.encode())])
def test_fake_llm_runs_with_zero_budgets(settings, svc, filename, body):
    """疑似 LLM（無料）は、上限がすべて 0（既定）でも今まで通り動く."""
    assert settings.unset_budgets() == list(BUDGETS)
    llm = FakeLLM([EMPTY])
    done = _run(settings, svc, llm, filename, body)
    assert len(llm.calls) == 1 and done.status == JobStatus.SUCCEEDED


def test_none_llm_still_converts_with_zero_budgets(settings, svc):
    from review_platform.adapters.llm import NoLLM

    done = _run(replace(settings, llm="none"), svc, NoLLM())
    assert done.status == JobStatus.SUCCEEDED


def test_worker_start_warns_when_paid_llm_is_disabled(settings, svc, caplog, monkeypatch):
    w = Worker(settings, svc.blobs, svc.queue, svc.repo, PaidStub(), NoNotifier(), FakeRunner())
    monkeypatch.setattr("review_platform.worker.signal.signal", lambda *a: None)
    w.stopping.set()
    with caplog.at_level(logging.WARNING):
        w.run_forever()
    assert "paid LLM disabled: budget not set" in caplog.text
