"""submit --wait の待ち方. 受け付けた後の問い合わせの一時的な失敗は再試行し、それでも駄目なら「状態不明・再送するな」（6）.
一部だけをレビューした成功は 7 で区別する."""

from __future__ import annotations

import httpx
import pytest

from review_platform import cli

JOB = {"job_id": "j1", "status": "queued"}
DONE = {"job_id": "j1", "status": "succeeded", "summary": {"open_must": 0}}


@pytest.fixture
def api(monkeypatch):
    routes: dict = {}
    real = httpx.Client
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        r = routes.get((request.method, request.url.path))
        if r is None:
            return httpx.Response(404, json={"error": "not_found", "message": "ありません"})
        if isinstance(r, list):
            r = r.pop(0) if len(r) > 1 else r[0]
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr("time.sleep", sleeps.append)
    routes["sleeps"] = sleeps
    return routes


def _run(tmp_path, *extra) -> int:
    f = tmp_path / "a.md"
    f.write_text("# a", encoding="utf-8")
    return cli.main(["submit", str(f), "--url", "http://rp.invalid", "--token", "t", "--wait", *extra])


def _conn_error():
    return httpx.ConnectError("reset")


def test_transient_poll_errors_are_retried(api, tmp_path, capsys):
    api[("POST", "/v1/jobs")] = httpx.Response(202, json=JOB)
    api[("GET", "/v1/jobs/j1")] = [
        httpx.Response(503, json={"error": "unavailable", "message": "混んでいます"}),
        _conn_error(),
        httpx.Response(429, json={"error": "rate_limited", "message": "多すぎます"}),
        httpx.Response(200, json=DONE),
    ]
    api[("GET", "/v1/jobs/j1/report")] = httpx.Response(200, text="# レビュー結果")
    assert _run(tmp_path) == cli.EXIT_OK
    assert "再試行します" in capsys.readouterr().err
    # 間隔を延ばして待つ
    backoff = [s for s in api["sleeps"] if s != cli.POLL_INTERVAL]
    assert backoff == sorted(backoff) and len(backoff) >= 2


@pytest.mark.parametrize("failure", [httpx.Response(502, text="bad gateway"), "conn", httpx.Response(200, text="<html>proxy</html>")])
def test_persistent_poll_errors_give_unknown_state_with_job_id(api, tmp_path, capsys, failure):
    """以前は 1 回の失敗で 3（やり直せば通る）になり、スクリプトが送り直して二重のジョブ・二重の費用になった."""
    api[("POST", "/v1/jobs")] = httpx.Response(202, json=JOB)
    api[("GET", "/v1/jobs/j1")] = [_conn_error() if failure == "conn" else failure]
    assert _run(tmp_path) == cli.EXIT_UNKNOWN
    err = capsys.readouterr().err
    assert "j1" in err and "送り直さず" in err and "Traceback" not in err
    assert cli.EXIT_UNKNOWN not in (cli.EXIT_SERVER, cli.EXIT_REQUEST)


def test_report_fetch_failure_after_success_is_unknown_state(api, tmp_path, capsys):
    api[("POST", "/v1/jobs")] = httpx.Response(202, json=DONE)
    api[("GET", "/v1/jobs/j1/report")] = [httpx.Response(500, text="oops")]
    assert _run(tmp_path) == cli.EXIT_UNKNOWN
    assert "j1" in capsys.readouterr().err


def test_permanent_poll_error_is_not_retried(api, tmp_path, capsys):
    api[("POST", "/v1/jobs")] = httpx.Response(202, json=JOB)
    api[("GET", "/v1/jobs/j1")] = [httpx.Response(401, json={"error": "unauthorized", "message": "期限切れ"})]
    assert _run(tmp_path) == cli.EXIT_REQUEST
    err = capsys.readouterr().err
    assert "再試行します" not in err and "j1" in err


def test_initial_submit_failure_is_still_3(api, tmp_path):
    """POST の失敗は 3 のまま（5xx はジョブができていることがあるので、送り直す前に確かめるよう表示する. test_submit_post_ambiguity.py）."""
    api[("POST", "/v1/jobs")] = [httpx.Response(503, json={"error": "unavailable", "message": "混んでいます"})]
    assert _run(tmp_path) == cli.EXIT_SERVER


@pytest.mark.parametrize(
    ("summary", "want"),
    [
        ({"open_must": 0, "partial": True}, cli.EXIT_PARTIAL),
        ({"open_must": 1, "partial": True}, cli.EXIT_MUST),
        ({"open_must": 0, "partial": False}, cli.EXIT_OK),
        ({"open_must": 0}, cli.EXIT_OK),  # 古いサーバー（partial が無い）
    ],
)
def test_partial_review_has_its_own_exit_code(api, tmp_path, capsys, summary, want):
    api[("POST", "/v1/jobs")] = httpx.Response(202, json=JOB)
    api[("GET", "/v1/jobs/j1")] = httpx.Response(200, json={"job_id": "j1", "status": "succeeded", "summary": summary})
    api[("GET", "/v1/jobs/j1/report")] = httpx.Response(200, text="# レビュー結果")
    assert _run(tmp_path) == want
    if want == cli.EXIT_PARTIAL:
        assert "一部だけ" in capsys.readouterr().err


def test_exit_codes_are_distinct():
    codes = [cli.EXIT_OK, cli.EXIT_MUST, cli.EXIT_REQUEST, cli.EXIT_SERVER, cli.EXIT_TIMEOUT, cli.EXIT_JOB_FAILED, cli.EXIT_UNKNOWN, cli.EXIT_PARTIAL]
    assert len(set(codes)) == len(codes) and (cli.EXIT_UNKNOWN, cli.EXIT_PARTIAL) == (6, 7)
