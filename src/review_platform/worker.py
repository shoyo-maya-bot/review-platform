"""ワーカー. キューからジョブを 1 件ずつ取り、変換 → AI レビュー → 結果の保存 → 通知 を行う.

- 冪等: 同じメッセージが 2 回届いても、ジョブ表の状態（条件付き更新）で 1 回分しか処理しない
- 処理中は定期的にメッセージの「見えない時間」を延ばす（途中で別のワーカーに渡らないように）
- 一時的な失敗はキューに戻してやり直す. MAX_ATTEMPTS 回失敗したら失敗にする
- SIGTERM を受けたら、処理中のジョブを終えてから止まる
"""

from __future__ import annotations

import contextlib
import json
import logging
import signal
import tempfile
import threading
import time
from pathlib import Path

from .config import Settings
from .domain import Job, JobKind, JobStatus, iso, now, valid_job_id
from .pipeline import PermanentError, RetryableError, report
from .pipeline.convert import ToolRunner, to_markdown
from .ports import BlobStore, Conflict, JobQueue, JobRepository, LLMClient, NotFound, Notifier, QueueMessage

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
VISIBILITY_SECONDS = 900
HEARTBEAT_SECONDS = 300
CLEANUP_INTERVAL_SECONDS = 3600


class _Heartbeat:
    """処理が長くなっても、メッセージが別のワーカーに渡らないよう見えない時間を延ばし続ける."""

    def __init__(self, queue: JobQueue, msg: QueueMessage):
        self.queue, self.msg = queue, msg
        self.stop = threading.Event()
        self.t = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self.stop.wait(HEARTBEAT_SECONDS):
            try:
                self.queue.extend(self.msg, VISIBILITY_SECONDS)
            except Exception:  # noqa: BLE001 - 延長の失敗は致命的ではない（最悪、別のワーカーがやり直す）
                log.warning("heartbeat failed", extra={"job_id": self.msg.job_id})

    def __enter__(self):
        self.t.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.t.join(timeout=5)


class Worker:
    def __init__(self, settings: Settings, blobs: BlobStore, queue: JobQueue, repo: JobRepository, llm: LLMClient, notifier: Notifier, runner: ToolRunner | None = None):
        self.s = settings
        self.blobs, self.queue, self.repo = blobs, queue, repo
        self.llm, self.notifier = llm, notifier
        self.runner = runner or ToolRunner()
        self.stopping = threading.Event()
        self._last_cleanup = 0.0

    # ---- ループ
    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: self.stopping.set())
        signal.signal(signal.SIGINT, lambda *_: self.stopping.set())
        log.info("worker started", extra={"backend": self.s.backend, "llm": self.llm.name})
        while not self.stopping.is_set():
            if not self.run_once():
                self._cleanup_if_due()
                if self.s.backend == "azure":
                    self.stopping.wait(5)  # Storage Queue には長いポーリングが無い
        log.info("worker stopped")

    def run_once(self, wait_seconds: int = 10) -> bool:
        msgs = self.queue.receive(max_messages=1, wait_seconds=wait_seconds, visibility_seconds=VISIBILITY_SECONDS)
        for m in msgs:
            self.handle(m)
        return bool(msgs)

    def _cleanup_if_due(self) -> None:
        if time.monotonic() - self._last_cleanup < CLEANUP_INTERVAL_SECONDS:
            return
        self._last_cleanup = time.monotonic()
        try:
            n = self.repo.delete_expired(int(time.time()))
            if n:
                log.info("expired jobs deleted", extra={"count": n})
        except Exception:  # noqa: BLE001
            log.exception("cleanup failed")

    # ---- 1 件
    def handle(self, m: QueueMessage) -> None:
        if not valid_job_id(m.job_id):
            log.error("malformed message", extra={"receive_count": m.receive_count})
            self.queue.dead_letter(m)
            return
        try:
            job = self.repo.get(m.job_id)
        except NotFound:
            log.warning("job not found; dropping message", extra={"job_id": m.job_id})
            self.queue.delete(m)
            return
        if job.status in (JobStatus.SUCCEEDED, JobStatus.FAILED):
            self.queue.delete(m)  # 二重に届いたメッセージ
            return
        if m.receive_count > MAX_ATTEMPTS or job.attempts >= MAX_ATTEMPTS:
            self._finish(job, m, JobStatus.FAILED, error=f"{MAX_ATTEMPTS} 回やり直しても終わりませんでした")
            return
        previous = job.status
        job.status, job.attempts, job.updated_at, job.error = JobStatus.RUNNING, job.attempts + 1, iso(now()), ""
        try:
            self.repo.transition(job, expected=previous)
        except Conflict:
            log.info("job taken by another worker", extra={"job_id": job.job_id})
            return
        log.info("job started", extra={"job_id": job.job_id, "kind": job.kind.value, "attempt": job.attempts})
        t0 = time.monotonic()
        try:
            with _Heartbeat(self.queue, m):
                rep = self._process(job)
        except PermanentError as e:
            self._finish(job, m, JobStatus.FAILED, error=str(e)[:500])
            return
        except (RetryableError, Exception) as e:  # noqa: BLE001 - 想定外の失敗もやり直しの対象にする
            level = logging.WARNING if isinstance(e, RetryableError) else logging.ERROR
            log.log(level, "job attempt failed", extra={"job_id": job.job_id, "attempt": job.attempts, "error": type(e).__name__}, exc_info=level == logging.ERROR)
            if job.attempts >= MAX_ATTEMPTS:
                self._finish(job, m, JobStatus.FAILED, error=f"やり直しても失敗しました（{type(e).__name__}）")
                return
            job.status, job.error, job.updated_at = JobStatus.QUEUED, f"一時的な失敗のため再試行します（{type(e).__name__}）", iso(now())
            with contextlib.suppress(Conflict):
                self.repo.transition(job, expected=JobStatus.RUNNING)
            # メッセージは消さない. 見えない時間が切れたら、またどこかのワーカーに届く
            self.queue.extend(m, 60 * job.attempts)
            return
        job.summary = report.summary(rep)
        job.cost_usd = rep["cost_usd"]
        log.info(
            "job processed",
            extra={"job_id": job.job_id, "seconds": round(time.monotonic() - t0, 1), "cost_usd": job.cost_usd, **{f"n_{k}": v for k, v in job.summary["severity"].items()}},
        )
        self._finish(job, m, JobStatus.SUCCEEDED)

    def _finish(self, job: Job, m: QueueMessage, status: JobStatus, error: str = "") -> None:
        previous = job.status
        job.status, job.error, job.updated_at = status, error, iso(now())
        try:
            self.repo.transition(job, expected=previous)
        except Conflict:
            log.warning("job state changed concurrently", extra={"job_id": job.job_id})
            return
        self.queue.delete(m)
        if status == JobStatus.FAILED:
            log.warning("job failed", extra={"job_id": job.job_id, "error": error[:200]})
        url = f"{self.s.public_base_url}/v1/jobs/{job.job_id}/report" if self.s.public_base_url else ""
        self.notifier.notify(job, url)

    # ---- 中身
    def _process(self, job: Job) -> dict:
        data = self.blobs.get(job.input_key)
        if job.kind == JobKind.DIFF:
            from .pipeline.diffreview import review_diff

            text = data.decode("utf-8")
            if self.llm.name == "none":
                raise PermanentError("RP_LLM=none のため、差分のレビューはできません")
            out = review_diff(text, self.llm, self.s.llm_model_standard, self.s.llm_model_deep, self.s.budget_per_job_usd, job.title)
            rep = report.build(job, out.findings, out.calls, out.notes, routing=out.routing)
        else:
            from .pipeline.document import DocumentReviewer

            with tempfile.TemporaryDirectory(prefix="rp-") as tmp:
                conv = to_markdown(job.filename, data, Path(tmp), self.runner)
            findings, calls, notes = [], [], []
            if self.llm.name == "none":
                notes.append("RP_LLM=none のため、変換と変換ロスの確認だけを行いました")
            else:
                out = DocumentReviewer(self.llm, self.s.llm_model_standard, self.s.llm_model_deep, self.s.budget_per_job_usd).review(job.filename, conv.markdown)
                findings, calls, notes = out.findings, out.calls, out.notes
            rep = report.build(job, findings, calls, notes, warnings=conv.warnings)
            self.blobs.put(f"results/{job.job_id}/document.md", conv.markdown.encode("utf-8"), "text/markdown; charset=utf-8")
        rep["job"]["status"] = JobStatus.SUCCEEDED.value  # 保存するのは成功したときだけ
        rep["job"]["summary"] = report.summary(rep)
        self.blobs.put(job.report_json_key, json.dumps(rep, ensure_ascii=False, indent=1).encode("utf-8"), "application/json")
        self.blobs.put(job.report_md_key, report.markdown(rep).encode("utf-8"), "text/markdown; charset=utf-8")
        return rep
