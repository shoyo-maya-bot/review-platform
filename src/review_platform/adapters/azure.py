"""Azure の実装（Blob・Storage Queue・Table）. 認証はマネージド ID. Azurite のときだけ接続文字列を使う.

Storage Queue には DLQ が無いので、受信回数が上限を超えたメッセージは `<キュー名>-poison` に移す.
Table Storage には TTL が無いので、期限切れの行はワーカーが定期的に消す（delete_expired）.
日次の費用は同じ表の別のパーティション（spend）に置き、ETag の条件付き更新で足す（同時に足しても失われない）.
呼び出しの前の予約（reserve_spend）は、全体と利用者の 2 行を同じパーティションのトランザクション（ETag 付き）で一度に足す.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any

from azure.core import MatchConditions
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError
from azure.data.tables import TableClient, TableTransactionError, UpdateMode
from azure.storage.blob import BlobServiceClient, ContentSettings
from azure.storage.queue import QueueClient

from ..domain import Job, JobStatus
from ..ports import SPEND_ALL, SPEND_OWNER, SPEND_TOTAL, Conflict, NotFound, QueueMessage, spend_expires_at, spend_scope

PARTITION = "job"
SPEND_PARTITION = "spend"


def _credential():
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential(exclude_interactive_browser_credential=True)


class AzureBlobStore:
    def __init__(self, container: str, account_url: str = "", connection_string: str = ""):
        svc = BlobServiceClient.from_connection_string(connection_string) if connection_string else BlobServiceClient(account_url, credential=_credential())
        self.container = svc.get_container_client(container)

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        self.container.upload_blob(key, data, overwrite=True, content_settings=ContentSettings(content_type=content_type))

    def get(self, key: str) -> bytes:
        try:
            return self.container.download_blob(key).readall()
        except ResourceNotFoundError as e:
            raise NotFound(key) from e


class AzureQueue:
    def __init__(self, queue_name: str, account_url: str = "", connection_string: str = "", max_dequeue: int = 5):
        if connection_string:
            self.q = QueueClient.from_connection_string(connection_string, queue_name)
            self.poison = QueueClient.from_connection_string(connection_string, f"{queue_name}-poison")
        else:
            cred = _credential()
            self.q = QueueClient(account_url, queue_name, credential=cred)
            self.poison = QueueClient(account_url, f"{queue_name}-poison", credential=cred)
        self.max_dequeue = max_dequeue

    def send(self, job_id: str) -> None:
        self.q.send_message(json.dumps({"job_id": job_id}))

    def receive(self, max_messages: int = 1, wait_seconds: int = 10, visibility_seconds: int = 900) -> list[QueueMessage]:
        # Storage Queue には長いポーリングが無い. 空のときの待ちはワーカー側で行う
        out = []
        for m in self.q.receive_messages(messages_per_page=max(1, min(max_messages, 32)), visibility_timeout=visibility_seconds, max_messages=max_messages):
            try:
                job_id = str(json.loads(m.content)["job_id"])
            except (ValueError, KeyError, TypeError):
                job_id = ""
            msg = QueueMessage(job_id=job_id, receipt=f"{m.id}|{m.pop_receipt}", receive_count=int(m.dequeue_count or 1))
            if msg.receive_count > self.max_dequeue:
                self.dead_letter(msg)
                continue
            out.append(msg)
        return out

    @staticmethod
    def _split(message: QueueMessage) -> tuple[str, str]:
        mid, _, pop = message.receipt.partition("|")
        return mid, pop

    def delete(self, message: QueueMessage) -> None:
        mid, pop = self._split(message)
        # 見えない時間が切れて別のワーカーが受け取っていたら、そちらに任せる
        with contextlib.suppress(ResourceNotFoundError):
            self.q.delete_message(mid, pop)

    def extend(self, message: QueueMessage, visibility_seconds: int) -> None:
        mid, pop = self._split(message)
        res = self.q.update_message(mid, pop_receipt=pop, visibility_timeout=visibility_seconds)
        message.receipt = f"{mid}|{res.pop_receipt}"  # 更新のたびに pop receipt が変わる

    def dead_letter(self, message: QueueMessage) -> None:
        self.poison.send_message(json.dumps({"job_id": message.job_id, "receive_count": message.receive_count}))
        self.delete(message)


class AzureJobRepository:
    """行: PartitionKey=job, RowKey=job_id. 利用者ごとの一覧は owner で絞り込む（件数が少ない前提. 多くなったら索引用の行を足す）."""

    def __init__(self, table: str, account_url: str = "", connection_string: str = ""):
        self.t = TableClient.from_connection_string(connection_string, table) if connection_string else TableClient(account_url, table, credential=_credential())

    @staticmethod
    def _entity(job: Job) -> dict[str, Any]:
        return {
            "PartitionKey": PARTITION,
            "RowKey": job.job_id,
            "owner": job.owner,
            "status": job.status.value,
            "created_at": job.created_at,
            "expires_at": job.expires_at,
            "data": json.dumps(job.to_dict(), ensure_ascii=False),
        }

    def create(self, job: Job) -> None:
        try:
            self.t.create_entity(self._entity(job))
        except ResourceExistsError as e:
            raise Conflict(job.job_id) from e

    def _raw(self, job_id: str):
        try:
            return self.t.get_entity(PARTITION, job_id)
        except ResourceNotFoundError as e:
            raise NotFound(job_id) from e

    def get(self, job_id: str) -> Job:
        return Job.from_dict(json.loads(self._raw(job_id)["data"]))

    def transition(self, job: Job, expected: JobStatus, expected_attempts: int | None = None) -> None:
        cur = self._raw(job.job_id)
        if cur["status"] != expected.value:
            raise Conflict(job.job_id)
        if expected_attempts is not None and int(json.loads(cur["data"]).get("attempts", 0) or 0) != expected_attempts:
            raise Conflict(job.job_id)
        try:
            self.t.update_entity(self._entity(job), mode=UpdateMode.REPLACE, etag=cur.metadata["etag"], match_condition=MatchConditions.IfNotModified)
        except ResourceModifiedError as e:
            raise Conflict(job.job_id) from e

    def list_by_owner(self, owner: str, limit: int = 20) -> list[Job]:
        rows = self.t.query_entities("PartitionKey eq @p and owner eq @o", parameters={"p": PARTITION, "o": owner}, select=["data", "created_at"])
        jobs = sorted((Job.from_dict(json.loads(r["data"])) for r in rows), key=lambda j: j.job_id, reverse=True)
        return jobs[:limit]

    def delete_expired(self, now_epoch: int, limit: int = 100) -> int:
        n = 0
        rows = self.t.query_entities("PartitionKey eq @p and expires_at gt 0 and expires_at lt @n", parameters={"p": PARTITION, "n": now_epoch}, select=["RowKey"])
        for r in rows:
            if n >= limit:
                break
            self.t.delete_entity(PARTITION, r["RowKey"])
            n += 1
        old = self.t.query_entities("PartitionKey eq @p and expires_at lt @n", parameters={"p": SPEND_PARTITION, "n": now_epoch}, select=["RowKey"])
        for i, r in enumerate(old):
            if i >= limit:
                break
            with contextlib.suppress(ResourceNotFoundError):
                self.t.delete_entity(SPEND_PARTITION, r["RowKey"])
        return n

    def add_spend(self, day: str, owner: str, usd: float, attempts: int = 10) -> None:
        for scope in (SPEND_ALL, spend_scope(owner)):
            row = f"{day}|{scope}"
            for _ in range(attempts):
                try:
                    cur = self.t.get_entity(SPEND_PARTITION, row)
                except ResourceNotFoundError:
                    try:
                        self.t.create_entity({"PartitionKey": SPEND_PARTITION, "RowKey": row, "usd": float(usd), "expires_at": spend_expires_at(day)})
                        break
                    except ResourceExistsError:
                        continue  # 別のワーカーが先に作った. 読み直して足す
                cur["usd"] = float(cur.get("usd", 0.0)) + float(usd)
                try:
                    self.t.update_entity(cur, mode=UpdateMode.REPLACE, etag=cur.metadata["etag"], match_condition=MatchConditions.IfNotModified)
                    break
                except ResourceModifiedError:
                    continue  # 同時に更新された. 読み直して足す
            else:
                raise Conflict(f"spend {row}")

    def reserve_spend(self, day: str, owner: str, usd: float, limit_total: float, limit_owner: float, attempts: int = 10) -> str | None:
        scopes = ((SPEND_ALL, limit_total, SPEND_TOTAL), (spend_scope(owner), limit_owner, SPEND_OWNER))
        for _ in range(attempts):
            ops: list[tuple] = []
            for scope, limit, label in scopes:
                row = f"{day}|{scope}"
                try:
                    cur = self.t.get_entity(SPEND_PARTITION, row)
                except ResourceNotFoundError:
                    cur = None
                used = float(cur.get("usd", 0.0)) if cur is not None else 0.0
                if limit and used + usd > limit + 1e-9:
                    return label
                if cur is None:
                    ops.append(("create", {"PartitionKey": SPEND_PARTITION, "RowKey": row, "usd": float(usd), "expires_at": spend_expires_at(day)}))
                else:
                    cur["usd"] = used + float(usd)
                    ops.append(("update", cur, {"mode": UpdateMode.REPLACE, "etag": cur.metadata["etag"], "match_condition": MatchConditions.IfNotModified}))
            try:
                self.t.submit_transaction(ops)  # 2 行とも同じパーティション. どちらかが読んだ後に変わっていれば両方とも書かない
                return None
            except (TableTransactionError, ResourceModifiedError, ResourceExistsError):
                continue  # 同時に更新された. 読み直して確かめ直す
        raise Conflict(f"spend reserve {day}")

    def spent(self, day: str, owner: str) -> tuple[float, float]:
        out = []
        for scope in (SPEND_ALL, spend_scope(owner)):
            try:
                out.append(float(self.t.get_entity(SPEND_PARTITION, f"{day}|{scope}").get("usd", 0.0)))
            except ResourceNotFoundError:
                out.append(0.0)
        return out[0], out[1]
