"""review-platform のコマンド.

review-platform api                  API と MCP を起動
review-platform worker [--once]      ワーカーを起動
review-platform token                API トークンを作る（トークンと、サーバーに置く SHA-256 を表示）
review-platform check-config --role api|worker
review-platform submit FILE --url URL [--token T] [--wait]   ファイルを送って結果を表示

submit の終了コード: 0 成功（--wait なら未解決の MUST なし）/ 1 未解決の MUST あり / 2 送れなかった（引数・ファイル・認証・入力の誤り）/
3 送れなかった・状態を読めなかった（通信・サーバーの誤り）/ 4 時間切れ（--timeout）/ 5 ジョブが失敗した
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
EXIT_SERVER = 3  # 送れなかった・状態を読めなかった（接続できない・時間切れの通信・5xx・429. 待ってからやり直すと通ることがある）
EXIT_TIMEOUT = 4  # --wait: --timeout の秒数までに終わらなかった（ジョブは動き続けている. job_id で後から読める）
EXIT_JOB_FAILED = 5  # --wait: ジョブが failed で終わった（理由を表示する）


class _SubmitError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


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
    return _SubmitError(code, f"{what}（HTTP {r.status_code}）: {detail}" + (f". {hint}" if hint else ""))


def _request(c, method: str, url: str, what: str, **kw):
    import httpx

    try:
        r = c.request(method, url, **kw)
    except httpx.HTTPError as e:
        raise _SubmitError(EXIT_SERVER, f"{what}: サーバーに届きませんでした（{type(e).__name__}）") from None
    if r.status_code >= 400:
        raise _api_error(r, what)
    return r


def _json(r, what: str) -> dict:
    try:
        data = r.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise _SubmitError(EXIT_SERVER, f"{what}: 応答が JSON ではありません（URL が review-platform の API か確かめてください）")
    return data


def cmd_submit(args) -> int:
    try:
        return _submit(args)
    except _SubmitError as e:
        print(f"error: {e}", file=sys.stderr)
        return e.code


def _submit(args) -> int:
    import httpx

    token = args.token or os.environ.get("RP_TOKEN", "")
    headers = {"authorization": f"Bearer {token}"} if token else {}
    path = Path(args.file)
    try:
        data = path.read_bytes()
    except OSError as e:
        raise _SubmitError(EXIT_REQUEST, f"ファイルを読めません: {path}（{e.strerror or type(e).__name__}）") from None
    with httpx.Client(base_url=args.url.rstrip("/"), headers=headers, timeout=60) as c:
        r = _request(c, "POST", "/v1/jobs", "送れませんでした", files={"file": (path.name, data)}, data={"title": args.title or ""})
        if r.status_code != 202:
            raise _SubmitError(EXIT_SERVER, f"送れませんでした（HTTP {r.status_code}. 202 のはず）")
        job = _json(r, "送れませんでした")
        job_id = job.get("job_id", "")
        print(f"受け付けました: {job_id}", file=sys.stderr)
        if not args.wait:
            print(job_id)
            return EXIT_OK
        deadline = time.monotonic() + args.timeout
        while job.get("status") not in ("succeeded", "failed") and time.monotonic() < deadline:
            time.sleep(3)
            job = _json(_request(c, "GET", f"/v1/jobs/{job_id}", "状態を読めませんでした"), "状態を読めませんでした")
        status = job.get("status", "")
        if status == "failed":
            print(f"error: ジョブが失敗しました（{job_id}）: {job.get('error', '')}", file=sys.stderr)
            return EXIT_JOB_FAILED
        if status != "succeeded":
            print(f"error: {args.timeout} 秒で終わりませんでした（{job_id} は {status}. 後から GET /v1/jobs/{job_id} で読めます）", file=sys.stderr)
            return EXIT_TIMEOUT
        rep = _request(c, "GET", f"/v1/jobs/{job_id}/report", "結果を読めませんでした", params={"format": args.format})
        print(rep.text)
        return EXIT_MUST if (job.get("summary") or {}).get("open_must") else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="review-platform", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("api").set_defaults(fn=cmd_api)
    w = sub.add_parser("worker")
    w.add_argument("--once", action="store_true", help="1 件だけ処理して終わる（試験用）")
    w.add_argument("--wait", type=int, default=10)
    w.set_defaults(fn=cmd_worker)
    sub.add_parser("token").set_defaults(fn=cmd_token)
    c = sub.add_parser("check-config")
    c.add_argument("--role", choices=["api", "worker"], required=True)
    c.set_defaults(fn=cmd_check)
    sm = sub.add_parser("submit")
    sm.add_argument("file")
    sm.add_argument("--url", default="http://127.0.0.1:8080")
    sm.add_argument("--token", help="省略時は環境変数 RP_TOKEN")
    sm.add_argument("--title")
    sm.add_argument("--wait", action="store_true", help="結果まで待って表示する（終了コードは上の一覧）")
    sm.add_argument("--timeout", type=int, default=600, help="--wait で待つ秒数（既定 600）")
    sm.add_argument("--format", choices=["md", "json"], default="md")
    sm.set_defaults(fn=cmd_submit)
    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except ConfigError as e:
        print(f"設定の誤り: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
