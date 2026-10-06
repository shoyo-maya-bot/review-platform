from __future__ import annotations

import email.message
import hashlib
import io
import json
import logging
import urllib.error
import urllib.request

import httpx
import pytest

from conftest import TOKEN
from review_platform import cli, logs
from review_platform.adapters.llm import AnthropicLLM, LLMError, price_for
from review_platform.adapters.notify import SlackNotifier
from review_platform.domain import Job, JobKind, JobStatus


def test_cli_token(capsys):
    assert cli.main(["token"]) == 0
    out = capsys.readouterr().out.splitlines()
    token = out[0].split(": ", 1)[1]
    assert token.startswith("rp_") and out[1].endswith(hashlib.sha256(token.encode()).hexdigest())


def test_cli_check_config(monkeypatch, capsys):
    monkeypatch.setenv("RP_API_TOKENS_SHA256", "x")
    assert cli.main(["check-config", "--role", "api"]) == 2
    monkeypatch.setenv("RP_API_TOKENS_SHA256", hashlib.sha256(b"t").hexdigest())
    assert cli.main(["check-config", "--role", "api"]) == 0
    monkeypatch.setenv("RP_BACKEND", "nope")
    assert cli.main(["check-config", "--role", "worker"]) == 2


def test_cli_submit_and_worker_once(server, settings, monkeypatch, tmp_path, capsys):
    """CLI で送り、CLI のワーカー（--once）で処理し、CLI で結果を受け取る."""
    doc = tmp_path / "設計.md"
    doc.write_text("# 設計\n本文", encoding="utf-8")
    assert cli.main(["submit", str(doc), "--url", server, "--token", TOKEN]) == 0
    job_id = capsys.readouterr().out.strip()
    for k, v in {
        "RP_DATA_DIR": str(settings.data_dir),
        "RP_LLM": "fake",
        "RP_API_TOKENS_SHA256": settings.api_token_hashes[0],
        "RP_LOG_LEVEL": "warning",
    }.items():
        monkeypatch.setenv(k, v)
    assert cli.main(["worker", "--once", "--wait", "1"]) == 0
    r = httpx.get(f"{server}/v1/jobs/{job_id}", headers={"authorization": f"Bearer {TOKEN}"}).json()
    assert r["status"] == "succeeded"
    doc2 = tmp_path / "b.md"
    doc2.write_text("# b", encoding="utf-8")
    monkeypatch.setattr("time.sleep", lambda s: None)
    # --wait は結果まで待つ. ワーカーが居ないので終わらず、タイムアウトで 1
    assert cli.main(["submit", str(doc2), "--url", server, "--token", TOKEN, "--wait", "--timeout", "0"]) == 1
    assert cli.main(["submit", str(doc2), "--url", server, "--token", "wrong"]) == 1


class _Server:
    """LLMClient の opener を差し替える偽の窓口（通信しない）. 応答は (status, body, headers) を順に返す."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[urllib.request.Request] = []
        self.sleeps: list[float] = []

    def open(self, req: urllib.request.Request, timeout: float):
        self.requests.append(req)
        status, body, headers = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        raw = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        if status >= 400:
            msg = email.message.Message()
            for k, v in headers.items():
                msg[k] = v
            raise urllib.error.HTTPError(req.full_url, status, "error", msg, io.BytesIO(raw))
        return _Resp(status, headers, raw)

    def body(self, i: int) -> dict:
        return json.loads(self.requests[i].data)


class _Resp:
    def __init__(self, status, headers, raw):
        self.status, self.headers, self._raw = status, headers, raw

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None


def _anthropic(server: _Server, **kw) -> AnthropicLLM:
    return AnthropicLLM("sk-test", "https://llm.example.com", opener=server.open, sleep=server.sleeps.append, **kw)


def _ok(text="ok", stop="end_turn", **usage):
    body = {"model": "claude-sonnet-5-5", "content": [{"type": "text", "text": text}], "usage": {"input_tokens": 1000, "output_tokens": 100, **usage}, "stop_reason": stop}
    return (200, body, {"request-id": "req_ok"})


def _err(status, retry_after=None, text="overloaded"):
    return (status, text, {"retry-after": retry_after} if retry_after else {})


def test_anthropic_success_and_retry():
    server = _Server(_err(529, retry_after="3"), _ok())
    r = _anthropic(server).complete("claude-sonnet-5-5", "sys", "user", 100)
    assert r.text == "ok" and len(server.requests) == 2 and r.request_id == "req_ok"
    req, body = server.requests[1], server.body(1)
    assert req.get_header("X-api-key") == "sk-test" and req.full_url == "https://llm.example.com/v1/messages" and body["system"] == "sys"
    assert body["messages"] == [{"role": "user", "content": "user"}] and body["max_tokens"] == 100
    assert len(server.sleeps) == 1 and 3.0 <= server.sleeps[0] <= 3.5  # retry-after に従って待つ
    assert r.cost_usd == pytest.approx((1000 * 2 + 100 * 10) / 1e6)


@pytest.mark.parametrize(("resp", "retryable"), [(_err(400, text="bad sk-ant-abcdef123456"), False), (_ok(stop="max_tokens"), False), (_err(401, text="unauthorized"), False)])
def test_anthropic_errors(resp, retryable):
    server = _Server(resp)
    with pytest.raises(LLMError) as e:
        _anthropic(server).complete("m", "s", "u", 1)
    assert e.value.retryable is retryable and "abcdef123456" not in str(e.value)
    assert len(server.requests) == 1  # 再試行しない


def test_anthropic_max_tokens_is_not_zero_findings():
    with pytest.raises(LLMError, match="max_tokens") as e:
        _anthropic(_Server(_ok('{"findings": [', stop="max_tokens"))).complete("claude-sonnet-5-5", "s", "u", 10)
    assert e.value.retryable is False


def test_anthropic_gives_up():
    server = _Server(_err(503))
    with pytest.raises(LLMError) as e:
        _anthropic(server).complete("m", "s", "u", 1)
    assert e.value.retryable and len(server.requests) == 4  # 1 回 + 再試行 3 回


def test_anthropic_connection_error_is_retryable():
    def refuse(req, timeout):
        raise urllib.error.URLError("connection refused")

    llm = AnthropicLLM("sk-test", "https://llm.example.com", opener=refuse, sleep=lambda s: None, max_retries=1)
    with pytest.raises(LLMError) as e:
        llm.complete("m", "s", "u", 1)
    assert e.value.retryable


def test_anthropic_without_key_is_not_retryable(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    with pytest.raises(LLMError) as e:
        AnthropicLLM("", "https://llm.example.com", opener=_Server(_ok()).open).complete("m", "s", "u", 1)
    assert not e.value.retryable


def test_anthropic_logs_usage_with_request_id(caplog):
    caplog.set_level(logging.INFO)
    _anthropic(_Server(_ok(cache_read_input_tokens=5000))).complete("claude-sonnet-5-5", "秘密の本文", "u", 100)
    recs = [r for r in caplog.records if getattr(r, "llm", None)]
    assert len(recs) == 1
    rec = recs[0].llm
    assert rec["request_id"] == "req_ok" and rec["model"] == "claude-sonnet-5-5" and rec["input_tokens"] == 1000 and rec["cache_read_input_tokens"] == 5000
    assert "秘密の本文" not in caplog.text  # 本文はログに出さない


def test_anthropic_cost_counts_cache_tokens_with_shared_prices():
    from devtools_common.llm import price_for as shared

    r = _anthropic(_Server(_ok(cache_read_input_tokens=10_000, cache_creation_input_tokens=2_000))).complete("claude-sonnet-5-5", "s", "u", 100)
    p = shared("claude-sonnet-5-5")
    assert r.input_tokens == 13_000
    assert r.cost_usd == pytest.approx((1000 * p.input + 100 * p.output + 2000 * p.cache_write + 10_000 * p.cache_read) / 1e6)


def test_anthropic_masks_pii_before_sending():
    from review_platform.adapters.llm import Masker

    server = _Server(_ok())
    _anthropic(server, masker=Masker()).complete("claude-sonnet-5-5", "連絡先 ops@example.co.jp", "<document>担当 090-1234-5678 / taro@example.com</document>", 100)
    sent = server.requests[0].data.decode()
    assert "ops@example.co.jp" not in sent and "090-1234-5678" not in sent and "taro@example.com" not in sent
    assert "＊＊＊" in json.loads(sent)["messages"][0]["content"]


def test_anthropic_reuses_one_client_per_model():
    llm = AnthropicLLM("sk-test", "https://llm.example.com/")
    assert llm.client("a") is llm.client("a") and llm.client("a") is not llm.client("b")
    assert llm.client("a").base_url == "https://llm.example.com" and llm.client("a").max_retries == 3


def test_price_lookup():
    assert price_for("apac.anthropic.claude-haiku-4-5") == (1.0, 5.0) and price_for("unknown-model") == (4.0, 20.0)
    # 共通の料金表（devtools_common.llm.PRICES）で引く. Bedrock の ID・推論プロファイルの ARN・日付付きの ID も同じ単価
    assert price_for("anthropic.claude-sonnet-4-5-20250929-v1:0") == (3.0, 15.0)
    assert price_for("arn:aws:bedrock:ap-northeast-1:123456789012:inference-profile/apac.anthropic.claude-sonnet-4-5-20250929-v1:0") == (3.0, 15.0)
    assert price_for("claude-haiku-4-5-20251001") == (1.0, 5.0) and price_for("claude-opus-4-1") == (15.0, 75.0)
    assert price_for("claude-opus-4-9") == (4.0, 20.0)  # 表に無い新しい版は高い側で見積もる（古い版の単価に当てない）


def test_cost_for_unknown_model_counts_cache_as_input():
    from review_platform.adapters.llm import cost

    assert cost("unknown-model", 1_000_000, 0, 500_000, 500_000) == pytest.approx(8.0)


def test_slack_notify_sanitizes_and_never_raises():
    sent = []
    n = SlackNotifier("https://hooks.example.com/x", client=httpx.Client(transport=httpx.MockTransport(lambda r: sent.append(json.loads(r.content)) or httpx.Response(200))))
    job = Job.create("a", JobKind.DOCUMENT, "a.md", 1, "<!channel> @here 設計")
    job.status = JobStatus.SUCCEEDED
    job.summary = {"severity": {"MUST": 1, "WANT": 2}}
    n.notify(job, "https://rp.example.com/v1/jobs/x/report")
    text = sent[0]["text"]
    assert "<!channel>" not in text and "@here" not in text and "MUST 1" in text and text.endswith("/report")
    bad = SlackNotifier("https://hooks.example.com/x", client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    bad.notify(job, "")  # 例外にしない


def test_json_logs(capsys):
    logs.setup("INFO")
    logging.getLogger("t").info("hello", extra={"job_id": "j1"})
    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger("t").exception("failed")
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert lines[0]["message"] == "hello" and lines[0]["job_id"] == "j1" and lines[0]["level"] == "INFO"
    assert "ValueError" in lines[1]["exception"]


def test_factory_selection():
    from dataclasses import replace

    from review_platform.config import Settings
    from review_platform.factory import llm, notifier

    s = Settings()
    assert llm(s).name == "none" and llm(replace(s, llm="fake")).name == "fake"
    assert llm(replace(s, llm="anthropic", anthropic_api_key="k")).name == "anthropic"
    assert type(notifier(s)).__name__ == "NoNotifier"
    assert type(notifier(replace(s, notify="slack", slack_webhook_url="https://x"))).__name__ == "SlackNotifier"
