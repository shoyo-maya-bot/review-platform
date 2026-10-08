"""第 2 回修正: 差分の一次呼び出しの予算確認・max_tokens で切れた呼び出しの費用・日次の上限・期限切れのファイルの削除."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from conftest import FakeRunner
from review_platform.adapters.llm import FakeLLM, LLMError
from review_platform.adapters.notify import NoNotifier
from review_platform.config import Settings
from review_platform.domain import JobStatus
from review_platform.ports import LLMReply
from review_platform.worker import Worker, _CostMeter, _today

DIFF = """diff --git a/src/main/java/x/MemberService.java b/src/main/java/x/MemberService.java
--- a/src/main/java/x/MemberService.java
+++ b/src/main/java/x/MemberService.java
@@ -1,2 +1,2 @@
-    void a() {}
+    @Transactional void a() {}
"""
EMPTY = '```json\n{"findings": []}\n```'
MUST = '```json\n{"findings": [{"severity": "MUST", "location": "1 章", "title": "t", "detail": "d", "evidence": "e"}]}\n```'


def _worker(settings, svc, llm):
    return Worker(settings, svc.blobs, svc.queue, svc.repo, llm, NoNotifier(), FakeRunner())


# ---- 7. 差分ジョブの一次呼び出しの前に予算を確かめる


@pytest.mark.parametrize("budget", [0.0001, 0.001])
def test_diff_job_does_not_call_llm_over_budget(settings, svc, budget):
    """見積もりより小さい予算では一次の呼び出しもしない（以前は予算が小さくても約 $0.20 使っていた）.
    予算 0 は「有料の LLM を呼ばない」意味になった（tests/test_budget_zero.py）."""
    s = replace(settings, budget_per_job_usd=budget)
    job = svc.submit("alice", "pr.diff", DIFF.encode())
    llm = FakeLLM([EMPTY])
    _worker(s, svc, llm).run_once(wait_seconds=1)
    done = svc.repo.get(job.job_id)
    assert llm.calls == []
    assert done.status == JobStatus.FAILED and "見積もり" in done.error and done.cost_usd == 0


def test_diff_estimate_is_close_to_first_call():
    from auto_review.diff import parse_diff
    from auto_review.router import plan

    from review_platform.pipeline.diffreview import build_config, estimate_first_call

    cfg = build_config("claude-sonnet-5-5", "claude-opus-5-5", 1.0)
    changes = parse_diff(DIFF)
    est = estimate_first_call(cfg, DIFF, changes, plan(changes, cfg).risky_files)
    # 出力 4000 トークンを上限いっぱいとみなすので、Sonnet でも数セント〜. 0 にはならない
    assert 0.01 < est < 0.5


def test_diff_job_within_budget_still_runs(settings, svc):
    job = svc.submit("alice", "pr.diff", DIFF.encode())
    llm = FakeLLM([EMPTY])
    _worker(settings, svc, llm).run_once(wait_seconds=1)
    assert len(llm.calls) == 1 and svc.repo.get(job.job_id).status == JobStatus.SUCCEEDED


# ---- 8. max_tokens で切れた呼び出しの費用も数える


class _CutLLM(FakeLLM):
    """最初の呼び出しは出力が max_tokens で切れる（課金はされる）. 以降は普通に返す."""

    def __init__(self, cut_cost: float, replies: list[str] | None = None, cuts: int = 1):
        super().__init__(replies or [EMPTY])
        self.cut_cost, self.cuts = cut_cost, cuts

    def complete(self, model, system, user, max_tokens, schema=None) -> LLMReply:
        if self.cuts:
            self.cuts -= 1
            self.calls.append({"model": model, "cut": True})
            raise LLMError("出力が上限で切れました（max_tokens）", retryable=False, cost_usd=self.cut_cost)
        return super().complete(model, system, user, max_tokens, schema=schema)


def test_cost_meter_counts_max_tokens_failure():
    meter = _CostMeter(_CutLLM(0.3))
    with pytest.raises(LLMError):
        meter.complete("claude-sonnet-5-5", "s", "u", 10)
    assert meter.spent == pytest.approx(0.3)


def test_failed_job_records_cost_of_cut_call(settings, svc):
    job = svc.submit("alice", "a.md", "# 設計\n本文".encode())
    _worker(settings, svc, _CutLLM(0.3)).run_once(wait_seconds=1)
    done = svc.repo.get(job.job_id)
    assert done.status == JobStatus.FAILED and "max_tokens" in done.error
    assert done.cost_usd == pytest.approx(0.3)
    assert svc.repo.spent(_today(), "alice") == pytest.approx((0.3, 0.3))


def test_cut_deep_call_cost_is_in_report_and_job(settings, svc):
    """差分の上位モデルの呼び出しが切れても standard の結果は出る. 切れた呼び出しの費用も結果・ジョブに入る."""
    s = replace(settings, budget_per_job_usd=5.0)
    job = svc.submit("alice", "pr.diff", DIFF.encode())

    class CutSecond(FakeLLM):
        def complete(self, model, system, user, max_tokens, schema=None):
            if self.calls:
                self.calls.append({"model": model})
                raise LLMError("出力が上限で切れました（max_tokens）", cost_usd=0.4)
            return super().complete(model, system, user, max_tokens, schema=schema)

    must = '```json\n{"findings": [{"severity": "MUST", "file": "MemberService.java", "line": 2, "title": "t", "detail": "d"}]}\n```'
    _worker(s, svc, CutSecond([must])).run_once(wait_seconds=1)
    done = svc.repo.get(job.job_id)
    rep = json.loads(svc.blobs.get(done.report_json_key))
    assert done.status == JobStatus.SUCCEEDED
    assert rep["cost_usd"] == pytest.approx(sum(c["cost_usd"] for c in rep["calls"]) + 0.4, abs=1e-4)
    assert done.cost_usd == pytest.approx(rep["cost_usd"], abs=1e-4)
    assert any("失敗した LLM の呼び出しの費用" in n for n in rep["notes"])


def test_anthropic_max_tokens_error_carries_cost():
    from review_platform.adapters import llm as m

    class _C:
        def __init__(self, *a, **k):
            pass

        def complete(self, *a, **k):
            from devtools_common import llm as dc

            comp = dc.Completion(
                text="{",
                content=[],
                model="claude-sonnet-5-5",
                stop_reason="max_tokens",
                usage=dc.Usage(input_tokens=1000, output_tokens=4000),
                request_id="req_1",
                cost_usd=0.061,
            )
            raise dc.MaxTokensError(comp)

    llm = m.AnthropicLLM("sk-test", "https://llm.example.com")
    llm._clients["claude-sonnet-5-5"] = _C()
    with pytest.raises(LLMError) as e:
        llm.complete("claude-sonnet-5-5", "s", "u", 4000)
    assert e.value.cost_usd == pytest.approx(0.061) and e.value.retryable is False


def test_bedrock_max_tokens_error_carries_cost():
    from review_platform.adapters.llm import _reply, cost

    with pytest.raises(LLMError) as e:
        _reply("claude-sonnet-5-5", {"content": [], "usage": {"input_tokens": 1000, "output_tokens": 4000}, "stop_reason": "max_tokens"})
    assert e.value.cost_usd == pytest.approx(cost("claude-sonnet-5-5", 1000, 4000)) and e.value.cost_usd > 0


# ---- 9. 日次の上限（全体・利用者ごと）


def test_daily_owner_limit_blocks_before_llm(settings, svc):
    s = replace(settings, budget_daily_per_owner_usd=0.5)
    svc.repo.add_spend(_today(), "alice", 0.5)
    job = svc.submit("alice", "a.md", "# 設計".encode())
    llm = FakeLLM()
    _worker(s, svc, llm).run_once(wait_seconds=1)
    done = svc.repo.get(job.job_id)
    assert llm.calls == [] and done.status == JobStatus.FAILED and "利用者ごと" in done.error
    # 別の利用者は止めない
    other = svc.submit("bob", "a.md", "# 設計".encode())
    _worker(s, svc, llm).run_once(wait_seconds=1)
    assert svc.repo.get(other.job_id).status == JobStatus.SUCCEEDED


def test_daily_total_limit_blocks_everyone(settings, svc):
    s = replace(settings, budget_daily_usd=1.0)
    svc.repo.add_spend(_today(), "carol", 1.0)
    job = svc.submit("bob", "pr.diff", DIFF.encode())
    llm = FakeLLM([EMPTY])
    _worker(s, svc, llm).run_once(wait_seconds=1)
    done = svc.repo.get(job.job_id)
    assert llm.calls == [] and done.status == JobStatus.FAILED and "全体" in done.error


def test_daily_remaining_caps_job_budget(settings, svc):
    """日次の残りが 1 ジョブの上限より小さければ、その残りで見積もる（差分の一次呼び出しも呼ばない）."""
    s = replace(settings, budget_per_job_usd=5.0, budget_daily_usd=1.0)
    svc.repo.add_spend(_today(), "carol", 0.9999)
    job = svc.submit("alice", "pr.diff", DIFF.encode())
    llm = FakeLLM([EMPTY])
    _worker(s, svc, llm).run_once(wait_seconds=1)
    assert llm.calls == [] and "見積もり" in svc.repo.get(job.job_id).error


def test_successful_job_adds_to_daily_spend(settings, svc):
    job = svc.submit("alice", "a.md", "# 設計\n本文".encode())
    _worker(settings, svc, FakeLLM([MUST, '```json\n{"verdicts": [], "findings": []}\n```'])).run_once(wait_seconds=1)
    done = svc.repo.get(job.job_id)
    total, mine = svc.repo.spent(_today(), "alice")
    assert done.cost_usd > 0 and total == pytest.approx(done.cost_usd, abs=1e-4) and mine == pytest.approx(total)


def test_daily_limit_zero_does_not_block_free_fake_llm(settings, svc):
    """費用のかからない疑似 LLM（fake）では、0（未設定）の日次の上限は効かせない. 有料の LLM では 0 は「呼ばない」（tests/test_budget_zero.py）."""
    s = replace(settings, budget_daily_usd=0.0, budget_daily_per_owner_usd=0.0)
    svc.repo.add_spend(_today(), "alice", 1000.0)
    job = svc.submit("alice", "a.md", "# 設計".encode())
    _worker(s, svc, FakeLLM()).run_once(wait_seconds=1)
    assert svc.repo.get(job.job_id).status == JobStatus.SUCCEEDED


def test_spend_record_failure_does_not_fail_job(settings, svc, caplog):
    class Broken:
        def __init__(self, inner):
            self.inner = inner

        def add_spend(self, *a):
            raise RuntimeError("throttled")

        def __getattr__(self, n):
            return getattr(self.inner, n)

    job = svc.submit("alice", "a.md", "# 設計".encode())
    Worker(settings, svc.blobs, svc.queue, Broken(svc.repo), FakeLLM(), NoNotifier(), FakeRunner()).run_once(wait_seconds=1)
    assert svc.repo.get(job.job_id).status == JobStatus.SUCCEEDED and "daily spend not recorded" in caplog.text


def test_daily_limits_from_env(monkeypatch):
    monkeypatch.setenv("RP_BUDGET_DAILY_USD", "50")
    monkeypatch.setenv("RP_BUDGET_DAILY_PER_OWNER_USD", "5.5")
    s = Settings.from_env()
    assert (s.budget_daily_usd, s.budget_daily_per_owner_usd) == (50.0, 5.5)
    monkeypatch.delenv("RP_BUDGET_DAILY_USD")
    monkeypatch.delenv("RP_BUDGET_DAILY_PER_OWNER_USD")
    d = Settings.from_env()
    assert (d.budget_daily_usd, d.budget_daily_per_owner_usd) == (0.0, 0.0)  # 既定は 0 = 有料の LLM を呼ばない


# ---- 11. 期限切れのジョブは原本と結果のファイルも消す（ローカル）


def _expire(svc, job_id: str) -> None:
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(svc.repo.path)) as con:
        con.execute("UPDATE jobs SET expires_at = 1 WHERE job_id = ?", (job_id,))
        con.commit()


def test_local_expiry_deletes_upload_and_results(settings, svc):
    job = svc.submit("alice", "a.md", "# 設計".encode())
    _worker(settings, svc, FakeLLM()).run_once(wait_seconds=1)
    keep = svc.submit("alice", "b.md", "# 設計".encode())
    root = settings.data_dir / "blobs"
    assert (root / "uploads" / job.job_id).is_dir() and (root / "results" / job.job_id / "report.json").is_file()
    _expire(svc, job.job_id)
    assert svc.repo.delete_expired(now_epoch=10) == 1
    assert not (root / "uploads" / job.job_id).exists() and not (root / "results" / job.job_id).exists()
    assert (root / "uploads" / keep.job_id).is_dir()  # 期限内のものは残す
    with pytest.raises(Exception):  # noqa: B017
        svc.repo.get(job.job_id)


def test_local_expiry_keeps_row_when_files_cannot_be_deleted(settings, svc, monkeypatch):
    import shutil

    job = svc.submit("alice", "a.md", "# 設計".encode())
    _expire(svc, job.job_id)

    def boom(*a, **k):
        raise OSError("busy")

    monkeypatch.setattr(shutil, "rmtree", boom)
    assert svc.repo.delete_expired(now_epoch=10) == 0
    assert svc.repo.get(job.job_id).job_id == job.job_id  # 次の回にやり直せるよう、行は残す


def test_local_expiry_removes_old_spend(settings, svc):
    svc.repo.add_spend("2000-01-01", "alice", 1.0)
    svc.repo.delete_expired(now_epoch=2_000_000_000)
    assert svc.repo.spent("2000-01-01", "alice") == (0.0, 0.0)
