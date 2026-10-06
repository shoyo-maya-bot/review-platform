"""LLM に送る前の伏せ字（RP_MASK_PII）. 本物の LLM は呼ばない."""

from __future__ import annotations

from dataclasses import replace

import pytest

from review_platform.adapters.llm import FakeLLM, Masker
from review_platform.config import ConfigError, Settings
from review_platform.factory import llm
from review_platform.pipeline.diffreview import review_diff
from review_platform.pipeline.document import DocumentReviewer

DOC = "# 連絡先\n担当: 山田 taro.yamada@example.co.jp / 090-1234-5678 / ０３－１２３４－５６７８ / +81 90 1234 5678\n受付番号 2024-0001"
PII = ("taro.yamada@example.co.jp", "090-1234-5678", "０３－１２３４－５６７８", "+81 90 1234 5678")
DIFF = """diff --git a/src/Contact.java b/src/Contact.java
--- a/src/Contact.java
+++ b/src/Contact.java
@@ -1,2 +1,3 @@
 class Contact {
+  String admin = "admin@example.com"; // 03-1234-5678
 }
"""


@pytest.mark.parametrize(("raw", "want"), [(None, True), ("1", True), ("true", True), ("ON", True), ("0", False), ("false", False), ("off", False)])
def test_rp_mask_pii_env(monkeypatch, raw, want):
    monkeypatch.setenv("RP_AUTH", "none")
    if raw is None:
        monkeypatch.delenv("RP_MASK_PII", raising=False)
    else:
        monkeypatch.setenv("RP_MASK_PII", raw)
    assert Settings.from_env().mask_pii is want


def test_rp_mask_pii_rejects_unknown_value(monkeypatch):
    monkeypatch.setenv("RP_AUTH", "none")
    monkeypatch.setenv("RP_MASK_PII", "maybe")
    with pytest.raises(ConfigError, match="RP_MASK_PII"):
        Settings.from_env()


def test_factory_masks_documents_by_default():
    fake = llm(replace(Settings(), llm="fake"))
    assert isinstance(fake, FakeLLM) and fake.masker is not None
    DocumentReviewer(fake, "claude-sonnet-5-5", "", budget_usd=5).review("連絡先.md", DOC)
    user = fake.calls[0]["user"]
    assert not [p for p in PII if p in user]
    assert "＊＊＊" in user and "受付番号 2024-0001" in user  # 個人情報でない番号は残す


def test_factory_masks_diffs_by_default():
    fake = llm(replace(Settings(), llm="fake"))
    review_diff(DIFF, fake, "claude-sonnet-5-5", "", budget_usd=1.0)
    user = fake.calls[0]["user"]
    assert "admin@example.com" not in user and "03-1234-5678" not in user and "Contact.java" in user


def test_masking_can_be_turned_off():
    fake = llm(replace(Settings(), llm="fake", mask_pii=False))
    assert fake.masker is None
    DocumentReviewer(fake, "claude-sonnet-5-5", "", budget_usd=5).review("a.md", DOC)
    assert "taro.yamada@example.co.jp" in fake.calls[0]["user"]


def test_factory_passes_masker_to_anthropic():
    a = llm(replace(Settings(), llm="anthropic", anthropic_api_key="k"))
    assert isinstance(a.masker, Masker) and a.client("claude-sonnet-5-5").masker is a.masker
    assert llm(replace(Settings(), llm="anthropic", anthropic_api_key="k", mask_pii=False)).masker is None


def test_fake_masks_system_too():
    fake = FakeLLM(masker=Masker())
    fake.complete("m", "問い合わせ先 help@example.com", "u", 10)
    assert "help@example.com" not in fake.calls[0]["system"]
