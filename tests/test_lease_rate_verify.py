"""心拍と貸し出し期限の順序（落ちても試行回数を失わない）・件数制限の全台共有（RP_RATE_LIMIT_SHARED）・MUST の検証の上限の明記."""

from __future__ import annotations

import contextlib
import json
from dataclasses import replace
from datetime import timedelta

from starlette.testclient import TestClient

from conftest import TOKEN, FakeRunner
from review_platform.adapters.llm import FakeLLM
from review_platform.adapters.notify import NoNotifier
from review_platform.api import create_app
from review_platform.domain import Job, JobKind, JobStatus, iso, now
from review_platform.pipeline.document import MAX_VERIFY, DocumentReviewer
from review_platform.ports import QueueMessage
from review_platform.worker import HEARTBEAT_SECONDS, LEASE_SECONDS, MAX_ATTEMPTS, VISIBILITY_SECONDS, Worker, _Heartbeat

H = {"authorization": f"Bearer {TOKEN}"}


def _worker(settings, svc, llm):
    return Worker(settings, svc.blobs, svc.queue, svc.repo, llm, NoNotifier(), FakeRunner())


# ---- 心拍と貸し出し期限


def test_lease_ends_before_message_becomes_visible_again():
    # 以前は LEASE_SECONDS == VISIBILITY_SECONDS で、落ちた後の再配信が貸し出し中に当たって見送られ、受信回数を 1 回むだにした
    assert LEASE_SECONDS < VISIBILITY_SECONDS
    assert HEARTBEAT_SECONDS < LEASE_SECONDS


def _once(hb) -> None:
    """心拍のループを 1 回だけ回す（待たずに）."""
    answers = iter([False, True])
    hb.stop.wait = lambda _t: next(answers)


def test_heartbeat_extends_lease_before_message():
    calls: list[str] = []

    class Queue:
        def extend(self, msg, seconds):
            calls.append(f"message:{seconds}")

    class Repo:
        def transition(self, job, expected, expected_attempts=None):
            calls.append("lease")

    job = Job.create("alice", JobKind.DOCUMENT, "a.md", 1)
    job.status, job.attempts = JobStatus.RUNNING, 1
    hb = _Heartbeat(Queue(), QueueMessage(job.job_id, "r"), Repo(), job)
    _once(hb)
    hb._run()
    assert calls == ["lease", f"message:{VISIBILITY_SECONDS}"]


def test_heartbeat_extends_message_even_if_lease_update_fails():
    calls: list[str] = []

    class Queue:
        def extend(self, msg, seconds):
            calls.append("message")

    class Repo:
        def transition(self, job, expected, expected_attempts=None):
            raise RuntimeError("throttled")

    job = Job.create("alice", JobKind.DOCUMENT, "a.md", 1)
    hb = _Heartbeat(Queue(), QueueMessage(job.job_id, "r"), Repo(), job)
    _once(hb)
    hb._run()
    assert calls == ["message"]


def test_redelivery_after_crash_takes_over_instead_of_skipping(settings, svc):
    """ワーカーが落ちて、最後の心拍から見えない時間が過ぎて再配信された. 貸し出しは切れているので、見送らずに引き継ぐ."""
    job = svc.submit("alice", "a.md", b"# x")
    job.status, job.attempts = JobStatus.RUNNING, 1
    job.updated_at = iso(now() - timedelta(seconds=VISIBILITY_SECONDS - 30))  # 最後の心拍（期限の更新）の直後に落ちた
    svc.repo.transition(job, expected=JobStatus.QUEUED)
    _worker(settings, svc, FakeLLM()).run_once(wait_seconds=1)
    done = svc.repo.get(job.job_id)
    assert done.status == JobStatus.SUCCEEDED and done.attempts == 2


def test_three_crashes_use_all_attempts(settings, svc, monkeypatch):
    """落ちるたびに再配信される. 受信回数を見送りでむだにせず、MAX_ATTEMPTS 回まで試す."""
    job = svc.submit("alice", "a.md", b"# x")
    w = _worker(settings, svc, FakeLLM())
    attempts_seen: list[int] = []

    def crash(job, meter, remaining_budget):
        attempts_seen.append(job.attempts)
        raise SystemExit  # プロセスが落ちた（例外の処理もメッセージの削除もしない）

    monkeypatch.setattr(w, "_process", crash)
    for _ in range(MAX_ATTEMPTS + 1):
        with contextlib.suppress(SystemExit):
            w.run_once(wait_seconds=1)
        # 見えない時間（受信した時から）が切れて再配信された. 貸し出し期限は受信の少し後（状態の更新）から数えるので、
        # 再配信の時点の期限の経過は見えない時間より少し短い
        cur = svc.repo.get(job.job_id)
        if cur.status == JobStatus.RUNNING:
            cur.updated_at = iso(now() - timedelta(seconds=VISIBILITY_SECONDS - 5))
            svc.repo.transition(cur, expected=JobStatus.RUNNING, expected_attempts=cur.attempts)
        _release(svc)
    assert attempts_seen == list(range(1, MAX_ATTEMPTS + 1))
    assert svc.repo.get(job.job_id).status == JobStatus.FAILED


def _release(svc):
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(svc.queue.path)) as con:
        con.execute("UPDATE messages SET visible_at = 0")
        con.commit()


# ---- 件数制限の全台共有


def _post(c):
    return c.post("/v1/jobs", headers=H, files={"file": ("a.md", b"# x")}).status_code


def test_rate_limit_is_per_process_by_default(settings, svc):
    s = replace(settings, rate_limit_per_minute=2)
    with TestClient(create_app(s, svc)) as a, TestClient(create_app(s, svc)) as b:
        assert [_post(a), _post(a), _post(b), _post(b)] == [202, 202, 202, 202]  # 既定は台ごと（従来どおり）


def test_shared_rate_limit_counts_across_instances(settings, svc):
    s = replace(settings, rate_limit_per_minute=2, rate_limit_shared=True)
    with TestClient(create_app(s, svc)) as a, TestClient(create_app(s, svc)) as b:  # 同じジョブ表を使う 2 台
        codes = [_post(a), _post(b), _post(a), _post(b)]
    assert codes[:2] == [202, 202] and codes[2:] == [429, 429]


def test_shared_rate_limit_falls_back_to_process_when_store_fails(settings, svc, monkeypatch):
    s = replace(settings, rate_limit_per_minute=1, rate_limit_shared=True)

    def broken(*a, **kw):
        raise RuntimeError("table unavailable")

    monkeypatch.setattr(svc.repo, "count_request", broken)
    with TestClient(create_app(s, svc)) as c:
        assert [_post(c), _post(c)] == [202, 429]  # 保存先が落ちても API は止めず、その台のメモリで数える


def test_config_reads_shared_flag(monkeypatch):
    from review_platform.config import Settings

    monkeypatch.setenv("RP_RATE_LIMIT_SHARED", "true")
    monkeypatch.setenv("RP_AUTH", "none")
    monkeypatch.setenv("RP_HOST", "127.0.0.1")
    assert Settings.from_env().rate_limit_shared is True
    monkeypatch.delenv("RP_RATE_LIMIT_SHARED")
    assert Settings.from_env().rate_limit_shared is False


# ---- MUST の検証の上限


def _reply(*items: dict, key: str = "findings", extra: dict | None = None) -> str:
    return "```json\n" + json.dumps({key: list(items), **(extra or {})}, ensure_ascii=False) + "\n```"


def _f(title: str) -> dict:
    return {"severity": "MUST", "location": "1 章", "title": title, "detail": "d", "evidence": "e"}


def test_unverified_musts_beyond_the_cap_are_marked():
    n = MAX_VERIFY + 3
    first = _reply(*[_f(f"m{i}") for i in range(n)])
    verify = _reply(*[{"id": f"s{i + 1}", "verdict": "confirm", "reason": "あり"} for i in range(MAX_VERIFY)], key="verdicts", extra={"findings": []})
    llm = FakeLLM([first, verify])
    out = DocumentReviewer(llm, "claude-sonnet-5-5", "claude-opus-5-5", budget_usd=5).review("a.md", "本文")
    by_title = {f.title: f for f in out.findings}
    assert all(by_title[f"m{i}"].status == "confirmed" for i in range(MAX_VERIFY))
    rest = [by_title[f"m{i}"] for i in range(MAX_VERIFY, n)]
    assert all(f.status == "unverified" and "未検証" in f.verdict_note for f in rest)
    assert any(f"MUST {n} 件のうち先頭 {MAX_VERIFY} 件" in x and "残り 3 件は未検証" in x for x in out.notes), out.notes


def test_all_musts_verified_note_unchanged():
    first = _reply(_f("a"), _f("b"))
    verify = _reply({"id": "s1", "verdict": "confirm", "reason": "r"}, {"id": "s2", "verdict": "confirm", "reason": "r"}, key="verdicts", extra={"findings": []})
    out = DocumentReviewer(FakeLLM([first, verify]), "claude-sonnet-5-5", "claude-opus-5-5", budget_usd=5).review("a.md", "本文")
    assert "MUST 2 件を上位モデルで検証しました" in out.notes
    assert not any("未検証" in f.verdict_note for f in out.findings)
