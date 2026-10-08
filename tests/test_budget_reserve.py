"""第 3 回修正: 日次の上限を複数のワーカーで超えない（呼び出しの前に見積もり額を共有の集計に予約し、終わったら実額で精算する）・
auto-review の over_budget の扱い・差分の見積もりの伏せ字."""

from __future__ import annotations

import contextlib
import json
import threading
from dataclasses import replace

import pytest

from conftest import FakeRunner
from review_platform.adapters.llm import FakeLLM, LLMError, Masker
from review_platform.adapters.notify import NoNotifier
from review_platform.domain import JobStatus
from review_platform.worker import Worker, _CostMeter, _today

DIFF = """diff --git a/src/main/java/x/MemberService.java b/src/main/java/x/MemberService.java
--- a/src/main/java/x/MemberService.java
+++ b/src/main/java/x/MemberService.java
@@ -1,2 +1,2 @@
-    void a() {}
+    @Transactional void a() {}
"""
EMPTY = '```json\n{"findings": []}\n```'
DOC = "# 設計\n本文".encode()


def _worker(settings, svc, llm, repo=None):
    return Worker(settings, svc.blobs, svc.queue, repo or svc.repo, llm, NoNotifier(), FakeRunner())


class _Rendezvous(FakeLLM):
    """2 台のワーカーが同時に呼び出しに入ったところで待ち合わせる（以前は日次の集計を読んでから呼ぶまでの間に、両方とも通っていた）.
    返す費用は 0.9 に固定（精算の後でも、もう 1 回分の予約 1.0 は上限 1.5 に入らない. 待ち合わせの時間に依らず決まる）."""

    def __init__(self):
        super().__init__([EMPTY])
        self.barrier = threading.Barrier(2, timeout=2)
        self.lock = threading.Lock()

    def complete(self, model, system, user, max_tokens, schema=None):
        with contextlib.suppress(threading.BrokenBarrierError):  # 相手が来ない（予約できずに呼ばなかった）
            self.barrier.wait()
        with self.lock:
            r = super().complete(model, system, user, max_tokens, schema=schema)
        return replace(r, cost_usd=0.9)


def test_two_workers_cannot_both_pass_daily_limit(settings, svc, monkeypatch):
    """日次の上限（全体）に 1 回分の見積もりしか入らないとき、2 台が同時に処理しても LLM を呼ぶのは 1 回だけ."""
    monkeypatch.setattr(_CostMeter, "estimate", lambda self, *a: 1.0, raising=False)
    s = replace(settings, budget_per_job_usd=5.0, budget_daily_usd=1.5, budget_daily_per_owner_usd=0.0)
    jobs = [svc.submit(o, "a.md", DOC) for o in ("alice", "bob")]
    llm = _Rendezvous()
    ts = [threading.Thread(target=_worker(s, svc, llm).run_once, kwargs={"wait_seconds": 1}) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=30)
    done = [svc.repo.get(j.job_id) for j in jobs]
    assert len(llm.calls) == 1, "日次の上限を 2 台で超えて呼んだ"
    assert sorted(d.status for d in done) == [JobStatus.FAILED, JobStatus.SUCCEEDED]
    failed = next(d for d in done if d.status == JobStatus.FAILED)
    assert "上限" in failed.error and "全体" in failed.error
    # 精算の後の集計は実額（予約した見積もり 1.0 のままではない）
    assert svc.repo.spent(_today(), "alice")[0] == pytest.approx(0.9)


def test_reservation_is_settled_to_actual_cost(settings, svc):
    s = replace(settings, budget_per_job_usd=5.0, budget_daily_usd=10.0, budget_daily_per_owner_usd=10.0)
    job = svc.submit("alice", "a.md", DOC)

    seen = []

    class Spy(FakeLLM):
        def complete(self, *a, **k):
            seen.append(svc.repo.spent(_today(), "alice"))  # 呼び出しの最中は見積もり額が予約されている
            return super().complete(*a, **k)

    _worker(s, svc, Spy([EMPTY])).run_once(wait_seconds=1)
    done = svc.repo.get(job.job_id)
    assert done.status == JobStatus.SUCCEEDED
    assert seen and seen[0][0] > done.cost_usd > 0  # 予約（出力は上限いっぱい）は実額より大きい
    assert svc.repo.spent(_today(), "alice") == pytest.approx((done.cost_usd, done.cost_usd), abs=1e-4)


def test_cut_call_reservation_is_settled_to_billed_cost(settings, svc):
    """max_tokens で切れた呼び出しは、課金された額で精算する."""
    s = replace(settings, budget_per_job_usd=5.0, budget_daily_usd=10.0, budget_daily_per_owner_usd=10.0)

    class Cut(FakeLLM):
        def complete(self, model, system, user, max_tokens, schema=None):
            self.calls.append({})
            raise LLMError("出力が上限で切れました（max_tokens）", cost_usd=0.03)

    job = svc.submit("alice", "a.md", DOC)
    _worker(s, svc, Cut()).run_once(wait_seconds=1)
    assert svc.repo.get(job.job_id).status == JobStatus.FAILED
    assert svc.repo.spent(_today(), "alice") == pytest.approx((0.03, 0.03))


def test_reservation_store_failure_is_retryable_and_does_not_call(settings, svc):
    s = replace(settings, budget_per_job_usd=5.0, budget_daily_usd=10.0, budget_daily_per_owner_usd=10.0)

    class Broken:
        def __init__(self, inner):
            self.inner = inner

        def reserve_spend(self, *a):
            raise RuntimeError("throttled")

        def __getattr__(self, n):
            return getattr(self.inner, n)

    job = svc.submit("alice", "a.md", DOC)
    llm = FakeLLM([EMPTY])
    _worker(s, svc, llm, Broken(svc.repo)).run_once(wait_seconds=1)
    done = svc.repo.get(job.job_id)
    assert llm.calls == [] and done.status == JobStatus.QUEUED and "再試行" in done.error


def test_no_reservation_without_daily_limits(settings, svc):
    """日次の上限が無い（無料の LLM で 0）ときは予約しない. 呼んだ後に実額を足す."""

    class NoReserve:
        def __init__(self, inner):
            self.inner = inner

        def reserve_spend(self, *a):
            raise AssertionError("予約しないはず")

        def __getattr__(self, n):
            return getattr(self.inner, n)

    job = svc.submit("alice", "a.md", DOC)
    _worker(settings, svc, FakeLLM([EMPTY]), NoReserve(svc.repo)).run_once(wait_seconds=1)
    done = svc.repo.get(job.job_id)
    assert done.status == JobStatus.SUCCEEDED and svc.repo.spent(_today(), "alice")[0] == pytest.approx(done.cost_usd, abs=1e-4)


def test_estimate_counts_masked_text():
    raw = "連絡先 taro.yamada.longaddress@example.com 090-1234-5678 " * 50
    plain = _CostMeter(FakeLLM()).estimate("claude-sonnet-5-5", "s", raw, 10)
    masked = _CostMeter(FakeLLM(masker=Masker())).estimate("claude-sonnet-5-5", "s", raw, 10)
    assert masked != plain


# ---- auto-review が予算超過で呼ばなかった（over_budget: true）とき


def test_auto_review_over_budget_fails_job_with_reason(settings, svc, monkeypatch):
    """auto-review が予算超過の見込みで呼ばなかったら、指摘 0 件の成功にせず、理由を残して failed にする."""
    from review_platform.pipeline import diffreview

    monkeypatch.setattr(diffreview, "estimate_first_call", lambda *a, **k: 0.0)  # こちらの事前の確認をすり抜けた場合
    s = replace(settings, budget_per_job_usd=0.001)
    job = svc.submit("alice", "pr.diff", DIFF.encode())
    llm = FakeLLM([EMPTY])
    _worker(s, svc, llm).run_once(wait_seconds=1)
    done = svc.repo.get(job.job_id)
    assert llm.calls == []
    assert done.status == JobStatus.FAILED and "予算" in done.error and "AI レビューを行いませんでした" in done.error
    with pytest.raises(Exception):  # noqa: B017 - 失敗したジョブの結果（指摘 0 件）は保存しない
        json.loads(svc.blobs.get(done.report_json_key))


def test_diff_backend_passes_masker_to_auto_review():
    """auto-review の見積もりは、実際に送るのと同じ伏せ字（RP_MASK_PII）の後の文字列で数える."""
    from auto_review.review import Reviewer

    from review_platform.pipeline.diffreview import _Backend, build_config

    m = Masker()
    r = Reviewer(build_config("claude-sonnet-5-5", "claude-opus-5-5", 1.0), _Backend(FakeLLM(masker=m)))
    assert r.masker is m
    assert Reviewer(build_config("claude-sonnet-5-5", "", 1.0), _Backend(FakeLLM())).masker is None  # RP_MASK_PII=0 なら伏せない
