"""review-platform のコマンド.

review-platform api                  API と MCP を起動
review-platform worker [--once [--wait 秒]]   ワーカーを起動（--once は 1 件だけ処理して終わる）
review-platform token                API トークンを作る（トークンと、サーバーに置く SHA-256 を表示）
review-platform check-config --role api|worker   設定を検査する（誤りなら 2）
review-platform submit FILE [--url URL] [--token T] [--title 題名] [--wait [--timeout 秒]] [--format md|json]
                                     ファイルを送って結果を表示（--url の既定 http://127.0.0.1:8080. --timeout の既定 600）

submit の終了コード: 0 成功（--wait なら未解決の MUST なし）/ 1 未解決の MUST あり /
2 送れなかった（引数・ファイル・認証・入力の誤り. --wait の --timeout が 1 未満も. 何も送らない）/
3 送れなかった、または受け付けられたか分からない（通信・サーバーの誤り. 送った後の読み取りの時間切れ・切断・5xx・API ではない応答なら
  ジョブができていることがある. 送り直す前に GET /v1/jobs で確かめる）/
4 時間切れ（--timeout までに状態を読めて、まだ終わっていなかった）/ 5 ジョブが失敗した /
6 受け付けた後に状態・結果を読めなかった（通信・5xx・429 が、最大 5 回の再試行か --timeout の残りを使い切るまで続いた.
  ジョブは動いている. 送り直さず job_id で読む）/
7 成功したが文書の一部だけをレビューした（長すぎる文書. 未解決の MUST があれば 1）
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import secrets
import sys
import time
from pathlib import Path

from . import __version__
from .config import ConfigError, Settings

log = logging.getLogger("review_platform")


def _settings(role: str) -> Settings:
    s = Settings.from_env()
    s.validate(role)
    return s


def cmd_api(_args) -> int:
    import uvicorn

    from . import logs
    from .api import create_app
    from .factory import services

    s = _settings("api")
    logs.setup(s.log_level)
    app = create_app(s, services(s))
    uvicorn.run(
        app,
        host=s.host,
        port=s.port,
        log_config=None,
        proxy_headers=True,
        forwarded_allow_ips=s.forwarded_allow_ips,
        server_header=False,
        timeout_keep_alive=s.keep_alive_seconds,
    )
    return 0


def cmd_worker(args) -> int:
    from . import logs
    from .factory import llm, notifier, storage
    from .worker import Worker

    s = _settings("worker")
    logs.setup(s.log_level)
    blobs, queue, repo = storage(s)
    w = Worker(s, blobs, queue, repo, llm(s), notifier(s))
    if args.once:
        w.run_once(wait_seconds=args.wait)
        return 0
    w.run_forever()
    return 0


def cmd_token(_args) -> int:
    token = "rp_" + secrets.token_urlsafe(32)
    print(f"トークン（利用者に渡す。どこにも保存しない）: {token}")
    print(f"RP_API_TOKENS_SHA256 に足す値（サーバー側）: {hashlib.sha256(token.encode()).hexdigest()}")
    return 0


def cmd_check(args) -> int:
    try:
        _settings(args.role)
    except ConfigError as e:
        print(f"設定の誤り: {e}", file=sys.stderr)
        return 2
    print("設定は正しく読めました")
    return 0


# submit の終了コード（README の「submit の終了コード」と同じ）
EXIT_OK = 0  # 成功（--wait なら MUST の未解決なし. --wait なしなら受け付けた）
EXIT_MUST = 1  # --wait: 結果に未解決の MUST がある
EXIT_REQUEST = 2  # 送れなかった（引数・ファイル・認証 401/403・見つからない 404・入力の誤り 400/413/415 など. 直さない限り同じ）
EXIT_SERVER = 3  # 送れなかった（接続できない・429. ジョブはできていない）か、受け付けられたか分からない（送った後の時間切れ・切断・5xx・
#                   API ではない応答. ジョブができていることがあるので、送り直す前に GET /v1/jobs で確かめる. メッセージに出す）
EXIT_TIMEOUT = 4  # --wait: --timeout の秒数までに、状態を読めて終わっていなかった（ジョブは動き続けている. job_id で後から読める）
EXIT_JOB_FAILED = 5  # --wait: ジョブが failed で終わった（理由を表示する）
EXIT_UNKNOWN = 6  # --wait: 受け付けた後、状態か結果を読めなかった（再試行しても通信・5xx・429）. ジョブは動いている. 再送すると二重のジョブ・費用になる
EXIT_PARTIAL = 7  # --wait: 成功したが文書の一部だけをレビューした（未解決の MUST があれば 1 を優先）

POLL_INTERVAL = 3.0  # 状態を聞く間隔（秒）
POLL_RETRIES = 5  # 受け付けた後の通信の一時的な失敗（接続・5xx・429）を続けて何回まで再試行するか
POLL_BACKOFF_MAX = 30.0  # 再試行の待ち時間の上限（秒. 3, 6, 12, 24, 30 と延ばす）


class _SubmitError(Exception):
    def __init__(self, code: int, message: str, maybe_sent: bool = False):
        super().__init__(message)
        self.code = code
        # 要求はサーバーに届いた後かもしれない（POST ならジョブができていることがある）
        self.maybe_sent = maybe_sent


# 要求がサーバーに届く前に失敗したと言える通信の例外. これ以外（読み取りの時間切れ・切断など）は、届いて処理された後かもしれない
def _not_sent_errors() -> tuple[type[Exception], ...]:
    import httpx

    return (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError, httpx.UnsupportedProtocol)


MAYBE_CREATED = (
    "ジョブができたかどうか分かりません（サーバーに届いた後の失敗のため）. 送り直すと二重のジョブ・二重の費用になることがあるので、"
    "先に GET /v1/jobs（自分のジョブの一覧. 新しい順）で同じファイルのジョブが無いか確かめてください"
)


def _api_error(r, what: str) -> _SubmitError:
    """API の失敗の応答を、トレースバックではなく 1 行の説明と終了コードにする."""
    try:
        body = r.json()
        detail = f"{body.get('error', '')}: {body.get('message', '')}".strip(": ") if isinstance(body, dict) else ""
    except ValueError:
        detail = ""
    detail = detail or r.text.strip()[:200]
    hint = {401: "トークンを確かめてください（--token か RP_TOKEN）", 403: "このトークンでは使えません", 404: "URL（--url）か job_id を確かめてください"}.get(r.status_code, "")
    code = EXIT_SERVER if r.status_code >= 500 or r.status_code == 429 else EXIT_REQUEST
    return _SubmitError(code, f"{what}（HTTP {r.status_code}）: {detail}" + (f". {hint}" if hint else ""), maybe_sent=r.status_code >= 500)


def _request(c, method: str, url: str, what: str, **kw):
    import httpx

    try:
        r = c.request(method, url, **kw)
    except httpx.HTTPError as e:
        if isinstance(e, _not_sent_errors()):
            raise _SubmitError(EXIT_SERVER, f"{what}: サーバーに届きませんでした（{type(e).__name__}）") from None
        raise _SubmitError(EXIT_SERVER, f"{what}: 応答を受け取れませんでした（{type(e).__name__}）", maybe_sent=True) from None
    if r.status_code >= 400:
        raise _api_error(r, what)
    return r


def _json(r, what: str) -> dict:
    try:
        data = r.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise _SubmitError(EXIT_SERVER, f"{what}: 応答が JSON ではありません（URL が review-platform の API か確かめてください）", maybe_sent=True)
    return data


def cmd_submit(args) -> int:
    try:
        return _submit(args)
    except _SubmitError as e:
        print(f"error: {e}", file=sys.stderr)
        return e.code


def _submit(args) -> int:
    import httpx

    if args.wait and args.timeout < 1:
        # 以前は 0・負の値だと、ジョブを作った直後に一度も待たずに 4（時間切れ）で終わっていた. 送る前に止める
        raise _SubmitError(EXIT_REQUEST, f"--timeout は 1 以上の秒数にしてください（{args.timeout}）. 待たないときは --wait を付けない")
    token = args.token or os.environ.get("RP_TOKEN", "")
    headers = {"authorization": f"Bearer {token}"} if token else {}
    path = Path(args.file)
    try:
        data = path.read_bytes()
    except OSError as e:
        raise _SubmitError(EXIT_REQUEST, f"ファイルを読めません: {path}（{e.strerror or type(e).__name__}）") from None
    with httpx.Client(base_url=args.url.rstrip("/"), headers=headers, timeout=60) as c:
        try:
            r = _request(c, "POST", "/v1/jobs", "送れませんでした", files={"file": (path.name, data)}, data={"title": args.title or ""})
            if r.status_code != 202:
                raise _SubmitError(EXIT_SERVER, f"送れませんでした（HTTP {r.status_code}. 202 のはず）", maybe_sent=True)
            job = _json(r, "送れませんでした")
        except _SubmitError as e:
            if e.maybe_sent:
                raise _SubmitError(e.code, f"{e}. {MAYBE_CREATED}") from None
            raise
        job_id = job.get("job_id", "")
        print(f"受け付けました: {job_id}", file=sys.stderr)
        if not args.wait:
            print(job_id)
            return EXIT_OK
        deadline = time.monotonic() + args.timeout
        while job.get("status") not in ("succeeded", "failed") and time.monotonic() < deadline:
            time.sleep(POLL_INTERVAL)
            job = _poll(c, "GET", f"/v1/jobs/{job_id}", "状態を読めませんでした", job_id, deadline, as_json=True)
        status = job.get("status", "")
        if status == "failed":
            print(f"error: ジョブが失敗しました（{job_id}）: {job.get('error', '')}", file=sys.stderr)
            return EXIT_JOB_FAILED
        if status != "succeeded":
            # ここに来るのは、状態を読めて、まだ終わっていなかったときだけ（読めずに --timeout を迎えたときは _poll が 6）
            print(f"error: {args.timeout} 秒で終わりませんでした（{job_id} は {status}. 後から GET /v1/jobs/{job_id} で読めます）", file=sys.stderr)
            return EXIT_TIMEOUT
        rep = _poll(c, "GET", f"/v1/jobs/{job_id}/report", "結果を読めませんでした", job_id, None, params={"format": args.format})
        print(rep.text)
        summary = job.get("summary") or {}
        if summary.get("open_must"):
            return EXIT_MUST
        if summary.get("partial"):
            print(f"warning: 文書が長いため、一部だけをレビューしました（{job_id}. 結果の「範囲」を見てください）", file=sys.stderr)
            return EXIT_PARTIAL
        return EXIT_OK


def _poll(c, method: str, url: str, what: str, job_id: str, deadline: float | None, as_json: bool = False, **kw):
    """受け付けた後の問い合わせ. 一時的な失敗（接続・時間切れの通信・5xx・429・JSON でない応答）は待って再試行する.

    POLL_RETRIES 回再試行しても、または deadline（--timeout）までに次の再試行を待てなくなっても読めなければ
    EXIT_UNKNOWN（ジョブは受け付け済みで動いている. 再送すると二重のジョブ・二重の費用になる. 状態が分からないので 4 ではない）.
    直さない限り同じ失敗（401・403・404 など）は、そのまま EXIT_REQUEST.
    """
    wait = POLL_INTERVAL
    for attempt in range(POLL_RETRIES + 1):
        try:
            r = _request(c, method, url, what, **kw)
            return _json(r, what) if as_json else r
        except _SubmitError as e:
            if e.code != EXIT_SERVER:
                raise _SubmitError(e.code, f"{e}（job_id: {job_id}）") from None
            last = e
        if attempt == POLL_RETRIES or (deadline is not None and time.monotonic() + wait > deadline):
            break
        print(f"warning: {last}. {wait:.0f} 秒後に再試行します（{attempt + 1}/{POLL_RETRIES}）", file=sys.stderr)
        time.sleep(wait)
        wait = min(wait * 2, POLL_BACKOFF_MAX)
    raise _SubmitError(
        EXIT_UNKNOWN,
        f"{last}. ジョブ {job_id} は受け付け済みです. 送り直さず、後から GET /v1/jobs/{job_id} で状態を読んでください",
    )


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="review-platform", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("api").set_defaults(fn=cmd_api)
    w = sub.add_parser("worker")
    w.add_argument("--once", action="store_true", help="1 件だけ処理して終わる（試験用）")
    w.add_argument("--wait", type=int, default=10, help="--once のとき、キューを待つ秒数（既定 10. --once なしでは使わない）")
    w.set_defaults(fn=cmd_worker)
    sub.add_parser("token").set_defaults(fn=cmd_token)
    c = sub.add_parser("check-config")
    c.add_argument("--role", choices=["api", "worker"], required=True)
    c.set_defaults(fn=cmd_check)
    sm = sub.add_parser("submit")
    sm.add_argument("file")
    sm.add_argument("--url", default="http://127.0.0.1:8080", help="API の URL（既定 http://127.0.0.1:8080）")
    sm.add_argument("--token", help="省略時は環境変数 RP_TOKEN")
    sm.add_argument("--title", help="ジョブの題名（結果の見出しに出る. 任意）")
    sm.add_argument("--wait", action="store_true", help="結果まで待って表示する（終了コードは上の一覧）")
    sm.add_argument("--timeout", type=int, default=600, help="--wait で待つ秒数（既定 600. 1 以上）")
    sm.add_argument("--format", choices=["md", "json"], default="md", help="--wait で表示する結果の形式（既定 md）")
    sm.set_defaults(fn=cmd_submit)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except ConfigError as e:
        print(f"設定の誤り: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
