"""review-platform のコマンド.

review-platform api                  API と MCP を起動
review-platform worker [--once]      ワーカーを起動
review-platform token                API トークンを作る（トークンと、サーバーに置く SHA-256 を表示）
review-platform check-config --role api|worker
review-platform submit FILE --url URL [--token T] [--wait]   ファイルを送って結果を表示
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
    uvicorn.run(app, host=s.host, port=s.port, log_config=None, proxy_headers=True, forwarded_allow_ips=s.forwarded_allow_ips, server_header=False, timeout_keep_alive=s.keep_alive_seconds)
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


def cmd_submit(args) -> int:
    import httpx

    token = args.token or os.environ.get("RP_TOKEN", "")
    headers = {"authorization": f"Bearer {token}"} if token else {}
    path = Path(args.file)
    with httpx.Client(base_url=args.url.rstrip("/"), headers=headers, timeout=60) as c:
        r = c.post("/v1/jobs", files={"file": (path.name, path.read_bytes())}, data={"title": args.title or ""})
        if r.status_code != 202:
            print(f"送れませんでした（{r.status_code}）: {r.text}", file=sys.stderr)
            return 1
        job = r.json()
        print(f"受け付けました: {job['job_id']}", file=sys.stderr)
        if not args.wait:
            print(job["job_id"])
            return 0
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            job = c.get(f"/v1/jobs/{job['job_id']}").json()
            if job["status"] in ("succeeded", "failed"):
                break
            time.sleep(3)
        if job["status"] != "succeeded":
            print(f"終わりませんでした（{job['status']}）: {job.get('error', '')}", file=sys.stderr)
            return 1
        print(c.get(f"/v1/jobs/{job['job_id']}/report", params={"format": args.format}).text)
        return 1 if job["summary"].get("open_must") else 0


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
    sm.add_argument("--wait", action="store_true")
    sm.add_argument("--timeout", type=int, default=600)
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
