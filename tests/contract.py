"""保存・キュー・ジョブ表の実装（local・aws・azure）が同じように振る舞うことを確かめる共通の試験."""

from __future__ import annotations

import time

import pytest

from review_platform.domain import Job, JobKind, JobStatus, valid_job_id
from review_platform.ports import Conflict, NotFound


def blob_contract(blobs) -> None:
    blobs.put("uploads/j1/a.xlsx", b"hello", "application/octet-stream")
    assert blobs.get("uploads/j1/a.xlsx") == b"hello"
    blobs.put("uploads/j1/a.xlsx", b"again")
    assert blobs.get("uploads/j1/a.xlsx") == b"again"
    with pytest.raises(NotFound):
        blobs.get("uploads/none/x")


def queue_contract(q, supports_visibility: bool = True) -> None:
    q.send("20260101000000000000-0123456789abcdef")
    msgs = q.receive(max_messages=1, wait_seconds=5, visibility_seconds=30)
    assert len(msgs) == 1 and msgs[0].job_id == "20260101000000000000-0123456789abcdef" and msgs[0].receive_count == 1
    assert q.receive(max_messages=1, wait_seconds=1, visibility_seconds=30) == []  # 見えない間は届かない
    if supports_visibility:
        q.extend(msgs[0], 0)
        again = q.receive(max_messages=1, wait_seconds=5, visibility_seconds=30)
        assert len(again) == 1 and again[0].receive_count == 2
        msgs = again
    q.delete(msgs[0])
    time.sleep(0.2)
    assert q.receive(max_messages=1, wait_seconds=1, visibility_seconds=1) == []


def repo_contract(repo) -> None:
    a = Job.create("alice", JobKind.DOCUMENT, "a.xlsx", 30)
    b = Job.create("alice", JobKind.DIFF, "b.diff", 30)
    c = Job.create("bob", JobKind.DOCUMENT, "c.md", 30)
    for j in (a, b, c):
        repo.create(j)
    with pytest.raises(Conflict):
        repo.create(a)
    got = repo.get(a.job_id)
    assert got == a
    with pytest.raises(NotFound):
        repo.get("20000101000000000000-0000000000000000")
    a.status = JobStatus.RUNNING
    repo.transition(a, expected=JobStatus.QUEUED)
    with pytest.raises(Conflict):
        repo.transition(a, expected=JobStatus.QUEUED)  # もう queued ではない
    assert repo.get(a.job_id).status == JobStatus.RUNNING
    a.attempts = 1
    repo.transition(a, expected=JobStatus.RUNNING, expected_attempts=0)
    with pytest.raises(Conflict):
        repo.transition(a, expected=JobStatus.RUNNING, expected_attempts=0)  # 試行回数が進んでいる（別のワーカーが取った）
    assert repo.get(a.job_id).attempts == 1
    mine = repo.list_by_owner("alice", 10)
    assert {j.job_id for j in mine} == {a.job_id, b.job_id}
    assert [j.job_id for j in repo.list_by_owner("alice", 1)] == [max(a.job_id, b.job_id)]


def spend_contract(repo) -> None:
    """日次の費用: 全体と利用者ごとに足し上がり、日付・利用者で分かれる. 一覧にジョブとして出ない."""
    assert repo.spent("2026-10-08", "alice") == (0.0, 0.0)
    repo.add_spend("2026-10-08", "alice", 0.25)
    repo.add_spend("2026-10-08", "alice", 0.5)
    repo.add_spend("2026-10-08", "bob/x#1", 0.125)  # キーに使えない文字を含む利用者名
    total, mine = repo.spent("2026-10-08", "alice")
    assert total == pytest.approx(0.875) and mine == pytest.approx(0.75)
    assert repo.spent("2026-10-08", "bob/x#1")[1] == pytest.approx(0.125)
    assert repo.spent("2026-10-09", "alice") == (0.0, 0.0)
    assert all(valid_job_id(j.job_id) for j in repo.list_by_owner("alice", 100))  # 集計の行はジョブの一覧に出ない


def reserve_contract(repo) -> None:
    """呼び出しの前の予約: 上限（全体・利用者ごと）を超えるなら何も足さずに超えた範囲を返す. 0 の上限は効かせない. 精算は add_spend（負もありうる）."""
    from review_platform.ports import SPEND_OWNER, SPEND_TOTAL

    day = "2026-10-10"
    assert repo.reserve_spend(day, "alice", 0.4, 1.0, 0.5) is None
    assert repo.spent(day, "alice") == pytest.approx((0.4, 0.4))
    assert repo.reserve_spend(day, "alice", 0.2, 1.0, 0.5) == SPEND_OWNER  # 利用者ごと 0.6 > 0.5
    assert repo.spent(day, "alice") == pytest.approx((0.4, 0.4))  # 超えたときは何も足さない（全体にも）
    assert repo.reserve_spend(day, "bob", 0.5, 1.0, 0.5) is None  # 全体 0.9
    assert repo.reserve_spend(day, "carol", 0.2, 1.0, 0.5) == SPEND_TOTAL  # 全体 1.1 > 1.0
    assert repo.spent(day, "carol") == pytest.approx((0.9, 0.0))
    assert repo.reserve_spend(day, "carol", 0.6, 0.0, 0.0) is None  # 上限 0 は効かせない（無料の LLM. 有料は worker が先に止める）
    repo.add_spend(day, "alice", -0.3)  # 精算: 予約 0.4 に対して実額 0.1
    assert repo.spent(day, "alice") == pytest.approx((1.2, 0.1))
    assert repo.reserve_spend(day, "dave", 2.0, 5.0, 1.0) == SPEND_OWNER  # 1 回で上限を超える額（まだ行が無い）
    assert repo.spent(day, "dave") == pytest.approx((1.2, 0.0))


def reserve_race_contract(repo, workers: int = 8) -> None:
    """同時に予約しても、上限に入る分しか通らない（読んでから足すまでの間に別のワーカーが割り込んでも超えない）."""
    import threading

    day, usd, limit = "2026-10-11", 0.3, 1.0  # 1.0 に入るのは 3 件まで
    start = threading.Barrier(workers)
    got: list[str | None] = []
    lock = threading.Lock()

    def one(i: int) -> None:
        start.wait()
        r = repo.reserve_spend(day, f"user{i}", usd, limit, 0.0)
        with lock:
            got.append(r)

    ts = [threading.Thread(target=one, args=(i,)) for i in range(workers)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=60)
    assert len(got) == workers
    assert got.count(None) == 3
    assert repo.spent(day, "user0")[0] == pytest.approx(0.9)


def rate_contract(repo) -> None:
    """API の件数制限（RP_RATE_LIMIT_SHARED）: 区切り・利用者ごとに上限まで数え、上限に達したら足さずに False. 一覧にジョブとして出ない."""
    w = 1_760_000_040
    assert [repo.count_request("alice", w, 2) for _ in range(3)] == [True, True, False]
    assert repo.count_request("bob/x#1", w, 2) is True  # 利用者ごと（キーに使えない文字も可）
    assert repo.count_request("alice", w + 60, 2) is True  # 次の区切りは数え直す
    assert all(valid_job_id(j.job_id) for j in repo.list_by_owner("alice", 100))


def rate_race_contract(repo, workers: int = 8) -> None:
    """複数の台が同時に数えても、上限を超えて通さない."""
    import threading

    start = threading.Barrier(workers)
    got: list[bool] = []
    lock = threading.Lock()

    def one() -> None:
        start.wait()
        r = repo.count_request("carol", 1_760_000_100, 3)
        with lock:
            got.append(r)

    ts = [threading.Thread(target=one) for _ in range(workers)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=60)
    assert got.count(True) == 3 and len(got) == workers
