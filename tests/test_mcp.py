"""MCP を本物の HTTP（uvicorn）で起動し、MCP のクライアントから使う."""

from __future__ import annotations

import asyncio
import base64
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from conftest import TOKEN, TOKEN2, FakeRunner
from review_platform.adapters.llm import FakeLLM
from review_platform.adapters.notify import NoNotifier
from review_platform.api import create_app
from review_platform.worker import Worker


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(settings, svc):
    port = _free_port()
    cfg = uvicorn.Config(create_app(settings, svc), host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    srv = uvicorn.Server(cfg)
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/healthz").status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    t.join(timeout=5)


async def _session(url: str, token: str, fn):
    async with streamablehttp_client(f"{url}/mcp", headers={"authorization": f"Bearer {token}"}) as (r, w, _), ClientSession(r, w) as s:
        await s.initialize()
        return await fn(s)


def _data(result) -> dict:
    if getattr(result, "structuredContent", None):
        sc = result.structuredContent
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    return json.loads(result.content[0].text)


def test_mcp_tools_end_to_end(server, settings, svc):
    async def submit(s):
        tools = {t.name for t in (await s.list_tools()).tools}
        assert tools == {"submit_document", "submit_diff", "get_job", "get_report", "list_jobs"}
        res = await s.call_tool("submit_document", {"filename": "設計.md", "content_base64": base64.b64encode("# 設計\n本文".encode()).decode(), "title": "t"})
        return _data(res)

    job = asyncio.run(_session(server, TOKEN, submit))
    assert job["status"] == "queued"
    Worker(settings, svc.blobs, svc.queue, svc.repo, FakeLLM(), NoNotifier(), FakeRunner()).run_once(wait_seconds=1)

    async def read(s):
        j = _data(await s.call_tool("get_job", {"job_id": job["job_id"], "wait_seconds": 5}))
        rep = await s.call_tool("get_report", {"job_id": job["job_id"]})
        jobs = _data(await s.call_tool("list_jobs", {}))
        return j, rep.content[0].text, jobs

    j, md, jobs = asyncio.run(_session(server, TOKEN, read))
    assert j["status"] == "succeeded" and md.startswith("# レビュー結果") and jobs["jobs"][0]["job_id"] == job["job_id"]

    async def other(s):
        return _data(await s.call_tool("get_job", {"job_id": job["job_id"]}))

    assert asyncio.run(_session(server, TOKEN2, other))["error"] == "not_found"  # 他人のジョブは見えない


def test_mcp_rejects_bad_input_and_requires_auth(server):
    async def bad(s):
        return _data(await s.call_tool("submit_document", {"filename": "a.exe", "content_base64": "TVo="}))

    assert asyncio.run(_session(server, TOKEN, bad))["error"] == "bad_request"
    r = httpx.post(f"{server}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers={"accept": "application/json, text/event-stream"})
    assert r.status_code == 401
