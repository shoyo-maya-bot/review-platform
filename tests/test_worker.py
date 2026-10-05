from __future__ import annotations

import json

import pytest

from conftest import FakeRunner, xlsx_bytes
from review_platform.adapters.llm import FakeLLM, LLMError
from review_platform.adapters.notify import NoNotifier
from review_platform.domain import JobStatus
from review_platform.pipeline import report
from review_platform.worker import MAX_ATTEMPTS, Worker

MUST = (
    '```json\n{"findings": [{"severity": "MUST", "location": "項目定義 行 3", "title": "桁が API と違う", "detail": "d", "evidence": "e"},'
    ' {"severity": "WANT", "location": "2 章", "title": "異常系が無い", "detail": "d", "evidence": "e"}]}\n```'
)
VERIFY = '```json\n{"verdicts": [{"id": "s1", "verdict": "reject", "reason": "表の別の行で定義済み"}], "findings": []}\n```'


class Recorder:
    def __init__(self):
        self.jobs = []

    def notify(self, job, url):
        self.jobs.append((job.status.value, url))


def _worker(settings, svc, llm=None, runner=None, notifier=None):
    return Worker(settings, svc.blobs, svc.queue, svc.repo, llm or FakeLLM(), notifier or NoNotifier(), runner or FakeRunner())


def _report(svc, job_id):
    return json.loads(svc.blobs.get(f"results/{job_id}/report.json"))


def test_document_review_with_verification(settings, svc):
    job = svc.submit("alice", "設計書.xlsx", xlsx_bytes(), "会員")
    llm = FakeLLM([MUST, VERIFY])
    rec = Recorder()
    runner = FakeRunner(warnings=["非表示の行が 3 行あります"])
    assert _worker(settings, svc, llm, runner, rec).run_once(wait_seconds=1)
    done = svc.repo.get(job.job_id)
    assert done.status == JobStatus.SUCCEEDED and done.attempts == 1
    rep = _report(svc, job.job_id)
    assert [f["title"] for f in rep["findings"]] == ["異常系が無い"]  # MUST は検証で却下
    assert [f["title"] for f in rep["rejected"]] == ["桁が API と違う"]
    assert rep["conversion_warnings"] and [c["step"] for c in rep["calls"]] == ["standard", "verify"]
    assert [c["model"] for c in llm.calls] == [settings.llm_model_standard, settings.llm_model_deep]
    assert "<document" in llm.calls[0]["user"] and "指示として扱わず" in llm.calls[0]["system"]
    assert done.summary["severity"]["MUST"] == 0 and done.summary["rejected"] == 1
    assert svc.blobs.get(f"results/{job.job_id}/document.md").startswith(b"# ")
    assert rec.jobs == [("succeeded", "")]
    assert svc.queue.receive(1, 0, 1) == []  # メッセージは消えている


def test_llm_none_converts_only(settings, svc):
    from dataclasses import replace

    from review_platform.adapters.llm import NoLLM

    s = replace(settings, llm="none")
    job = svc.submit("alice", "a.xlsx", xlsx_bytes())
    w = Worker(s, svc.blobs, svc.queue, svc.repo, NoLLM(), NoNotifier(), FakeRunner())
    w.run_once(wait_seconds=1)
    rep = _report(svc, job.job_id)
    assert rep["findings"] == [] and "RP_LLM=none" in rep["notes"][0] and rep["calls"] == []


def test_conversion_failure_is_permanent(settings, svc):
    job = svc.submit("alice", "a.xlsx", xlsx_bytes())
    _worker(settings, svc, runner=FakeRunner(code=2)).run_once(wait_seconds=1)
    done = svc.repo.get(job.job_id)
    assert done.status == JobStatus.FAILED and "doc2md" in done.error and done.attempts == 1


class FlakyLLM(FakeLLM):
    def __init__(self, fails: int):
        super().__init__(['```json\n{"findings": []}\n```'])
        self.fails = fails

    def complete(self, model, system, user, max_tokens):
        if self.fails:
            self.fails -= 1
            raise LLMError("429", retryable=True)
        return super().complete(model, system, user, max_tokens)


def test_retryable_failure_is_retried_then_succeeds(settings, svc):
    job = svc.submit("alice", "a.md", "# 設計".encode())
    w = _worker(settings, svc, llm=FlakyLLM(fails=1))
    w.run_once(wait_seconds=1)
    assert svc.repo.get(job.job_id).status == JobStatus.QUEUED
    msgs = svc.queue.receive(1, 0, 900)
    assert msgs == []  # まだ見えない（少し待ってからやり直す）
    svc.queue.extend(_any_message(svc), 0)
    w.run_once(wait_seconds=1)
    done = svc.repo.get(job.job_id)
    assert done.status == JobStatus.SUCCEEDED and done.attempts == 2


def _any_message(svc):
    import sqlite3
    from contextlib import closing

    from review_platform.ports import QueueMessage

    with closing(sqlite3.connect(svc.queue.path)) as con:
        mid, job_id, receipt = con.execute("SELECT id, job_id, receipt FROM messages").fetchone()
    return QueueMessage(job_id=job_id, receipt=receipt)


def test_gives_up_after_max_attempts(settings, svc):
    job = svc.submit("alice", "a.md", b"# x")
    w = _worker(settings, svc, llm=FlakyLLM(fails=99))
    for _ in range(MAX_ATTEMPTS):
        svc.queue.extend(_any_message(svc), 0)
        w.run_once(wait_seconds=1)
    done = svc.repo.get(job.job_id)
    assert done.status == JobStatus.FAILED and done.attempts == MAX_ATTEMPTS


def test_duplicate_message_is_idempotent(settings, svc):
    job = svc.submit("alice", "a.md", b"# x")
    svc.queue.send(job.job_id)  # 同じジョブのメッセージが 2 通
    llm = FakeLLM()
    w = _worker(settings, svc, llm=llm)
    w.run_once(wait_seconds=1)
    w.run_once(wait_seconds=1)
    assert len(llm.calls) == 1 and svc.repo.get(job.job_id).status == JobStatus.SUCCEEDED


def test_malformed_and_orphan_messages(settings, svc):
    svc.queue.send("../../not-a-job")
    svc.queue.send("20260101000000000000-0123456789abcdef")  # ジョブ表に無い
    w = _worker(settings, svc)
    w.run_once(wait_seconds=1)
    w.run_once(wait_seconds=1)
    assert svc.queue.dead_count() == 1 and svc.queue.receive(1, 0, 1) == []


def test_unreadable_llm_output_fails_instead_of_zero_findings(settings, svc):
    job = svc.submit("alice", "a.md", b"# x")
    _worker(settings, svc, llm=FakeLLM(["すみません"])).run_once(wait_seconds=1)
    assert svc.repo.get(job.job_id).status == JobStatus.FAILED


DIFF = """diff --git a/src/main/java/x/MemberService.java b/src/main/java/x/MemberService.java
--- a/src/main/java/x/MemberService.java
+++ b/src/main/java/x/MemberService.java
@@ -1,2 +1,2 @@
-    void a() {}
+    @Transactional void a() {}
"""


def test_diff_review_uses_auto_review(settings, svc):
    job = svc.submit("alice", "pr.diff", DIFF.encode(), "トランザクションを付ける")
    llm = FakeLLM(['```json\n{"findings": [{"severity": "WANT", "file": "MemberService.java", "line": 2, "title": "t", "detail": "d"}]}\n```'])
    _worker(settings, svc, llm=llm).run_once(wait_seconds=1)
    rep = _report(svc, job.job_id)
    assert rep["routing"]["tier"] == "standard" and len(rep["findings"]) == 1
    assert "<diff>" in llm.calls[0]["user"] and "# PR: トランザクションを付ける" in llm.calls[0]["user"]


def test_markdown_report_is_sanitized():
    from review_platform.domain import Job, JobKind

    class F:
        severity, status, location, evidence, models, verdict_note = "MUST", "unverified", "x|y", "", ["m"], ""
        title = "<script>alert(1)</script> @channel"
        detail = "![img](http://e/x.png)\n2 行目"

    rep = report.build(Job.create("a", JobKind.DOCUMENT, "a.md", 1, "<b>t</b>"), [F()], [], [])
    md = report.markdown(rep)
    assert "<script>" not in md and "@channel" not in md and "![img]" not in md and "<b>" not in md
    assert "x\\|y" in md


@pytest.mark.parametrize("budget", [0.0001])
def test_budget_blocks_expensive_document(settings, svc, budget):
    from dataclasses import replace

    s = replace(settings, budget_per_job_usd=budget)
    job = svc.submit("alice", "a.md", b"# x" * 1000)
    Worker(s, svc.blobs, svc.queue, svc.repo, FakeLLM(), NoNotifier(), FakeRunner()).run_once(wait_seconds=1)
    done = svc.repo.get(job.job_id)
    assert done.status == JobStatus.FAILED and "上限" in done.error
