"""重複配信・やり直しの費用・本文の大きさ・設定の検査など、壊れ方の試験."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import timedelta

import pytest
from starlette.testclient import TestClient

from conftest import TOKEN, FakeRunner
from review_platform.adapters.llm import FakeLLM
from review_platform.adapters.local import SqliteQueue
from review_platform.adapters.notify import NoNotifier
from review_platform.api import BodyLimit, create_app
from review_platform.config import ConfigError
from review_platform.domain import JobStatus, iso, now
from review_platform.ports import Conflict
from review_platform.worker import LEASE_SECONDS, Worker, _CostMeter

H = {"authorization": f"Bearer {TOKEN}"}
MUST = '```json\n{"findings": [{"severity": "MUST", "location": "1 章", "title": "t", "detail": "d", "evidence": "e"}]}\n```'
VERIFY = '```json\n{"verdicts": [{"id": "s1", "verdict": "confirm", "reason": "r"}], "findings": []}\n```'


def _worker(settings, svc, llm):
    return Worker(settings, svc.blobs, svc.queue, svc.repo, llm, NoNotifier(), FakeRunner())


def _release_all(svc):
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(svc.queue.path)) as con:
        con.execute("UPDATE messages SET visible_at = 0")
        con.commit()


def test_transition_checks_attempts(svc):
    job = svc.submit("alice", "a.md", b"# x")
    job.status, job.attempts = JobStatus.RUNNING, 1
    svc.repo.transition(job, expected=JobStatus.QUEUED, expected_attempts=0)
    with pytest.raises(Conflict):  # 別のワーカーが先に取った（試行回数が進んでいる）
        svc.repo.transition(job, expected=JobStatus.RUNNING, expected_attempts=0)


def test_duplicate_delivery_while_running_is_skipped(settings, svc):
    job = svc.submit("alice", "a.md", b"# x")
    job.status, job.attempts, job.updated_at = JobStatus.RUNNING, 1, iso(now())
    svc.repo.transition(job, expected=JobStatus.QUEUED)  # 別のワーカーが処理中
    llm = FakeLLM()
    _worker(settings, svc, llm).run_once(wait_seconds=1)
    assert llm.calls == [] and svc.repo.get(job.job_id).status == JobStatus.RUNNING
    assert svc.queue.receive(1, 0, 900) == []  # メッセージは消さずに見送る（見えない時間が切れたら確かめ直す）


def test_stale_running_job_is_taken_over(settings, svc):
    job = svc.submit("alice", "a.md", b"# x")
    job.status, job.attempts = JobStatus.RUNNING, 1
    job.updated_at = iso(now() - timedelta(seconds=LEASE_SECONDS + 5))  # 前のワーカーが落ちた
    svc.repo.transition(job, expected=JobStatus.QUEUED)
    _worker(settings, svc, FakeLLM()).run_once(wait_seconds=1)
    done = svc.repo.get(job.job_id)
    assert done.status == JobStatus.SUCCEEDED and done.attempts == 2


def test_cost_accumulates_across_retries(settings, svc):
    job = svc.submit("alice", "a.md", "# 設計\n本文".encode())
    job.cost_usd = 0.0123  # 前の試行で使った分
    svc.repo.transition(job, expected=JobStatus.QUEUED)
    _worker(settings, svc, FakeLLM([MUST, VERIFY])).run_once(wait_seconds=1)
    done = svc.repo.get(job.job_id)
    rep = json.loads(svc.blobs.get(done.report_json_key))
    assert done.status == JobStatus.SUCCEEDED
    assert done.cost_usd == pytest.approx(0.0123 + rep["cost_usd"], abs=1e-4)


def test_cost_meter_counts_calls_before_a_failure():
    llm = FakeLLM([MUST])
    meter = _CostMeter(llm)
    meter.complete("claude-sonnet-5-5", "s", "u" * 3000, 100)
    assert meter.spent > 0 and meter.name == "fake"


def test_retry_gets_only_the_remaining_budget(settings, svc):
    job = svc.submit("alice", "a.md", "# 設計\n本文".encode())
    job.cost_usd = settings.budget_per_job_usd  # 前の試行で使い切った
    svc.repo.transition(job, expected=JobStatus.QUEUED)
    _worker(settings, svc, FakeLLM()).run_once(wait_seconds=1)
    done = svc.repo.get(job.job_id)
    assert done.status == JobStatus.FAILED and "上限" in done.error


def test_local_queue_dead_letters_after_max_receive(tmp_path):
    q = SqliteQueue(tmp_path, max_receive=2)
    q.send("20260101000000000000-0123456789abcdef")
    for _ in range(2):
        (m,) = q.receive(1, 0, 900)
        q.extend(m, 0)
    assert q.receive(1, 0, 900) == [] and q.dead_count() == 1


def test_submit_marks_job_failed_when_queue_send_fails(svc, monkeypatch):
    def boom(job_id):
        raise RuntimeError("queue down")

    monkeypatch.setattr(svc.queue, "send", boom)
    with pytest.raises(RuntimeError):
        svc.submit("alice", "a.md", b"# x")
    (job,) = svc.list_for("alice")
    assert job.status == JobStatus.FAILED


@pytest.fixture
def client(settings, svc):
    with TestClient(create_app(settings, svc)) as c:
        yield c


def test_chunked_body_over_limit_is_rejected_early(client):
    def chunks():
        for _ in range(30):
            yield b"0" * (1024 * 1024)

    r = client.post("/v1/jobs", headers={**H, "content-type": "application/json"}, content=chunks())
    assert r.status_code == 413


def test_body_limit_stops_reading_at_the_limit():
    reached = []

    async def app(scope, receive, send):
        reached.append(True)

    received = 0

    async def receive():
        nonlocal received
        received += 1
        return {"type": "http.request", "body": b"0" * 1000, "more_body": True}

    sent = []

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "headers": [], "path": "/v1/jobs"}
    asyncio.run(BodyLimit(app, limit=5000)(scope, receive, send))
    assert reached == [] and received == 6  # 上限を超えた時点でやめ、アプリには渡さない
    assert sent[0]["status"] == 413


def test_mcp_body_over_limit(client, settings):
    big = b"0" * (int(settings.max_upload_bytes * 1.4) + 128 * 1024)
    r = client.post("/mcp", headers={**H, "content-type": "application/json", "accept": "application/json, text/event-stream"}, content=big)
    assert r.status_code == 413


@pytest.mark.parametrize("url", ["http://localhost.evil.example", "http://127.0.0.1.example.com", "ftp://x", "https://"])
def test_llm_base_url_rejects_lookalikes(settings, url):
    with pytest.raises(ConfigError):
        replace(settings, llm_base_url=url).validate("worker")


@pytest.mark.parametrize("url", ["https://api.example.com", "http://localhost:4010", "http://127.0.0.1:8080"])
def test_llm_base_url_accepts(settings, url):
    replace(settings, llm_base_url=url).validate("worker")
