from __future__ import annotations

import base64

import pytest
from starlette.testclient import TestClient

from conftest import TOKEN, TOKEN2, FakeRunner, xlsx_bytes
from review_platform.adapters.llm import FakeLLM
from review_platform.adapters.notify import NoNotifier
from review_platform.api import create_app
from review_platform.worker import Worker

H = {"authorization": f"Bearer {TOKEN}"}
H2 = {"authorization": f"Bearer {TOKEN2}"}


@pytest.fixture
def client(settings, svc):
    with TestClient(create_app(settings, svc)) as c:
        yield c


def test_health_is_public_and_has_security_headers(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("headers", [{}, {"authorization": "Bearer wrong"}, {"authorization": "Basic abc"}])
def test_auth_required(client, headers):
    assert client.get("/v1/jobs", headers=headers).status_code == 401
    assert client.post("/mcp", headers=headers, json={}).status_code == 401


def test_submit_multipart_and_json(client):
    r = client.post("/v1/jobs", headers=H, files={"file": ("設計書.xlsx", xlsx_bytes())}, data={"title": "会員 API"})
    assert r.status_code == 202, r.text
    job = r.json()
    assert job["status"] == "queued" and job["kind"] == "document" and job["title"] == "会員 API"
    assert r.headers["location"] == f"/v1/jobs/{job['job_id']}"
    r2 = client.post("/v1/jobs", headers=H, json={"filename": "x.diff", "content_base64": base64.b64encode(b"diff --git a/x b/x\n").decode()})
    assert r2.status_code == 202 and r2.json()["kind"] == "diff"
    jobs = client.get("/v1/jobs", headers=H).json()["jobs"]
    assert [j["job_id"] for j in jobs] == [r2.json()["job_id"], job["job_id"]]


@pytest.mark.parametrize(
    ("kwargs", "status"),
    [
        ({"files": {"file": ("a.exe", b"MZ")}}, 400),
        ({"files": {"file": ("a.xlsx", b"not a zip")}}, 400),
        ({"files": {"file": ("a.pdf", b"PK\x03\x04")}}, 400),
        ({"files": {"file": ("a.md", b"\xff\xfe")}}, 400),
        ({"files": {"file": ("a.diff", b"hello")}}, 400),
        ({"files": {"file": ("a.md", b"")}}, 400),
        ({"json": {"filename": "a.md", "content_base64": "@@@"}}, 400),
        ({"json": [1, 2]}, 400),
        ({"content": b"x", "headers": {"content-type": "text/plain"}}, 415),
    ],
)
def test_submit_rejects_bad_input(client, kwargs, status):
    headers = {**H, **kwargs.pop("headers", {})}
    assert client.post("/v1/jobs", headers=headers, **kwargs).status_code == status


def test_submit_too_large(client, settings):
    big = b"PK\x03\x04" + b"0" * (settings.max_upload_bytes + 10)
    r = client.post("/v1/jobs", headers=H, files={"file": ("big.xlsx", big)})
    assert r.status_code == 413


def test_jobs_are_private_and_report_flow(client, settings, svc):
    job = client.post("/v1/jobs", headers=H, files={"file": ("doc.md", "# 設計\n本文".encode())}).json()
    jid = job["job_id"]
    assert client.get(f"/v1/jobs/{jid}", headers=H2).status_code == 404  # 他人のジョブは「無い」
    assert client.get("/v1/jobs/../../etc", headers=H).status_code == 404
    assert client.get(f"/v1/jobs/{jid}/report", headers=H).status_code == 409
    w = Worker(settings, svc.blobs, svc.queue, svc.repo, FakeLLM(), NoNotifier(), FakeRunner())
    assert w.run_once(wait_seconds=1)
    assert client.get(f"/v1/jobs/{jid}", headers=H).json()["status"] == "succeeded"
    md = client.get(f"/v1/jobs/{jid}/report", headers=H)
    assert md.status_code == 200 and md.text.startswith("# レビュー結果") and md.headers["content-type"].startswith("text/markdown")
    js = client.get(f"/v1/jobs/{jid}/report?format=json", headers=H).json()
    assert js["schema"] == "review-platform/report@1" and js["job"]["status"] == "succeeded"
    assert client.get(f"/v1/jobs/{jid}/report", headers=H2).status_code == 404
    assert client.get(f"/v1/jobs/{jid}/report?format=html", headers=H).status_code == 400


def test_rate_limit(settings, svc):
    from dataclasses import replace

    s = replace(settings, rate_limit_per_minute=2)
    with TestClient(create_app(s, svc)) as c:
        codes = [c.post("/v1/jobs", headers=H, files={"file": ("a.md", b"# x")}).status_code for _ in range(3)]
    assert codes == [202, 202, 429]
