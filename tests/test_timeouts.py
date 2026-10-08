"""MCP の get_job の最大待ちと、前に置く ALB のアイドルタイムアウト・アプリの keep-alive の関係を固定する.

ALB のアイドルタイムアウトが get_job の最大待ちに近いと、待っている間（応答のバイトが流れない）に ALB が接続を切り、
エージェントには 504 が返る. アプリの keep-alive が ALB より短いと、ALB が使い回そうとした接続をアプリが先に閉じて 502 になる.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from review_platform.config import ConfigError, Settings
from review_platform.mcp_server import GET_JOB_MAX_WAIT_SECONDS

ROOT = Path(__file__).resolve().parent.parent
MARGIN = 30  # 最大待ちに上乗せする余裕（秒）. DB の読み取り・ネットワークの往復の分


def _variable(name: str) -> str:
    text = (ROOT / "infra/aws/variables.tf").read_text(encoding="utf-8")
    m = re.search(rf'^variable "{name}" \{{\n(.*?)^\}}', text, re.M | re.S)
    assert m, name
    return m.group(1)


def test_alb_idle_timeout_default_and_minimum_exceed_mcp_wait():
    body = _variable("alb_idle_timeout")
    default = int(re.search(r"^\s*default\s*=\s*(\d+)", body, re.M).group(1))
    minimum = int(re.search(r"var\.alb_idle_timeout >= (\d+)", body).group(1))
    assert minimum >= GET_JOB_MAX_WAIT_SECONDS + MARGIN
    assert default >= minimum
    # 説明に書いた数字が実際の値と一致する
    desc = re.search(r'description\s*=\s*"([^"]*)"', body).group(1)
    assert f"最大 {GET_JOB_MAX_WAIT_SECONDS} 秒" in desc and f"既定 {default} 秒" in desc and f"最小 {minimum} 秒" in desc


def test_app_keep_alive_is_longer_than_alb_idle_timeout():
    main = (ROOT / "infra/aws/main.tf").read_text(encoding="utf-8")
    m = re.search(r"RP_KEEP_ALIVE_SECONDS\s*=\s*tostring\(var\.alb_idle_timeout \+ (\d+)\)", main)
    assert m and int(m.group(1)) > 0


def test_keep_alive_setting(monkeypatch):
    assert Settings.from_env().keep_alive_seconds == 65
    monkeypatch.setenv("RP_KEEP_ALIVE_SECONDS", "305")
    assert Settings.from_env().keep_alive_seconds == 305
    monkeypatch.setenv("RP_KEEP_ALIVE_SECONDS", "0")
    with pytest.raises(ConfigError):
        Settings.from_env()


def test_api_passes_keep_alive_to_uvicorn(monkeypatch):
    from review_platform import cli

    seen = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: seen.update(kw))
    monkeypatch.setenv("RP_API_TOKENS_SHA256", "0" * 64)
    monkeypatch.setenv("RP_KEEP_ALIVE_SECONDS", "305")
    monkeypatch.setenv("RP_DATA_DIR", str(ROOT / ".pytest-unused"))
    monkeypatch.setattr("review_platform.factory.services", lambda s: None)
    monkeypatch.setattr("review_platform.api.create_app", lambda s, svc: object())
    assert cli.main(["api"]) == 0
    assert seen["timeout_keep_alive"] == 305
