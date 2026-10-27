"""ワーカー. キューからジョブを 1 件ずつ取り、変換 → AI レビュー → 結果の保存 → 通知 を行う.

- 冪等: ジョブを取るときは「状態と試行回数が読んだときのまま」の条件付き更新. 2 台が同時に取っても 1 台しか進まない
- 処理中のジョブには貸し出し期限（updated_at + LEASE_SECONDS）があり、期限内に届いた重複メッセージは見送る
- 処理中は定期的にメッセージの「見えない時間」とジョブの貸し出し期限を延ばす
- 一時的な失敗はキューに戻してやり直す. MAX_ATTEMPTS 回失敗したら失敗にする
- 費用はやり直しをまたいで足し上げ、残りの予算だけを次の試行に渡す
- 費用の上限（1 ジョブ・日次の全体・日次の利用者ごと）は既定で 0. 0 は「有料の LLM（anthropic・bedrock）を一切呼ばない」.
  有料の LLM は 3 つとも 0 より大きい値を明示的に設定したときだけ呼ぶ. 疑似 LLM（fake）と none は費用 0 なので上限 0 でも動く
  （fake で上限を明示したときは、fake が返す見積もりの費用で上限を試せる）
- 1 日（UTC）の費用の上限（全体・利用者ごと）を、共有の保存先（ジョブ表）の集計で確かめる. 使える予算は
  「1 ジョブの上限の残り」と「日次の上限の残り」の小さい方. 日次の上限に達していれば LLM を呼ばずに失敗にする
- 失敗した呼び出し（出力が max_tokens で切れた、など）の費用も、ジョブと日次の集計に足す. 課金されたかもしれないが額の
  分からない失敗（5xx・タイムアウト・接続の切断など. LLMError.maybe_billed）は、見積もり額（出力は上限いっぱい）で数える
- 日次の上限があるときは、LLM を 1 回呼ぶごとに、呼ぶ前に見積もり額（出力は上限いっぱい）を共有の集計に予約する（条件付き更新.
  複数のワーカーが同時に呼んでも上限を超えない）. 呼んだ後に実額との差を精算する. 予約できなければ呼ばずに失敗にする
- SIGTERM を受けたら、処理中のジョブを終えてから止まる
- 1 件の処理で想定外の例外（DynamoDB のスロットリングなど）が出ても、ログを出して次へ進む（ワーカーは落とさない）.
  メッセージは消していないので、見えない時間が切れたら再配信され、やり直される
"""

from __future__ import annotations

import json
import logging
import math
import signal
import tempfile
import threading
import time
from pathlib import Path

from .config import PAID_LLMS, Settings
from .domain import Job, JobKind, JobStatus, iso, now, parse_iso, valid_job_id
from .pipeline import PermanentError, RetryableError, report
from .pipeline.convert import ToolRunner, to_markdown
from .ports import SPEND_TOTAL, BlobStore, Conflict, JobQueue, JobRepository, LLMClient, LLMReply, NotFound, Notifier, QueueMessage

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
VISIBILITY_SECONDS = 900
HEARTBEAT_SECONDS = 300
# 心拍が止まってからこの秒数が過ぎたら、別のワーカーが引き継げる. メッセージの見えない時間より短くする:
# 同じ長さだと、ワーカーが落ちた後に再配信されたメッセージがまだ貸し出し中のジョブに当たって見送られ、受信回数を 1 回むだにする
# （MAX_ATTEMPTS が実質 1 回減る）. 心拍は先にジョブの期限を延ばしてからメッセージを延ばすので、期限は必ず再配信より前に切れる
LEASE_MARGIN_SECONDS = 60
LEASE_SECONDS = VISIBILITY_SECONDS - LEASE_MARGIN_SECONDS
CLEANUP_INTERVAL_SECONDS = 3600


class _CostMeter:
    """LLM の呼び出しを数える. 試行が途中で失敗しても、それまでに使った費用をジョブに足せるように.

    日次の上限があるとき（reserve=True）は、呼び出しの前に見積もり額（伏せ字の後の入力 + 出力の上限いっぱい）を共有の集計に
    予約し（JobRepository.reserve_spend. 条件付き更新なので、複数のワーカーが同時に呼んでも上限を超えない）、呼び出しの後に
    実額との差を精算する. 予約できなければ呼ばずに LLMError（再試行しない）. 日次の上限が無いときは、呼んだ後に実額を足すだけ.
    """

    def __init__(self, llm: LLMClient, repo: JobRepository | None = None, owner: str = "", limits: tuple[float, float] = (0.0, 0.0), reserve: bool = False):
        self.llm, self.name, self.spent = llm, llm.name, 0.0
        self.repo, self.owner, self.limits, self.reserve = repo, owner, limits, reserve

    @property
    def masker(self):
        return getattr(self.llm, "masker", None)

    def estimate(self, model: str, system: str, user: str, max_tokens: int) -> float:
        """予約する額. 外部に送るのと同じ伏せ字をかけた後の文字列で数え、出力は上限いっぱいとみなす."""
        from auto_review.tokens import estimate_tokens

        from .adapters.llm import cost

        m = self.masker
        if m is not None:
            system, user = m.text(system), m.text(user)
        return cost(model, estimate_tokens(system + user, 3.0), max_tokens)

    def complete(self, model: str, system: str, user: str, max_tokens: int, schema: dict | None = None) -> LLMReply:
        from .adapters.llm import LLMError

        held = 0.0
        # 予約と精算は同じ日付キーにする（UTC の日付をまたいで呼び出しが終わっても、予約した日の集計で精算する.
        # 以前は精算のときに日付を取り直していたので、予約額が前の日に残り、次の日の集計が負にずれた）
        day = _today()
        if self.reserve and self.repo is not None:
            held = self.estimate(model, system, user, max_tokens)
            try:
                over = self.repo.reserve_spend(day, self.owner, held, *self.limits)
            except Exception as e:  # noqa: BLE001 - 集計の保存先の失敗. 呼ばずに、一時的な失敗としてやり直す
                raise LLMError(f"日次の費用の予約に失敗しました（{type(e).__name__}）", retryable=True) from e
            if over:
                label, limit = ("全体", self.limits[0]) if over == SPEND_TOTAL else ("利用者ごと", self.limits[1])
                raise LLMError(f"本日（UTC）の LLM 費用の上限（{label} ${limit:.2f}）を超える見込み（見積もり ${held:.2f}）のため、LLM を呼びませんでした")
        try:
            r = self.llm.complete(model, system, user, max_tokens, schema=schema)
        except Exception as e:
            # 失敗しても課金された分（出力が max_tokens で切れた、など. LLMError.cost_usd）を数える.
            # 課金されたかもしれないが額の分からない失敗（5xx・タイムアウトなど）は、予約を 0 に戻さず、見積もり額
            # （出力は上限いっぱい）で数える（費用を少なく数えない）
            used = float(getattr(e, "cost_usd", 0.0) or 0.0)
            if self._maybe_billed(e):
                estimate = held or self.estimate(model, system, user, max_tokens)
                if estimate > used:
                    log.warning("llm call failed and may have been billed; counting the estimate", extra={"cost_usd": estimate, "error": type(e).__name__})
                    used = estimate
            self.spent += used
            self._settle(day, used, held)
            raise
        self.spent += r.cost_usd
        self._settle(day, r.cost_usd, held)
        return r

    def _maybe_billed(self, e: Exception) -> bool:
        """課金されたかもしれない失敗か. LLMError はその判断（maybe_billed）に従う. 想定外の例外（botocore の読み取りの
        タイムアウトなど）は、有料の LLM なら安全側に True."""
        from .adapters.llm import LLMError

        if isinstance(e, LLMError):
            return e.maybe_billed
        return self.name in PAID_LLMS

    def _settle(self, day: str, used: float, held: float) -> None:
        """日次の集計を実額にそろえる（予約していれば差額を足す. 負もありうる）. 集計の失敗でジョブを失敗・やり直しにしない
        （二重に費用をかけないため）. ログに残す. day は予約したときの日付（呼び出しの前に決めたもの）."""
        delta = used - held
        if self.repo is None or abs(delta) < 1e-12:
            return
        try:
            self.repo.add_spend(day, self.owner, delta)
        except Exception:  # noqa: BLE001
            log.exception("daily spend not recorded", extra={"cost_usd": used, "reserved_usd": held})


def _today() -> str:
    return now().strftime("%Y-%m-%d")


class _Heartbeat:
    """処理が長くなっても、メッセージとジョブが別のワーカーに渡らないよう、見えない時間と貸し出し期限を延ばし続ける."""

    def __init__(self, queue: JobQueue, msg: QueueMessage, repo: JobRepository, job: Job):
        self.queue, self.msg, self.repo = queue, msg, repo
        self.job = Job.from_dict(job.to_dict())  # 心拍で書き換えるのは写し（本体の状態は変えない）
        self.stop = threading.Event()
        self.t = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self.stop.wait(HEARTBEAT_SECONDS):
            # 先にジョブの貸し出し期限を延ばし、その後でメッセージを延ばす（期限がメッセージの再配信より後まで残らないように）.
            # 片方が失敗しても、もう片方は行う（延長の失敗は致命的ではない. 最悪、別のワーカーがやり直し、終わりの更新は条件で弾かれる）
            for what, extend in (("lease", self._extend_lease), ("message", self._extend_message)):
                try:
                    extend()
                except Exception:  # noqa: BLE001
                    log.warning("heartbeat failed", extra={"job_id": self.msg.job_id, "target": what})

    def _extend_lease(self) -> None:
        self.job.updated_at = iso(now())
        self.repo.transition(self.job, expected=JobStatus.RUNNING, expected_attempts=self.job.attempts)

    def _extend_message(self) -> None:
        self.queue.extend(self.msg, VISIBILITY_SECONDS)

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
        if self.llm.name in PAID_LLMS and self.s.unset_budgets():
            log.warning("paid LLM disabled: budget not set", extra={"llm": self.llm.name, "unset": self.s.unset_budgets()})
        while not self.stopping.is_set():
            try:
                got = self.run_once()
            except Exception:  # noqa: BLE001 - キューの受信の失敗など. 落ちずに少し待ってから続ける
                log.exception("worker loop failed; retrying")
                self.stopping.wait(5)
                continue
            if not got:
                self._cleanup_if_due()
                if self.s.backend == "azure":
                    self.stopping.wait(5)  # Storage Queue には長いポーリングが無い
        log.info("worker stopped")

    def run_once(self, wait_seconds: int = 10) -> bool:
        msgs = self.queue.receive(max_messages=1, wait_seconds=wait_seconds, visibility_seconds=VISIBILITY_SECONDS)
        for m in msgs:
            try:
                self.handle(m)
            except Exception:  # noqa: BLE001 - 1 件の失敗でワーカーを落とさない
                # メッセージは消さない. 見えない時間が切れたら再配信され、別の試行でやり直す
                log.exception("message handling failed; will be redelivered", extra={"job_id": m.job_id, "receive_count": m.receive_count})
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
        if job.status == JobStatus.RUNNING and _lease_active(job):
            # 別のワーカーが処理中（重複して届いたメッセージ）. 消さずに見送り、見えない時間が切れたら確かめ直す
            log.info("job leased by another worker; skipping", extra={"job_id": job.job_id})
            return
        if m.receive_count > MAX_ATTEMPTS or job.attempts >= MAX_ATTEMPTS:
            self._finish(job, m, JobStatus.FAILED, error=f"{MAX_ATTEMPTS} 回やり直しても終わりませんでした")
            return
        previous, prev_attempts = job.status, job.attempts
        job.status, job.attempts, job.updated_at, job.error = JobStatus.RUNNING, job.attempts + 1, iso(now()), ""
        try:
            self.repo.transition(job, expected=previous, expected_attempts=prev_attempts)
        except Conflict:
            log.info("job taken by another worker", extra={"job_id": job.job_id})
            return
        log.info("job started", extra={"job_id": job.job_id, "kind": job.kind.value, "attempt": job.attempts})
        t0 = time.monotonic()
        limits = (self.s.budget_daily_usd, self.s.budget_daily_per_owner_usd)
        meter = _CostMeter(self.llm, self.repo, job.owner, limits, reserve=self.llm.name != "none" and any(limits))
        try:
            remaining = self._remaining_budget(job)
            with _Heartbeat(self.queue, m, self.repo, job):
                rep = self._process(job, meter, remaining_budget=remaining)
        except PermanentError as e:
            job.cost_usd += meter.spent
            self._finish(job, m, JobStatus.FAILED, error=str(e)[:500])
            return
        except (RetryableError, Exception) as e:  # noqa: BLE001 - 想定外の失敗もやり直しの対象にする
            job.cost_usd += meter.spent
            level = logging.WARNING if isinstance(e, RetryableError) else logging.ERROR
            log.log(
                level,
                "job attempt failed",
                extra={"job_id": job.job_id, "attempt": job.attempts, "error": type(e).__name__, "cost_usd": job.cost_usd},
                exc_info=level == logging.ERROR,
            )
            why = _reason(e)
            if job.attempts >= MAX_ATTEMPTS:
                self._finish(job, m, JobStatus.FAILED, error=f"やり直しても失敗しました（{why}）"[:500])
                return
            job.status, job.error, job.updated_at = JobStatus.QUEUED, f"一時的な失敗のため再試行します（{why}）"[:500], iso(now())
            try:
                self.repo.transition(job, expected=JobStatus.RUNNING, expected_attempts=job.attempts)
            except Conflict:
                log.warning("job state changed concurrently", extra={"job_id": job.job_id})
                return
            # メッセージは消さない. 見えない時間が切れたら、またどこかのワーカーに届く
            self.queue.extend(m, 60 * job.attempts)
            return
        job.summary = report.summary(rep)
        job.cost_usd += max(rep["cost_usd"], meter.spent)  # 結果に残らない失敗した呼び出しの分も足す
        log.info(
            "job processed",
            extra={"job_id": job.job_id, "seconds": round(time.monotonic() - t0, 1), "cost_usd": job.cost_usd, **{f"n_{k}": v for k, v in job.summary["severity"].items()}},
        )
        self._finish(job, m, JobStatus.SUCCEEDED)

    # ---- 予算
    def _remaining_budget(self, job: Job) -> float:
        """この試行で使える予算. 1 ジョブの上限の残りと、日次の上限（全体・利用者ごと）の残りの小さい方.

        上限の 0 は「有料の LLM を呼ばない」. 有料の LLM（anthropic・bedrock）で上限が 1 つでも 0 なら PermanentError（LLM を呼ばない）.
        費用のかからない LLM（fake・none）では 0 の上限は効かせない（明示した上限だけを、fake の見積もりの費用で確かめる）.
        日次の上限に達していれば PermanentError（LLM を呼ばない）. LLM を使わない設定（RP_LLM=none）では日次の集計を読まない.
        """
        paid = self.llm.name in PAID_LLMS
        if paid and (unset := self.s.unset_budgets()):
            raise PermanentError(
                f"費用の上限（{'・'.join(unset)}）が 0（既定）のため、有料の LLM（{self.llm.name}）は呼びませんでした. "
                "AI レビューを使うときは、3 つの上限をすべて 0 より大きい値に設定してください"
            )
        # 1 ジョブの上限が 0 なのは、費用のかからない LLM のときだけ（上で止めている）. そのときは上限を効かせない
        remaining = max(0.0, self.s.budget_per_job_usd - job.cost_usd) if self.s.budget_per_job_usd else math.inf
        if self.llm.name == "none" or not (self.s.budget_daily_usd or self.s.budget_daily_per_owner_usd):
            return remaining
        total, mine = self.repo.spent(_today(), job.owner)
        for limit, used, label in (
            (self.s.budget_daily_usd, total, "全体"),
            (self.s.budget_daily_per_owner_usd, mine, "利用者ごと"),
        ):
            if not limit:
                continue
            if used >= limit:
                raise PermanentError(f"本日（UTC）の LLM 費用の上限（{label} ${limit:.2f}）に達しているため、AI レビューを行いませんでした. 明日以降に送り直してください")
            remaining = min(remaining, limit - used)
        return remaining

    def _finish(self, job: Job, m: QueueMessage, status: JobStatus, error: str = "") -> None:
        previous = job.status
        job.status, job.error, job.updated_at = status, error, iso(now())
        try:
            self.repo.transition(job, expected=previous, expected_attempts=job.attempts)
        except Conflict:
            log.warning("job state changed concurrently", extra={"job_id": job.job_id})
            return
        self.queue.delete(m)
        if status == JobStatus.FAILED:
            log.warning("job failed", extra={"job_id": job.job_id, "error": error[:200]})
        url = f"{self.s.public_base_url}/v1/jobs/{job.job_id}/report" if self.s.public_base_url else ""
        self.notifier.notify(job, url)

    # ---- 中身
    def _process(self, job: Job, llm: LLMClient, remaining_budget: float) -> dict:
        data = self.blobs.get(job.input_key)
        if job.kind == JobKind.DIFF:
            from .pipeline.diffreview import review_diff

            text = data.decode("utf-8")
            if llm.name == "none":
                raise PermanentError("RP_LLM=none のため、差分のレビューはできません")
            out = review_diff(text, llm, self.s.llm_model_standard, self.s.llm_model_deep, remaining_budget, job.title)
            rep = report.build(job, out.findings, out.calls, out.notes, routing=out.routing)
        else:
            from .pipeline.document import DocumentReviewer

            with tempfile.TemporaryDirectory(prefix="rp-") as tmp:
                conv = to_markdown(job.filename, data, Path(tmp), self.runner)
            findings, calls, notes, partial = [], [], [], None
            if llm.name == "none":
                notes.append("RP_LLM=none のため、変換と変換ロスの確認だけを行いました")
            else:
                out = DocumentReviewer(llm, self.s.llm_model_standard, self.s.llm_model_deep, remaining_budget).review(job.filename, conv.markdown)
                findings, calls, notes, partial = out.findings, out.calls, out.notes, out.partial
            rep = report.build(job, findings, calls, notes, warnings=conv.warnings, partial=partial)
            self.blobs.put(f"results/{job.job_id}/document.md", conv.markdown.encode("utf-8"), "text/markdown; charset=utf-8")
        spent = getattr(llm, "spent", None)
        if spent is not None and spent > rep["cost_usd"] + 1e-6:
            # 失敗した呼び出し（出力が max_tokens で切れた、など）の費用も結果の費用に含める
            rep["notes"].append(f"失敗した LLM の呼び出しの費用 ${spent - rep['cost_usd']:.4f} を費用に含めています")
            rep["cost_usd"] = round(spent, 4)
        rep["job"]["status"] = JobStatus.SUCCEEDED.value  # 保存するのは成功したときだけ
        rep["job"]["summary"] = report.summary(rep)
        self.blobs.put(job.report_json_key, json.dumps(rep, ensure_ascii=False, indent=1).encode("utf-8"), "application/json")
        self.blobs.put(job.report_md_key, report.markdown(rep).encode("utf-8"), "text/markdown; charset=utf-8")
        return rep


def _reason(e: BaseException) -> str:
    """ジョブのエラーに残す理由. RetryableError はこのパッケージが作った文（doc2md の stderr の要点・LLM の失敗. 伏せ字済み）なので中身も残す.
    想定外の例外は、中身に何が入るか分からないので型の名前だけ."""
    if isinstance(e, RetryableError) and str(e):
        return f"{type(e).__name__}: {str(e)[:400]}"
    return type(e).__name__


def _lease_active(job: Job) -> bool:
    try:
        return (now() - parse_iso(job.updated_at)).total_seconds() < LEASE_SECONDS
    except ValueError:
        return False
