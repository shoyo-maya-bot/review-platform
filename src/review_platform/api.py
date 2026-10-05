"""HTTP API と MCP（/mcp）. 1 つの ASGI アプリにまとめ、認証は共通のミドルウェアで行う.

POST /v1/jobs                 設計書・差分を送る（multipart の file、または JSON の filename + content_base64）
GET  /v1/jobs                 自分のジョブの一覧
GET  /v1/jobs/{id}            ジョブの状態
GET  /v1/jobs/{id}/report     結果（?format=md|json）
GET  /healthz                 死活（認証なし）
POST /mcp                     MCP（Streamable HTTP、stateless）
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import json
import logging
from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__
from .auth import AuthMiddleware, build_verifier, principal_of
from .config import Settings
from .ports import NotFound
from .service import InputError, NotReady, Services

log = logging.getLogger(__name__)


def _err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": code, "message": message}, status_code=status)


class _BodyTooLarge(Exception):
    pass


async def _read_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise _BodyTooLarge
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > limit:
            raise _BodyTooLarge
    return bytes(buf)


class SecurityHeaders:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def _send(message):
            if message["type"] == "http.response.start":
                h = list(message.get("headers") or [])
                names = {k.lower() for k, _ in h}
                for k, v in ((b"x-content-type-options", b"nosniff"), (b"cache-control", b"no-store"), (b"x-frame-options", b"DENY"), (b"referrer-policy", b"no-referrer")):
                    if k not in names:
                        h.append((k, v))
                message["headers"] = h
            await send(message)

        await self.app(scope, receive, _send)


def create_app(settings: Settings, services: Services, verifier=None, mcp_enabled: bool = True) -> Starlette:
    s = settings
    limit = int(s.max_upload_bytes * 1.4) + 64 * 1024  # base64 と multipart の余白

    async def healthz(request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def submit(request: Request) -> Response:
        owner = principal_of(request.scope)
        ctype = request.headers.get("content-type", "")
        try:
            if ctype.startswith("multipart/form-data"):
                declared = request.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > limit:
                    raise _BodyTooLarge
                async with request.form(max_files=1, max_fields=5) as form:
                    up = form.get("file")
                    if up is None or isinstance(up, str):
                        return _err(400, "bad_request", "file が必要です（multipart/form-data）")
                    data = await up.read(s.max_upload_bytes + 1)
                    filename, title = up.filename or "upload", str(form.get("title") or "")
            elif ctype.startswith("application/json"):
                body = json.loads(await _read_body(request, limit) or b"{}")
                if not isinstance(body, dict):
                    return _err(400, "bad_request", "JSON のオブジェクトを送ってください")
                filename, title = str(body.get("filename") or ""), str(body.get("title") or "")
                data = base64.b64decode(str(body.get("content_base64") or ""), validate=True)
            else:
                return _err(415, "unsupported_media_type", "multipart/form-data か application/json で送ってください")
            job = services.submit(owner, filename, data, title)
        except _BodyTooLarge:
            return _err(413, "too_large", f"上限 {s.max_upload_bytes // (1024 * 1024)} MB を超えています")
        except (ValueError, binascii.Error) as e:
            if isinstance(e, InputError):
                return _err(400, "bad_request", str(e))
            return _err(400, "bad_request", "本文を読めません（JSON・base64 の形を確かめてください）")
        return JSONResponse(job.public_view(), status_code=202, headers={"location": f"/v1/jobs/{job.job_id}"})

    async def list_jobs(request: Request) -> Response:
        owner = principal_of(request.scope)
        try:
            limit = int(request.query_params.get("limit", "20"))
        except ValueError:
            return _err(400, "bad_request", "limit は整数です")
        return JSONResponse({"jobs": [j.public_view() for j in services.list_for(owner, limit)]})

    async def get_job(request: Request) -> Response:
        owner = principal_of(request.scope)
        try:
            job = services.get_for(owner, request.path_params["job_id"])
        except NotFound:
            return _err(404, "not_found", "ジョブが見つかりません")
        return JSONResponse(job.public_view())

    async def get_report(request: Request) -> Response:
        owner = principal_of(request.scope)
        fmt = request.query_params.get("format", "md")
        if fmt not in ("md", "json"):
            return _err(400, "bad_request", "format は md か json です")
        try:
            data = services.report_for(owner, request.path_params["job_id"], fmt)
        except NotFound:
            return _err(404, "not_found", "ジョブが見つかりません")
        except NotReady as e:
            return _err(409, "not_ready", f"まだ結果がありません（状態: {e}）")
        media = "text/markdown; charset=utf-8" if fmt == "md" else "application/json"
        return Response(data, media_type=media)

    async def version(request: Request) -> Response:
        return JSONResponse({"name": "review-platform", "version": __version__, "backend": s.backend})

    routes: list[Any] = [
        Route("/healthz", healthz, methods=["GET"]),
        Route("/v1/version", version, methods=["GET"]),
        Route("/v1/jobs", submit, methods=["POST"]),
        Route("/v1/jobs", list_jobs, methods=["GET"]),
        Route("/v1/jobs/{job_id}", get_job, methods=["GET"]),
        Route("/v1/jobs/{job_id}/report", get_report, methods=["GET"]),
    ]

    lifespan = None
    if mcp_enabled:
        from mcp.server.fastmcp.server import StreamableHTTPASGIApp

        from .mcp_server import build_mcp

        mcp = build_mcp(s, services)
        mcp.streamable_http_app()  # セッションマネージャを作る
        routes.append(Route("/mcp", StreamableHTTPASGIApp(mcp.session_manager), methods=["GET", "POST", "DELETE"]))

        @contextlib.asynccontextmanager
        async def lifespan(app):
            async with mcp.session_manager.run():
                yield

    middleware = [
        Middleware(SecurityHeaders),
        Middleware(AuthMiddleware, verifier=verifier if verifier is not None else build_verifier(s), rate_limit_per_minute=s.rate_limit_per_minute),
    ]
    return Starlette(routes=routes, middleware=middleware, lifespan=lifespan)
