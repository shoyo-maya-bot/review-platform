"""AWS の実装（S3・SQS・DynamoDB）. 認証は実行ロール. RP_AWS_ENDPOINT_URL でエミュレーターにも向けられる."""

from __future__ import annotations

import json
import time
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from ..domain import Job, JobStatus
from ..ports import RATE_TTL_SECONDS, SPEND_ALL, SPEND_OWNER, SPEND_TOTAL, Conflict, NotFound, QueueMessage, rate_key, spend_expires_at, spend_scope

OWNER_INDEX = "owner-job-index"  # infra/aws の DynamoDB と同じ名前
_RETRY = Config(retries={"max_attempts": 5, "mode": "standard"}, connect_timeout=5, read_timeout=30)


def _client(service: str, region: str, endpoint_url: str = ""):
    kwargs: dict[str, Any] = {"region_name": region or None, "config": _RETRY}
    if endpoint_url:
        kwargs["endpoint_url"] = endpoint_url
    return boto3.client(service, **kwargs)


def _code(e: ClientError) -> str:
    return str(e.response.get("Error", {}).get("Code", ""))


class S3BlobStore:
    def __init__(self, bucket: str, region: str, endpoint_url: str = ""):
        self.bucket = bucket
        self.s3 = _client("s3", region, endpoint_url)

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)

    def get(self, key: str) -> bytes:
        try:
            return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except ClientError as e:
            if _code(e) in ("NoSuchKey", "404", "NotFound"):
                raise NotFound(key) from e
            raise


class SqsQueue:
    """失敗が続いたメッセージは、SQS のデッドレターキュー（redrive）が自動で退避する."""

    def __init__(self, queue_url: str, region: str, endpoint_url: str = ""):
        self.url = queue_url
        self.sqs = _client("sqs", region, endpoint_url)

    def send(self, job_id: str) -> None:
        self.sqs.send_message(QueueUrl=self.url, MessageBody=json.dumps({"job_id": job_id}))

    def receive(self, max_messages: int = 1, wait_seconds: int = 10, visibility_seconds: int = 900) -> list[QueueMessage]:
        res = self.sqs.receive_message(
            QueueUrl=self.url,
            MaxNumberOfMessages=max(1, min(max_messages, 10)),
            WaitTimeSeconds=max(0, min(wait_seconds, 20)),
            VisibilityTimeout=visibility_seconds,
            AttributeNames=["ApproximateReceiveCount"],
        )
        out = []
        for m in res.get("Messages", []):
            try:
                job_id = str(json.loads(m["Body"])["job_id"])
            except (ValueError, KeyError, TypeError):
                job_id = ""  # 壊れたメッセージ. ワーカーが退避する
            count = int(m.get("Attributes", {}).get("ApproximateReceiveCount", "1"))
            out.append(QueueMessage(job_id=job_id, receipt=m["ReceiptHandle"], receive_count=count))
        return out

    def delete(self, message: QueueMessage) -> None:
        self.sqs.delete_message(QueueUrl=self.url, ReceiptHandle=message.receipt)

    def extend(self, message: QueueMessage, visibility_seconds: int) -> None:
        self.sqs.change_message_visibility(QueueUrl=self.url, ReceiptHandle=message.receipt, VisibilityTimeout=visibility_seconds)

    def dead_letter(self, message: QueueMessage) -> None:
        # すぐ見えるようにして受信回数を進め、maxReceiveCount を超えたら SQS が DLQ に移す
        self.sqs.change_message_visibility(QueueUrl=self.url, ReceiptHandle=message.receipt, VisibilityTimeout=0)


class DynamoJobRepository:
    """表: パーティションキー job_id. 利用者ごとの一覧は GSI（owner, job_id）. job_id は日時で始まるので新しい順に並ぶ. 期限切れは DynamoDB の TTL が消す.

    日次の費用も同じ表に置く（job_id = `spend#<日付>#<all か利用者のハッシュ>`、owner を持たないので一覧の GSI には出ない）.
    足すのは UpdateItem の ADD（複数のワーカーが同時に足しても失われない）. expires_at の TTL で消える.
    呼び出しの前の予約（reserve_spend）は、全体と利用者の 2 つの項目を TransactWriteItems で「足した後も上限以下」を条件に一度に足す.
    """

    def __init__(self, table: str, region: str, endpoint_url: str = ""):
        self.table = table
        self.ddb = _client("dynamodb", region, endpoint_url)

    @staticmethod
    def _item(job: Job) -> dict[str, Any]:
        return {
            "job_id": {"S": job.job_id},
            "owner": {"S": job.owner},
            "status": {"S": job.status.value},
            "created_at": {"S": job.created_at},
            "expires_at": {"N": str(job.expires_at)},
            "attempts": {"N": str(job.attempts)},
            "data": {"S": json.dumps(job.to_dict(), ensure_ascii=False)},
        }

    def create(self, job: Job) -> None:
        try:
            self.ddb.put_item(TableName=self.table, Item=self._item(job), ConditionExpression="attribute_not_exists(job_id)")
        except ClientError as e:
            if _code(e) == "ConditionalCheckFailedException":
                raise Conflict(job.job_id) from e
            raise

    def get(self, job_id: str) -> Job:
        res = self.ddb.get_item(TableName=self.table, Key={"job_id": {"S": job_id}}, ConsistentRead=True)
        item = res.get("Item")
        if not item:
            raise NotFound(job_id)
        return Job.from_dict(json.loads(item["data"]["S"]))

    def transition(self, job: Job, expected: JobStatus, expected_attempts: int | None = None) -> None:
        cond, names, values = "#s = :expected", {"#s": "status"}, {":expected": {"S": expected.value}}
        if expected_attempts is not None:
            cond += " AND #a = :attempts"
            names["#a"] = "attempts"
            values[":attempts"] = {"N": str(expected_attempts)}
        try:
            self.ddb.put_item(TableName=self.table, Item=self._item(job), ConditionExpression=cond, ExpressionAttributeNames=names, ExpressionAttributeValues=values)
        except ClientError as e:
            if _code(e) == "ConditionalCheckFailedException":
                raise Conflict(job.job_id) from e
            raise

    def list_by_owner(self, owner: str, limit: int = 20) -> list[Job]:
        res = self.ddb.query(
            TableName=self.table,
            IndexName=OWNER_INDEX,
            KeyConditionExpression="#o = :o",
            ExpressionAttributeNames={"#o": "owner"},
            ExpressionAttributeValues={":o": {"S": owner}},
            ScanIndexForward=False,
            Limit=limit,
        )
        return [Job.from_dict(json.loads(i["data"]["S"])) for i in res.get("Items", [])]

    def delete_expired(self, now_epoch: int, limit: int = 100) -> int:
        return 0  # DynamoDB の TTL（expires_at）が消す

    @staticmethod
    def _spend_key(day: str, scope: str) -> dict[str, Any]:
        return {"job_id": {"S": f"spend#{day}#{scope}"}}

    def add_spend(self, day: str, owner: str, usd: float) -> None:
        for scope in (SPEND_ALL, spend_scope(owner)):
            self.ddb.update_item(
                TableName=self.table,
                Key=self._spend_key(day, scope),
                UpdateExpression="ADD usd :u SET expires_at = :e",
                ExpressionAttributeValues={":u": {"N": repr(float(usd))}, ":e": {"N": str(spend_expires_at(day))}},
            )

    def reserve_spend(self, day: str, owner: str, usd: float, limit_total: float, limit_owner: float, attempts: int = 5) -> str | None:
        scopes = ((SPEND_ALL, limit_total, SPEND_TOTAL), (spend_scope(owner), limit_owner, SPEND_OWNER))
        for _, limit, label in scopes:
            if limit and usd > limit:  # 項目がまだ無いときの条件（attribute_not_exists）で通らないよう、先に弾く
                return label
        items = []
        for scope, limit, _ in scopes:
            update: dict[str, Any] = {
                "TableName": self.table,
                "Key": self._spend_key(day, scope),
                "UpdateExpression": "ADD usd :u SET expires_at = :e",
                "ExpressionAttributeValues": {":u": {"N": repr(float(usd))}, ":e": {"N": str(spend_expires_at(day))}},
            }
            if limit:
                update["ConditionExpression"] = "attribute_not_exists(usd) OR usd <= :max"
                update["ExpressionAttributeValues"][":max"] = {"N": repr(float(limit) - float(usd))}
            items.append({"Update": update})
        for attempt in range(attempts):
            try:
                self.ddb.transact_write_items(TransactItems=items)
                return None
            except ClientError as e:
                if _code(e) != "TransactionCanceledException":
                    raise
                codes = [str(r.get("Code", "")) for r in e.response.get("CancellationReasons") or []]
                for (_, _, label), code in zip(scopes, codes, strict=False):
                    if code == "ConditionalCheckFailed":
                        return label
                if "TransactionConflict" not in codes or attempt == attempts - 1:
                    raise  # 条件以外の取り消し. 呼び出し側で一時的な失敗としてやり直す
                # 別のワーカーの予約と同時だった. 少し待って同じ条件でやり直す（条件は足した後の額で見るので、やり直しても超えない）
                time.sleep(0.05 * (attempt + 1))
        return None

    def count_request(self, principal: str, window: int, limit: int) -> bool:
        """件数の記録（job_id = `rate#<区切り>#<利用者のハッシュ>`. owner を持たないので一覧の GSI には出ない. TTL で消える）に、
        上限未満のときだけ 1 足す（UpdateItem の条件付き ADD）."""
        try:
            self.ddb.update_item(
                TableName=self.table,
                Key={"job_id": {"S": f"rate#{rate_key(principal, window)}"}},
                UpdateExpression="ADD n :one SET expires_at = :e",
                ConditionExpression="attribute_not_exists(n) OR n < :limit",
                ExpressionAttributeValues={":one": {"N": "1"}, ":limit": {"N": str(limit)}, ":e": {"N": str(window + RATE_TTL_SECONDS)}},
            )
        except ClientError as e:
            if _code(e) == "ConditionalCheckFailedException":
                return False
            raise
        return True

    def spent(self, day: str, owner: str) -> tuple[float, float]:
        out = []
        for scope in (SPEND_ALL, spend_scope(owner)):
            item = self.ddb.get_item(TableName=self.table, Key=self._spend_key(day, scope), ConsistentRead=True).get("Item") or {}
            out.append(float(item.get("usd", {}).get("N", "0")))
        return out[0], out[1]
