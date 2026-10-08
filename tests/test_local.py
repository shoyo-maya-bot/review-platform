from __future__ import annotations

import pytest

from contract import blob_contract, queue_contract, repo_contract, spend_contract
from review_platform.adapters.local import FsBlobStore, SqliteJobRepository, SqliteQueue
from review_platform.domain import Job, JobKind


def test_local_blob(tmp_path):
    blobs = FsBlobStore(tmp_path)
    blob_contract(blobs)
    for bad in ("../x", "/etc/passwd", "a/../../b", "a//b", ""):
        with pytest.raises(ValueError):
            blobs.put(bad, b"x")


def test_local_queue(tmp_path):
    q = SqliteQueue(tmp_path)
    queue_contract(q)
    q.send("20260101000000000000-0123456789abcdef")
    m = q.receive(1, 1, 30)[0]
    q.dead_letter(m)
    assert q.dead_count() == 1 and q.receive(1, 0, 30) == []


def test_local_repo(tmp_path):
    repo = SqliteJobRepository(tmp_path)
    repo_contract(repo)
    spend_contract(repo)
    old = Job.create("x", JobKind.DOCUMENT, "o.md", 1)
    old.expires_at = 1
    repo.create(old)
    assert repo.delete_expired(now_epoch=10) == 1
