"""第 8 回の指摘: submit の POST の失敗と --timeout の扱いを、README の終了コードの表と合わせる.

- POST の後の読み取りの時間切れ・切断・5xx・API ではない応答は、サーバーがジョブを作った後のことがある（冪等キーは無い）.
  以前は「ジョブはできていない（やり直してよい）」とだけ書いていたので、スクリプトが送り直すと二重のジョブ・二重の費用になりえた.
  終了コードは 3 のまま、送り直す前に GET /v1/jobs で確かめるよう表示する. 接続できない・429 は従来どおり（ジョブはできていない）.
- 状態を聞いている間に --timeout の残りで再試行を待てなくなったときは 6（状態不明）. README も 6 と書く.
- --wait の --timeout が 0・負だと、ジョブを作った直後に一度も待たずに 4 で終わっていた. 送る前に 2 で止める.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from review_platform import cli

JOB = {"job_id": "j1", "status": "queued"}
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def api(monkeypatch):
    routes: dict = {}
    calls: list[tuple[str, str]] = []
    real = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        r = routes.get((request.method, request.url.path))
        if r is None:
            return httpx.Response(404, json={"error": "not_found", "message": "ありません"})
        if isinstance(r, list):
            r = r.pop(0) if len(r) > 1 else r[0]
        if callable(r):
            r = r(request)
        return r

    monkeypatch.setattr(httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    clock = [0.0]
    monkeypatch.setattr(cli, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s)))
    routes["calls"] = calls
    routes["clock"] = clock
    return routes


def _run(tmp_path, *extra) -> int:
    f = tmp_path / "a.md"
    f.write_text("# a", encoding="utf-8")
    return cli.main(["submit", str(f), "--url", "http://rp.invalid", "--token", "t", *extra])


def _raise(exc_type):
    def f(request):
        raise exc_type("x", request=request)

    return f


@pytest.mark.parametrize(
    "response",
    [
        _raise(httpx.ReadTimeout),
        _raise(httpx.RemoteProtocolError),
        httpx.Response(502, text="bad gateway"),
        httpx.Response(500, json={"error": "internal", "message": "oops"}),
        httpx.Response(200, json=JOB),  # 202 以外の成功
        httpx.Response(202, text="<html>proxy</html>"),  # API ではない応答
    ],
)
@pytest.mark.parametrize("wait", [[], ["--wait"]])
def test_post_failure_after_sending_warns_that_the_job_may_exist(api, tmp_path, capsys, response, wait):
    api[("POST", "/v1/jobs")] = response
    assert _run(tmp_path, *wait) == cli.EXIT_SERVER
    err = capsys.readouterr().err
    assert "ジョブができたかどうか分かりません" in err and "GET /v1/jobs" in err and "Traceback" not in err


@pytest.mark.parametrize(
    "response",
    [_raise(httpx.ConnectError), _raise(httpx.ConnectTimeout), httpx.Response(429, json={"error": "rate_limited", "message": "多すぎます"})],
)
def test_post_failure_before_the_job_exists_does_not_warn(api, tmp_path, capsys, response):
    api[("POST", "/v1/jobs")] = response
    assert _run(tmp_path, "--wait") == cli.EXIT_SERVER
    assert "ジョブができたかどうか分かりません" not in capsys.readouterr().err


@pytest.mark.parametrize("timeout", ["0", "-5"])
def test_wait_timeout_below_one_is_rejected_before_sending(api, tmp_path, capsys, timeout):
    api[("POST", "/v1/jobs")] = httpx.Response(202, json=JOB)
    assert _run(tmp_path, "--wait", "--timeout", timeout) == cli.EXIT_REQUEST
    assert "--timeout は 1 以上" in capsys.readouterr().err
    assert api["calls"] == []  # ジョブを作らない


def test_timeout_without_wait_is_ignored_as_before(api, tmp_path, capsys):
    api[("POST", "/v1/jobs")] = httpx.Response(202, json=JOB)
    assert _run(tmp_path, "--timeout", "0") == cli.EXIT_OK
    assert capsys.readouterr().out.strip() == "j1"


def test_poll_retry_cut_short_by_timeout_is_unknown_not_timeout(api, tmp_path, capsys):
    """状態を読めないまま --timeout の残りで再試行を待てなくなったら 6（状態が分からない. 4 は「読めて、まだ終わっていない」）."""
    api[("POST", "/v1/jobs")] = httpx.Response(202, json=JOB)
    api[("GET", "/v1/jobs/j1")] = [httpx.Response(503, json={"error": "unavailable", "message": "混んでいます"})]
    assert _run(tmp_path, "--wait", "--timeout", "10") == cli.EXIT_UNKNOWN
    err = capsys.readouterr().err
    assert "送り直さず" in err and "j1" in err
    assert sum(1 for c in api["calls"] if c == ("GET", "/v1/jobs/j1")) < cli.POLL_RETRIES + 1  # 5 回待たずに止めた


def test_readme_exit_table_describes_these_cases():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    rows = {m[0]: m[1] for m in re.findall(r"^\|\s*`(\d)`\s*\|(.*)$", text, re.M)}
    assert "ジョブができたかどうか分かりません" in rows["3"] and "GET /v1/jobs" in rows["3"] and "冪等キーは無い" in rows["3"]
    assert "`--timeout` の残り" in rows["6"]
    assert "1 未満" in rows["2"]
    help_text = cli.__doc__ or ""
    assert "--timeout が 1 未満" in help_text and "--timeout の残り" in help_text
