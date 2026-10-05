"""保存・キュー・ジョブ表の実装（local・aws・azure）が同じように振る舞うことを確かめる共通の試験."""

from __future__ import annotations

import time

import pytest

from review_platform.domain import Job, JobKind, JobStatus
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
    mine = repo.list_by_owner("alice", 10)
    assert {j.job_id for j in mine} == {a.job_id, b.job_id}
    assert [j.job_id for j in repo.list_by_owner("alice", 1)] == [max(a.job_id, b.job_id)]
