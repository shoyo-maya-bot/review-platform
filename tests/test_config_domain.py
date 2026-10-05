from __future__ import annotations

from dataclasses import replace

import pytest

from review_platform.config import ConfigError, Settings
from review_platform.domain import Job, JobKind, kind_for, safe_filename, valid_job_id


def test_from_env_defaults(monkeypatch):
    for k in list(__import__("os").environ):
        if k.startswith("RP_"):
            monkeypatch.delenv(k)
    s = Settings.from_env()
    assert s.backend == "local" and s.auth == "token" and s.max_upload_bytes == 10 * 1024 * 1024
    assert ".xlsx" in s.allowed_extensions


@pytest.mark.parametrize(
    ("env", "msg"),
    [
        ({"RP_BACKEND": "gcp"}, "RP_BACKEND"),
        ({"RP_MAX_UPLOAD_MB": "0"}, "RP_MAX_UPLOAD_MB"),
        ({"RP_AUTH": "none", "RP_HOST": "0.0.0.0"}, "RP_AUTH=none"),
        ({"RP_AUTH": "jwt"}, "RP_JWT_ISSUER"),
        ({"RP_API_TOKENS_SHA256": "my-plain-token"}, "SHA-256"),
        ({"RP_BACKEND": "aws"}, "RP_S3_BUCKET"),
        ({"RP_BACKEND": "azure"}, "RP_AZURE_BLOB_URL"),
        ({"RP_LLM_BASE_URL": "http://example.com"}, "https"),
    ],
)
def test_invalid_env_is_rejected(monkeypatch, env, msg):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ConfigError, match=msg):
        Settings.from_env()


def test_role_specific_validation():
    with pytest.raises(ConfigError, match="RP_API_TOKENS_SHA256"):
        Settings().validate("api")
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        replace(Settings(), llm="anthropic").validate("worker")
    with pytest.raises(ConfigError, match="RP_SLACK_WEBHOOK_URL"):
        replace(Settings(), notify="slack", slack_webhook_url="http://x").validate("worker")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("設計書.xlsx", "設計書.xlsx"),
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\me\\仕様.DOCX", "仕様.docx"),
        ('a<b>:"|?*.pdf', "ab.pdf"),
        ("...", "upload"),
        ("x" * 300 + ".xlsx", "x" * 115 + ".xlsx"),
    ],
)
def test_safe_filename(name, expected):
    assert safe_filename(name) == expected


def test_kind_and_ids():
    assert kind_for("a.xlsx") == JobKind.DOCUMENT and kind_for("a.patch") == JobKind.DIFF and kind_for("a.exe") is None
    job = Job.create("u", JobKind.DOCUMENT, "../a.xlsx", 30)
    assert valid_job_id(job.job_id) and job.input_key == f"uploads/{job.job_id}/a.xlsx"
    assert not valid_job_id("../../x") and not valid_job_id("")
    assert Job.from_dict(job.to_dict()) == job
    assert "input_key" not in job.public_view() and "owner" not in job.public_view()
