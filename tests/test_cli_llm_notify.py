from __future__ import annotations

import hashlib
import json
import logging

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


def _anthropic(handler) -> AnthropicLLM:
    return AnthropicLLM("sk-test", "https://llm.example.com", client=httpx.Client(transport=httpx.MockTransport(handler)))


def _ok(text="ok", stop="end_turn"):
    return httpx.Response(200, json={"content": [{"type": "text", "text": text}], "usage": {"input_tokens": 1000, "output_tokens": 100}, "stop_reason": stop})


def test_anthropic_success_and_retry(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    calls = []

    def handler(req: httpx.Request):
        calls.append(req)
        if len(calls) == 1:
            return httpx.Response(529, text="overloaded")
        return _ok()

    r = _anthropic(handler).complete("claude-sonnet-5-5", "sys", "user", 100)
    assert r.text == "ok" and len(calls) == 2
    body = json.loads(calls[1].content)
    assert calls[1].headers["x-api-key"] == "sk-test" and calls[1].url.path == "/v1/messages" and body["system"] == "sys"
    assert r.cost_usd == pytest.approx((1000 * 2 + 100 * 10) / 1e6)


@pytest.mark.parametrize(("resp", "retryable"), [(httpx.Response(400, text="bad sk-ant-abcdef123456"), False), (_ok(stop="max_tokens"), False)])
def test_anthropic_errors(monkeypatch, resp, retryable):
    monkeypatch.setattr("time.sleep", lambda s: None)
    with pytest.raises(LLMError) as e:
        _anthropic(lambda req: resp).complete("m", "s", "u", 1)
    assert e.value.retryable is retryable and "abcdef123456" not in str(e.value)


def test_anthropic_gives_up(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    with pytest.raises(LLMError) as e:
        _anthropic(lambda req: httpx.Response(503)).complete("m", "s", "u", 1)
    assert e.value.retryable


def test_price_lookup():
    assert price_for("apac.anthropic.claude-haiku-4-5") == (1.0, 5.0) and price_for("unknown-model") == (4.0, 20.0)


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
