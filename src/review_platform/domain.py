"""ジョブとレビュー結果のモデル（保存先に依存しない）."""

from __future__ import annotations

import re
import secrets
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class JobKind(StrEnum):
    DOCUMENT = "document"  # 設計書（Excel・Word・PowerPoint・PDF・Markdown）
    DIFF = "diff"  # コードの差分（unified diff）


DOCUMENT_EXTENSIONS = (".xlsx", ".xlsm", ".docx", ".pptx", ".pdf", ".md")
DIFF_EXTENSIONS = (".diff", ".patch")


def now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def new_job_id() -> str:
    """推測できない ID（URL にそのまま使える）. 時刻順に並ぶよう先頭に日時を付ける."""
    return datetime.now(UTC).strftime("%Y%m%d%H%M%S%f") + "-" + secrets.token_hex(8)


JOB_ID_RE = re.compile(r"^\d{20}-[0-9a-f]{16}$")


def valid_job_id(job_id: str) -> bool:
    return bool(JOB_ID_RE.match(job_id or ""))


def safe_filename(name: str, max_len: int = 120) -> str:
    """利用者が付けたファイル名を、保存のキーに使える形にする（パスの成分・制御文字・予約文字を落とす）."""
    name = unicodedata.normalize("NFC", name or "").replace("\\", "/").split("/")[-1]
    name = "".join(c for c in name if c.isprintable() and c not in '<>:"|?*\x00')
    name = name.strip().strip(".")
    if not name:
        name = "upload"
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    stem = stem[: max_len - len(ext) - 1] if ext else stem[:max_len]
    return f"{stem}.{ext.lower()}" if ext else stem


def kind_for(filename: str) -> JobKind | None:
    lower = filename.lower()
    if lower.endswith(DIFF_EXTENSIONS):
        return JobKind.DIFF
    if lower.endswith(DOCUMENT_EXTENSIONS):
        return JobKind.DOCUMENT
    return None


@dataclass
class Job:
    job_id: str
    owner: str
    kind: JobKind
    filename: str
    input_key: str
    status: JobStatus = JobStatus.QUEUED
    title: str = ""
    created_at: str = field(default_factory=lambda: iso(now()))
    updated_at: str = field(default_factory=lambda: iso(now()))
    expires_at: int = 0  # UNIX 秒（DynamoDB の TTL と同じ形）
    attempts: int = 0
    error: str = ""
    summary: dict[str, Any] = field(default_factory=dict)
    cost_usd: float = 0.0

    @staticmethod
    def create(owner: str, kind: JobKind, filename: str, ttl_days: int, title: str = "") -> Job:
        job_id = new_job_id()
        safe = safe_filename(filename)
        created = now()
        return Job(
            job_id=job_id,
            owner=owner,
            kind=kind,
            filename=safe,
            input_key=f"uploads/{job_id}/{safe}",
            title=title[:200],
            created_at=iso(created),
            updated_at=iso(created),
            expires_at=int((created + timedelta(days=ttl_days)).timestamp()),
        )

    @property
    def report_json_key(self) -> str:
        return f"results/{self.job_id}/report.json"

    @property
    def report_md_key(self) -> str:
        return f"results/{self.job_id}/report.md"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind.value
        d["status"] = self.status.value
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> Job:
        return Job(
            job_id=str(d["job_id"]),
            owner=str(d["owner"]),
            kind=JobKind(d["kind"]),
            filename=str(d["filename"]),
            input_key=str(d["input_key"]),
            status=JobStatus(d.get("status", "queued")),
            title=str(d.get("title", "")),
            created_at=str(d.get("created_at", "")),
            updated_at=str(d.get("updated_at", "")),
            expires_at=int(d.get("expires_at", 0) or 0),
            attempts=int(d.get("attempts", 0) or 0),
            error=str(d.get("error", "")),
            summary=dict(d.get("summary") or {}),
            cost_usd=float(d.get("cost_usd", 0.0) or 0.0),
        )

    def public_view(self) -> dict[str, Any]:
        """API・MCP で利用者に返す形（保存のキーなど内部の値は出さない）."""
        return {
            "job_id": self.job_id,
            "kind": self.kind.value,
            "status": self.status.value,
            "filename": self.filename,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error": self.error,
            "summary": self.summary,
            "cost_usd": round(self.cost_usd, 4),
        }
