"""review-platform submit の終了コード. 未解決の MUST・送れなかった（直す必要がある／やり直せば通るかもしれない）・時間切れ・ジョブの失敗を区別し、
401・404・JSON でない応答でもトレースバックを出さない."""

from __future__ import annotations

import httpx
import pytest

from review_platform import cli

JOB = {"job_id": "j1", "status": "queued"}


def _done(status: str, open_must: int = 0, error: str = "") -> dict:
    return {"job_id": "j1", "status": status, "error": error, "summary": {"open_must": open_must}}


@pytest.fixture
def api(monkeypatch):
    """httpx.Client を、決まった応答を返す偽の API に差し替える（通信しない）. routes は (メソッド, パス) → 応答（関数か httpx.Response）."""
    routes: dict = {}
    real = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        r = routes.get((request.method, request.url.path))
        if r is None:
            return httpx.Response(404, json={"error": "not_found", "message": "ありません"})
        return r(request) if callable(r) else r

    monkeypatch.setattr(httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr("time.sleep", lambda s: None)
    return routes


def _run(tmp_path, *extra) -> int:
    f = tmp_path / "a.md"
    f.write_text("# a", encoding="utf-8")
    return cli.main(["submit", str(f), "--url", "http://rp.invalid", "--token", "t", *extra])


@pytest.mark.parametrize(
    ("final", "want"),
    [
        (_done("succeeded"), cli.EXIT_OK),
        (_done("succeeded", open_must=2), cli.EXIT_MUST),
        (_done("failed", error="doc2md: 入力の誤り"), cli.EXIT_JOB_FAILED),
    ],
)
def test_wait_exit_codes(api, tmp_path, capsys, final, want):
    api[("POST", "/v1/jobs")] = httpx.Response(202, json=JOB)
    api[("GET", "/v1/jobs/j1")] = httpx.Response(200, json=final)
    api[("GET", "/v1/jobs/j1/report")] = httpx.Response(200, text="# レビュー結果")
    assert _run(tmp_path, "--wait") == want
    err = capsys.readouterr().err
    if want == cli.EXIT_JOB_FAILED:
        assert "doc2md: 入力の誤り" in err


def test_wait_timeout_is_distinct_from_must(api, tmp_path, capsys):
    api[("POST", "/v1/jobs")] = httpx.Response(202, json=JOB)
    api[("GET", "/v1/jobs/j1")] = httpx.Response(200, json={"job_id": "j1", "status": "running"})
    assert _run(tmp_path, "--wait", "--timeout", "0") == cli.EXIT_TIMEOUT
    assert "j1" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("status", "body", "want", "hint"),
    [
        (401, {"error": "unauthorized", "message": "トークンが違います"}, cli.EXIT_REQUEST, "--token"),
        (404, "<html>not found</html>", cli.EXIT_REQUEST, "--url"),
        (413, {"error": "too_large", "message": "大きすぎます"}, cli.EXIT_REQUEST, "大きすぎます"),
        (503, {"error": "unavailable", "message": "混んでいます"}, cli.EXIT_SERVER, "混んでいます"),
        (429, {"error": "rate_limited", "message": "多すぎます"}, cli.EXIT_SERVER, "多すぎます"),
    ],
)
def test_submit_errors_have_exit_code_and_no_traceback(api, tmp_path, capsys, status, body, want, hint):
    api[("POST", "/v1/jobs")] = httpx.Response(status, json=body) if isinstance(body, dict) else httpx.Response(status, text=body)
    assert _run(tmp_path, "--wait") == want
    err = capsys.readouterr().err
    assert f"HTTP {status}" in err and hint in err and "Traceback" not in err


def test_job_lookup_404_during_wait_is_not_a_traceback(api, tmp_path, capsys):
    """以前は待っている間の GET が 404（ジョブが消えた・URL の誤り）だと KeyError のトレースバックで落ちた."""
    api[("POST", "/v1/jobs")] = httpx.Response(202, json=JOB)
    assert _run(tmp_path, "--wait") == cli.EXIT_REQUEST
    assert "状態を読めませんでした（HTTP 404）" in capsys.readouterr().err


def test_non_json_success_response_is_reported(api, tmp_path, capsys):
    """review-platform ではない URL（プロキシのログイン画面など）が 202 を返しても、JSON の読み取りで落ちない."""
    api[("POST", "/v1/jobs")] = httpx.Response(202, text="<html>login</html>")
    assert _run(tmp_path) == cli.EXIT_SERVER
    assert "JSON ではありません" in capsys.readouterr().err


def test_unreachable_server_and_missing_file(monkeypatch, tmp_path, capsys):
    real = httpx.Client

    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    monkeypatch.setattr(httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(refuse), **kw))
    assert _run(tmp_path) == cli.EXIT_SERVER
    assert "サーバーに届きませんでした（ConnectError）" in capsys.readouterr().err
    assert cli.main(["submit", str(tmp_path / "無い.md"), "--url", "http://rp.invalid"]) == cli.EXIT_REQUEST
    assert "ファイルを読めません" in capsys.readouterr().err


def test_readme_documents_submit_exit_codes():
    from pathlib import Path

    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
    section = readme.split("### submit の終了コード", 1)[1].split("\n#", 1)[0]
    for code in (cli.EXIT_OK, cli.EXIT_MUST, cli.EXIT_REQUEST, cli.EXIT_SERVER, cli.EXIT_TIMEOUT, cli.EXIT_JOB_FAILED):
        assert f"| `{code}` |" in section, code
