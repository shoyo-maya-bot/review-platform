"""AWS の実装を moto（AWS のふりをするライブラリ）で試す. CI では必ず流す."""

from __future__ import annotations

import os

import pytest

boto3 = pytest.importorskip("boto3")
moto = pytest.importorskip("moto")

from contract import blob_contract, queue_contract, repo_contract, spend_contract  # noqa: E402
from review_platform.adapters.aws import OWNER_INDEX, DynamoJobRepository, S3BlobStore, SqsQueue  # noqa: E402

pytestmark = pytest.mark.aws
REGION = "ap-northeast-1"


@pytest.fixture(autouse=True)
def aws(monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_DEFAULT_REGION": REGION}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with moto.mock_aws():
        yield


def test_s3():
    boto3.client("s3", region_name=REGION).create_bucket(Bucket="rp-test", CreateBucketConfiguration={"LocationConstraint": REGION})
    blob_contract(S3BlobStore("rp-test", REGION))


def test_sqs():
    url = boto3.client("sqs", region_name=REGION).create_queue(QueueName="rp-jobs")["QueueUrl"]
    q = SqsQueue(url, REGION)
    queue_contract(q)
    q.send("20260101000000000000-0123456789abcdef")
    m = q.receive(1, 5, 30)[0]
    q.dead_letter(m)  # すぐ見えるようにして受信回数を進める（DLQ への移動は SQS の redrive が行う）
    assert q.receive(1, 5, 30)[0].receive_count == 2


def test_dynamodb():
    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName="rp-jobs",
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "job_id", "AttributeType": "S"}, {"AttributeName": "owner", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
        GlobalSecondaryIndexes=[
            {
                "IndexName": OWNER_INDEX,
                "KeySchema": [{"AttributeName": "owner", "KeyType": "HASH"}, {"AttributeName": "job_id", "KeyType": "RANGE"}],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    )
    repo = DynamoJobRepository("rp-jobs", REGION)
    repo_contract(repo)
    spend_contract(repo)


def test_bedrock_request_shape(monkeypatch):
    """Bedrock の呼び出しの形（Messages の形・anthropic_version）と、出力が切れたときの扱い."""
    import io
    import json

    from review_platform.adapters.llm import BedrockLLM, LLMError

    llm = BedrockLLM(REGION)
    sent = {}

    def fake_invoke(**kw):
        sent.update(kw)
        body = {"content": [{"type": "text", "text": "ok"}], "usage": {"input_tokens": 10, "output_tokens": 2}, "stop_reason": sent.get("_stop", "end_turn")}
        return {"body": io.BytesIO(json.dumps(body).encode())}

    monkeypatch.setattr(llm.client, "invoke_model", fake_invoke)
    r = llm.complete("apac.anthropic.claude-sonnet", "sys", "user", 100)
    req = json.loads(sent["body"])
    assert req["anthropic_version"] == "bedrock-2023-05-31" and req["messages"][0]["content"] == "user" and r.text == "ok"

    def cut(**kw):
        body = {"content": [{"type": "text", "text": "…"}], "usage": {}, "stop_reason": "max_tokens"}
        return {"body": io.BytesIO(json.dumps(body).encode())}

    monkeypatch.setattr(llm.client, "invoke_model", cut)
    with pytest.raises(LLMError):
        llm.complete("m", "s", "u", 1)


def test_bedrock_structured_output(monkeypatch):
    """schema を渡すと tools・tool_choice を付け、道具の入力を JSON の文字列で返す."""
    import io
    import json

    from auto_review.findings import FINDINGS_SCHEMA

    from review_platform.adapters.llm import BedrockLLM

    llm = BedrockLLM(REGION)
    sent = {}

    def fake_invoke(**kw):
        sent.update(kw)
        content = [{"type": "tool_use", "id": "t", "name": "submit_review", "input": {"findings": []}}]
        body = {"content": content, "usage": {"input_tokens": 1, "output_tokens": 1}, "stop_reason": "tool_use"}
        return {"body": io.BytesIO(json.dumps(body).encode())}

    monkeypatch.setattr(llm.client, "invoke_model", fake_invoke)
    r = llm.complete("m", "s", "u", 10, schema=FINDINGS_SCHEMA)
    req = json.loads(sent["body"])
    assert req["tool_choice"]["name"] == "submit_review" and req["tools"][0]["input_schema"] == FINDINGS_SCHEMA
    assert json.loads(r.text) == {"findings": []}


def test_bedrock_masks_pii_logs_request_id_and_uses_shared_prices(monkeypatch, caplog):
    import io
    import json
    import logging

    from devtools_common.llm import price_for as shared

    from review_platform.adapters.llm import BedrockLLM, Masker

    llm = BedrockLLM(REGION, masker=Masker())
    sent = {}

    def fake_invoke(**kw):
        sent.update(kw)
        body = {"content": [{"type": "text", "text": "ok"}], "usage": {"input_tokens": 1000, "output_tokens": 100}, "stop_reason": "end_turn"}
        return {"body": io.BytesIO(json.dumps(body).encode()), "ResponseMetadata": {"RequestId": "bedrock-req-1"}}

    monkeypatch.setattr(llm.client, "invoke_model", fake_invoke)
    caplog.set_level(logging.INFO)
    model = "apac.anthropic.claude-sonnet-4-5-20250929-v1:0"
    r = llm.complete(model, "sys a@example.com", "担当 090-1234-5678", 100)
    req = json.loads(sent["body"])
    assert "a@example.com" not in req["system"] and "090-1234-5678" not in req["messages"][0]["content"]
    p = shared("claude-sonnet-4-5")
    assert r.request_id == "bedrock-req-1" and r.cost_usd == pytest.approx((1000 * p.input + 100 * p.output) / 1e6)
    rec = next(x.llm for x in caplog.records if getattr(x, "llm", None))
    assert rec["request_id"] == "bedrock-req-1" and rec["model"] == model


def test_factory_builds_aws(monkeypatch):
    from dataclasses import replace

    from review_platform.config import Settings
    from review_platform.factory import storage

    s = replace(Settings(), backend="aws", s3_bucket="b", sqs_queue_url="https://sqs.example/q", ddb_table="t", aws_region=REGION)
    blobs, queue, repo = storage(s)
    assert type(blobs).__name__ == "S3BlobStore" and type(queue).__name__ == "SqsQueue" and type(repo).__name__ == "DynamoJobRepository"
    assert os.environ["AWS_DEFAULT_REGION"] == REGION
