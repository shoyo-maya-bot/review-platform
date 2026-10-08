"""長すぎる文書は先頭だけをレビューする. そのとき結果・summary に「一部だけ」と出す（黙って全体をレビューした成功にしない）."""

from __future__ import annotations

import json

from conftest import FakeRunner, xlsx_bytes
from review_platform.adapters.llm import FakeLLM
from review_platform.adapters.notify import NoNotifier
from review_platform.pipeline import report
from review_platform.pipeline.document import MAX_DOC_CHARS, DocumentReviewer
from review_platform.worker import Worker

STD = "claude-sonnet-5-5"


def test_long_document_is_marked_partial():
    out = DocumentReviewer(FakeLLM(), STD, "", 100.0).review("a.md", "あ" * (MAX_DOC_CHARS + 10))
    assert out.partial == {"reviewed_chars": MAX_DOC_CHARS, "total_chars": MAX_DOC_CHARS + 10}
    assert any(n.startswith("一部だけ") for n in out.notes)


def test_short_document_is_not_partial():
    out = DocumentReviewer(FakeLLM(), STD, "", 100.0).review("a.md", "# 短い")
    assert out.partial is None


def test_worker_report_and_summary_show_partial(settings, svc):
    job = svc.submit("alice", "長い.xlsx", xlsx_bytes())
    runner = FakeRunner(markdown="# 長い\n\n" + "x" * (MAX_DOC_CHARS + 5))
    Worker(settings, svc.blobs, svc.queue, svc.repo, FakeLLM(), NoNotifier(), runner).run_once(wait_seconds=1)
    done = svc.repo.get(job.job_id)
    assert done.summary["partial"] is True
    rep = json.loads(svc.blobs.get(f"results/{job.job_id}/report.json"))
    assert rep["partial"]["reviewed_chars"] == MAX_DOC_CHARS and rep["job"]["summary"]["partial"] is True
    md = svc.blobs.get(f"results/{job.job_id}/report.md").decode("utf-8")
    assert "範囲: 一部だけ" in md


def test_whole_document_summary_is_not_partial(settings, svc):
    job = svc.submit("alice", "短い.xlsx", xlsx_bytes())
    Worker(settings, svc.blobs, svc.queue, svc.repo, FakeLLM(), NoNotifier(), FakeRunner()).run_once(wait_seconds=1)
    assert svc.repo.get(job.job_id).summary["partial"] is False
    rep = json.loads(svc.blobs.get(f"results/{job.job_id}/report.json"))
    assert rep["partial"] is None and "一部だけ" not in report.markdown(rep)
