"""ローカル（1 台の PC・docker compose）用の実装. ファイルと SQLite だけで動く.

API とワーカーが同じ RP_DATA_DIR を共有する前提. 本番の負荷には向かない（学習・試験用）.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from ..domain import Job, JobStatus
from ..ports import Conflict, NotFound, QueueMessage


def _check_key(key: str) -> None:
    parts = key.split("/")
    if not key or key.startswith("/") or any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"不正なキーです: {key!r}")


class FsBlobStore:
    def __init__(self, root: Path):
        self.root = root / "blobs"
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        _check_key(key)
        p = (self.root / key).resolve()
        if self.root.resolve() not in p.parents:
            raise ValueError(f"不正なキーです: {key!r}")
        return p

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + f".tmp-{secrets.token_hex(4)}")
        tmp.write_bytes(data)
        os.replace(tmp, p)

    def get(self, key: str) -> bytes:
        p = self._path(key)
        if not p.is_file():
            raise NotFound(key)
        return p.read_bytes()


class _Sqlite:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=30000")
        return con


class SqliteQueue(_Sqlite):
    """SQS に近い振る舞い（受け取ったメッセージは一定時間見えなくなる。削除しなければまた届く）."""

    def __init__(self, root: Path, max_receive: int = 5):
        super().__init__(root / "queue.sqlite3")
        self.max_receive = max_receive
        with closing(self.connect()) as con:
            con.execute(
                "CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,"
                " visible_at REAL NOT NULL, receive_count INTEGER NOT NULL DEFAULT 0, receipt TEXT, dead INTEGER NOT NULL DEFAULT 0)"
            )

    def send(self, job_id: str) -> None:
        with closing(self.connect()) as con:
            con.execute("INSERT INTO messages (job_id, visible_at) VALUES (?, ?)", (job_id, time.time()))

    def receive(self, max_messages: int = 1, wait_seconds: int = 10, visibility_seconds: int = 900) -> list[QueueMessage]:
        deadline = time.monotonic() + wait_seconds
        while True:
            got = self._receive_now(max_messages, visibility_seconds)
            if got or time.monotonic() >= deadline:
                return got
            time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))

    def _receive_now(self, max_messages: int, visibility_seconds: int) -> list[QueueMessage]:
        out: list[QueueMessage] = []
        with closing(self.connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            rows = con.execute(
                "SELECT id, job_id, receive_count FROM messages WHERE dead = 0 AND visible_at <= ? ORDER BY id LIMIT ?",
                (time.time(), max_messages),
            ).fetchall()
            for mid, job_id, count in rows:
                if count >= self.max_receive:  # SQS の maxReceiveCount と同じ. 何度も戻るメッセージを退避する
                    con.execute("UPDATE messages SET dead = 1 WHERE id = ?", (mid,))
                    continue
                receipt = f"{mid}:{secrets.token_hex(8)}"
                con.execute(
                    "UPDATE messages SET visible_at = ?, receive_count = receive_count + 1, receipt = ? WHERE id = ?",
                    (time.time() + visibility_seconds, receipt, mid),
                )
                out.append(QueueMessage(job_id=job_id, receipt=receipt, receive_count=count + 1))
            con.execute("COMMIT")
        return out

    def delete(self, message: QueueMessage) -> None:
        with closing(self.connect()) as con:
            con.execute("DELETE FROM messages WHERE receipt = ?", (message.receipt,))

    def extend(self, message: QueueMessage, visibility_seconds: int) -> None:
        with closing(self.connect()) as con:
            con.execute("UPDATE messages SET visible_at = ? WHERE receipt = ?", (time.time() + visibility_seconds, message.receipt))

    def dead_letter(self, message: QueueMessage) -> None:
        with closing(self.connect()) as con:
            con.execute("UPDATE messages SET dead = 1 WHERE receipt = ?", (message.receipt,))

    def dead_count(self) -> int:
        with closing(self.connect()) as con:
            return int(con.execute("SELECT COUNT(*) FROM messages WHERE dead = 1").fetchone()[0])


class SqliteJobRepository(_Sqlite):
    def __init__(self, root: Path):
        super().__init__(root / "jobs.sqlite3")
        with closing(self.connect()) as con:
            con.execute(
                "CREATE TABLE IF NOT EXISTS jobs (job_id TEXT PRIMARY KEY, owner TEXT NOT NULL, status TEXT NOT NULL,"
                " created_at TEXT NOT NULL, expires_at INTEGER NOT NULL, data TEXT NOT NULL)"
            )
            con.execute("CREATE INDEX IF NOT EXISTS jobs_owner ON jobs (owner, job_id)")

    def create(self, job: Job) -> None:
        with closing(self.connect()) as con:
            try:
                con.execute(
                    "INSERT INTO jobs (job_id, owner, status, created_at, expires_at, data) VALUES (?, ?, ?, ?, ?, ?)",
                    (job.job_id, job.owner, job.status.value, job.created_at, job.expires_at, json.dumps(job.to_dict())),
                )
            except sqlite3.IntegrityError as e:
                raise Conflict(job.job_id) from e

    def get(self, job_id: str) -> Job:
        with closing(self.connect()) as con:
            row = con.execute("SELECT data FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if not row:
            raise NotFound(job_id)
        return Job.from_dict(json.loads(row[0]))

    def transition(self, job: Job, expected: JobStatus, expected_attempts: int | None = None) -> None:
        sql = "UPDATE jobs SET status = ?, data = ? WHERE job_id = ? AND status = ?"
        args: tuple = (job.status.value, json.dumps(job.to_dict()), job.job_id, expected.value)
        if expected_attempts is not None:
            sql += " AND json_extract(data, '$.attempts') = ?"
            args += (expected_attempts,)
        with closing(self.connect()) as con:
            cur = con.execute(sql, args)
            if cur.rowcount != 1:
                raise Conflict(job.job_id)

    def list_by_owner(self, owner: str, limit: int = 20) -> list[Job]:
        with closing(self.connect()) as con:
            rows = con.execute("SELECT data FROM jobs WHERE owner = ? ORDER BY job_id DESC LIMIT ?", (owner, limit)).fetchall()
        return [Job.from_dict(json.loads(r[0])) for r in rows]

    def delete_expired(self, now_epoch: int, limit: int = 100) -> int:
        with closing(self.connect()) as con:
            cur = con.execute(
                "DELETE FROM jobs WHERE job_id IN (SELECT job_id FROM jobs WHERE expires_at > 0 AND expires_at < ? LIMIT ?)",
                (now_epoch, limit),
            )
            return cur.rowcount
