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

    def complete(self, model: str, system: str, user: str, max_tokens: int) -> LLMReply: ...


class Notifier(Protocol):
    def notify(self, job: Job, url: str) -> None: ...
