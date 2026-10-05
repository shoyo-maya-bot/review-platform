"""API と MCP が共有する受付の処理（入力の検査 → 保存 → ジョブ表 → キュー）."""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass

from .config import Settings
from .domain import DIFF_EXTENSIONS, Job, JobKind, JobStatus, kind_for, safe_filename, valid_job_id
from .ports import BlobStore, JobQueue, JobRepository, NotFound

log = logging.getLogger(__name__)

ZIP_MAGIC = b"PK\x03\x04"
PDF_MAGIC = b"%PDF-"


class InputError(ValueError):
    """利用者の入力の誤り（400）."""


class NotReady(RuntimeError):
    """結果がまだ無い（409）."""


@dataclass
class Services:
    settings: Settings
    blobs: BlobStore
    queue: JobQueue
    repo: JobRepository

    def submit(self, owner: str, filename: str, data: bytes, title: str = "") -> Job:
        s = self.settings
        name = safe_filename(filename)
        ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in s.allowed_extensions:
            raise InputError(f"受け付けない形式です（{ext or '拡張子なし'}）. 使える形式: {', '.join(s.allowed_extensions)}")
        kind = kind_for(name)
        if kind is None:
            raise InputError(f"受け付けない形式です（{ext}）")
        if not data:
            raise InputError("ファイルが空です")
        if len(data) > s.max_upload_bytes:
            raise InputError(f"ファイルが大きすぎます（上限 {s.max_upload_bytes // (1024 * 1024)} MB）")
        _check_content(ext, data)
        job = Job.create(owner=owner, kind=kind, filename=name, ttl_days=s.job_ttl_days, title=title.strip())
        self.blobs.put(job.input_key, data, _content_type(ext))
        self.repo.create(job)
        try:
            self.queue.send(job.job_id)
        except Exception:
            # キューに入らなかったジョブを queued のまま残さない（利用者には 500 が返る. 送り直せばよい）
            job.status, job.error = JobStatus.FAILED, "受付の途中で失敗しました. もう一度送ってください"
            with contextlib.suppress(Exception):
                self.repo.transition(job, expected=JobStatus.QUEUED)
            raise
        log.info("job submitted", extra={"job_id": job.job_id, "kind": job.kind.value, "bytes": len(data), "owner": owner})
        return job

    def get_for(self, owner: str, job_id: str) -> Job:
        """持ち主でなければ「無い」と同じに扱う（他人のジョブ ID の有無を漏らさない）."""
        if not valid_job_id(job_id):
            raise NotFound(job_id)
        job = self.repo.get(job_id)
        if job.owner != owner:
            raise NotFound(job_id)
        return job

    def report_for(self, owner: str, job_id: str, fmt: str = "md") -> bytes:
        job = self.get_for(owner, job_id)
        if job.status != JobStatus.SUCCEEDED:
            raise NotReady(job.status.value)
        return self.blobs.get(job.report_md_key if fmt == "md" else job.report_json_key)

    def list_for(self, owner: str, limit: int = 20) -> list[Job]:
        return self.repo.list_by_owner(owner, max(1, min(limit, 100)))


def _check_content(ext: str, data: bytes) -> None:
    """拡張子と中身が合うかを確かめる（別の形式を拡張子だけ変えて送られても、変換ツールに渡さない）."""
    if ext in (".xlsx", ".xlsm", ".docx", ".pptx") and not data.startswith(ZIP_MAGIC):
        raise InputError("Office のファイルとして読めません（中身が ZIP 形式ではありません）")
    if ext == ".pdf" and not data.startswith(PDF_MAGIC):
        raise InputError("PDF として読めません")
    if ext in (".md",) + DIFF_EXTENSIONS:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as e:
            raise InputError("テキストは UTF-8 で送ってください") from e
        if "\x00" in text:
            raise InputError("テキストに NUL 文字が含まれています")
        if ext in DIFF_EXTENSIONS and "diff --git " not in text and not text.lstrip().startswith(("--- ", "diff ")):
            raise InputError("unified diff として読めません（git diff の出力を送ってください）")


def _content_type(ext: str) -> str:
    return {
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".pdf": "application/pdf",
        ".md": "text/markdown; charset=utf-8",
        ".diff": "text/x-diff; charset=utf-8",
        ".patch": "text/x-diff; charset=utf-8",
    }.get(ext, "application/octet-stream")


__all__ = ["InputError", "NotReady", "Services", "JobKind"]
