"""外側（保存・キュー・ジョブ表・LLM・通知）との境目. 実装は adapters/ にある."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .domain import Job, JobStatus


class NotFound(KeyError):
    pass


class Conflict(RuntimeError):
    """条件付き更新が負けた（別のワーカーが先に取った、など）."""


class BlobStore(Protocol):
    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None: ...
    def get(self, key: str) -> bytes: ...  # 無ければ NotFound


@dataclass
class QueueMessage:
    job_id: str
    receipt: str  # 削除・見えない時間の延長に使う
    receive_count: int = 1


class JobQueue(Protocol):
    def send(self, job_id: str) -> None: ...
    def receive(self, max_messages: int = 1, wait_seconds: int = 10, visibility_seconds: int = 900) -> list[QueueMessage]: ...
    def delete(self, message: QueueMessage) -> None: ...
    def extend(self, message: QueueMessage, visibility_seconds: int) -> None: ...
    def dead_letter(self, message: QueueMessage) -> None: ...  # 何度も失敗したメッセージを退避（SQS は DLQ が自動でやるので削除しない）


class JobRepository(Protocol):
    def create(self, job: Job) -> None: ...
    def get(self, job_id: str) -> Job: ...  # 無ければ NotFound
    # 状態が expected（と、指定があれば attempts が expected_attempts）のときだけ上書き. 違えば Conflict
    def transition(self, job: Job, expected: JobStatus, expected_attempts: int | None = None) -> None: ...
    def list_by_owner(self, owner: str, limit: int = 20) -> list[Job]: ...
    def delete_expired(self, now_epoch: int, limit: int = 100) -> int: ...  # TTL の無い保存先のため
    # 日次の LLM 費用（全体と利用者ごと）. 複数のワーカーで共有するため、ジョブ表と同じ保存先に置く（day は UTC の YYYY-MM-DD）
    def add_spend(self, day: str, owner: str, usd: float) -> None: ...  # 全体と owner の両方に足す
    def spent(self, day: str, owner: str) -> tuple[float, float]: ...  # （全体, owner）. 無ければ 0（予約中の額を含む）
    # 呼び出しの前の予約. 足した後の額が上限（0 は上限なし）を超えないときだけ、全体と owner の両方に一度に足して None.
    # 超えるなら何も足さずに、超えた範囲（SPEND_TOTAL か SPEND_OWNER）を返す. 複数のワーカーが同時に予約しても上限を超えない
    # （条件付き更新）. 呼び出しの後は add_spend（実額 − 予約額. 負もありうる）で精算する
    def reserve_spend(self, day: str, owner: str, usd: float, limit_total: float, limit_owner: float) -> str | None: ...
    # API の件数制限を複数の台で共有するとき（RP_RATE_LIMIT_SHARED）. window（1 分ごとの区切りの UNIX 秒）の principal の件数が
    # limit 未満なら 1 足して True、limit に達していれば足さずに False（条件付き更新. 複数の台が同時に数えても超えない）
    def count_request(self, principal: str, window: int, limit: int) -> bool: ...


SPEND_ALL = "all"  # 全体の集計のキー
SPEND_TOTAL, SPEND_OWNER = "total", "owner"  # reserve_spend が返す「超えた範囲」
SPEND_TTL_DAYS = 40  # 日次の集計を残す日数（TTL・期限切れの削除に使う）


def spend_scope(owner: str) -> str:
    """利用者ごとの集計のキー. 保存先のキーに使えない文字（/ # ? など）を避けるため、利用者名はハッシュにする."""
    import hashlib

    return "u-" + hashlib.sha256(owner.encode("utf-8")).hexdigest()[:32]


RATE_WINDOW_SECONDS = 60  # 件数制限の区切り
RATE_TTL_SECONDS = 300  # 件数の記録を残す秒数（TTL・期限切れの削除に使う）


def rate_key(principal: str, window: int) -> str:
    """件数制限の記録のキー（利用者はハッシュにする. 費用の集計と同じ）."""
    return f"{window}#{spend_scope(principal)}"


def spend_expires_at(day: str) -> int:
    from datetime import UTC, datetime, timedelta

    return int((datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC) + timedelta(days=SPEND_TTL_DAYS)).timestamp())


@dataclass
class LLMReply:
    text: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    stop_reason: str = "end_turn"
    request_id: str = ""  # 窓口が返した request-id（問い合わせ用. 無ければ空）


class LLMClient(Protocol):
    name: str

    # schema を渡すと、対応する窓口は「そのスキーマを入力にした道具」を強制して構造化出力にし、道具の入力を JSON の文字列で text に返す.
    # 対応しない窓口は普通の文章を返す. どちらの場合も、受け取った側でスキーマを検証する
    def complete(self, model: str, system: str, user: str, max_tokens: int, schema: dict | None = None) -> LLMReply: ...


class Notifier(Protocol):
    def notify(self, job: Job, url: str) -> None: ...
