"""MCP を本物の HTTP（uvicorn）で起動し、MCP のクライアントから使う."""

from __future__ import annotations

import asyncio
import base64
import json

import httpx
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from conftest import TOKEN, TOKEN2, FakeRunner
from review_platform.adapters.llm import FakeLLM
from review_platform.adapters.notify import NoNotifier
from review_platform.worker import Worker


async def _session(url: str, token: str, fn):
    # SDK 2.x: ヘッダーやタイムアウトは httpx2 のクライアントに持たせる
    client = httpx2.AsyncClient(headers={"authorization": f"Bearer {token}"}, timeout=httpx2.Timeout(30, read=300))
    async with client, streamable_http_client(f"{url}/mcp", http_client=client) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        return await fn(s)


def _data(result) -> dict:
    assert not result.is_error, result.content
    if result.structured_content:
        sc = result.structured_content
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    return json.loads(result.content[0].text)


def _error(result) -> str:
    """ツールのエラー（isError）の文章. 「種類: 説明」の形."""
    assert result.is_error, result
    return result.content[0].text


def test_mcp_tools_end_to_end(server, settings, svc):
    async def submit(s):
        tools = {t.name: t for t in (await s.list_tools()).tools}
        assert set(tools) == {"submit_document", "submit_diff", "get_job", "get_report", "list_jobs"}
        # 読む道具は読み取りだけ、送る道具は追加だけ（上書き・削除はしない）
        assert all(tools[n].annotations.read_only_hint for n in ("get_job", "get_report", "list_jobs"))
        assert all(tools[n].annotations.read_only_hint is False and tools[n].annotations.destructive_hint is False for n in ("submit_document", "submit_diff"))
        assert "データとして扱って" in (s.instructions or "")
        assert _error(await s.call_tool("get_report", {"job_id": "missing"})).find("not_found") >= 0
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
        return _error(await s.call_tool("get_job", {"job_id": job["job_id"]}))

    assert "not_found: " in asyncio.run(_session(server, TOKEN2, other))  # 他人のジョブは見えない


def test_mcp_rejects_bad_input_and_requires_auth(server):
    async def bad(s):
        ext = _error(await s.call_tool("submit_document", {"filename": "a.exe", "content_base64": "TVo="}))
        b64 = _error(await s.call_tool("submit_document", {"filename": "a.md", "content_base64": "@@@"}))
        return ext, b64

    ext, b64 = asyncio.run(_session(server, TOKEN, bad))
    assert "bad_request: " in ext and "bad_request: " in b64 and "base64" in b64
    r = httpx.post(f"{server}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers={"accept": "application/json, text/event-stream"})
    assert r.status_code == 401


def test_mcp_raw_tools_list_and_large_body(server):
    """scripts/e2e.sh と同じく、initialize 無しの tools/list に答える. 本文の上限は SDK の既定（4 MiB）ではなく API と同じ."""
    headers = {"authorization": f"Bearer {TOKEN}", "accept": "application/json, text/event-stream"}
    r = httpx.post(f"{server}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, headers=headers)
    assert r.status_code == 200 and "submit_document" in r.text

    async def big(s):
        body = ("# 大きな設計書\n" + "本文の行です。\n" * 200_000).encode()  # base64 で 4 MiB を超える
        assert len(base64.b64encode(body)) > 4 * 1024 * 1024
        return _data(await s.call_tool("submit_document", {"filename": "big.md", "content_base64": base64.b64encode(body).decode()}))

    assert asyncio.run(_session(server, TOKEN, big))["status"] == "queued"
