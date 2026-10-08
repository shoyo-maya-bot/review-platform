"""料金表に無いモデル（Bedrock の application-inference-profile の ARN など）の費用の見積もり.

以前は (4, 20) で見積もっていたが、料金表には 5/25・15/75 のモデルがあり、費用を少なく数えて予算の確認が甘くなった.
"""

from __future__ import annotations

import logging

import pytest
from devtools_common.llm import PRICES

from review_platform.adapters import llm as llm_mod
from review_platform.adapters.llm import UNKNOWN_PRICE, cost, price_for

APP_PROFILE = "arn:aws:bedrock:ap-northeast-1:123456789012:application-inference-profile/a1b2c3d4e5f6"


def test_unknown_price_is_the_highest_in_the_table():
    assert (max(p.input for p in PRICES.values()), max(p.output for p in PRICES.values())) == UNKNOWN_PRICE
    for p in PRICES.values():
        assert UNKNOWN_PRICE[0] >= p.input and UNKNOWN_PRICE[1] >= p.output


def test_application_inference_profile_is_not_undercounted():
    """ID からモデルが分からない ARN は、表のどのモデルより安く数えない."""
    assert price_for(APP_PROFILE) == UNKNOWN_PRICE
    most_expensive = max(cost(m, 1_000_000, 1_000_000) for m in PRICES)
    assert cost(APP_PROFILE, 1_000_000, 1_000_000) >= most_expensive
    assert cost(APP_PROFILE, 1_000_000, 1_000_000) == pytest.approx(15.0 + 75.0)


def test_unknown_model_warns_once(caplog, monkeypatch):
    monkeypatch.setattr(llm_mod, "_warned_unknown", set())
    with caplog.at_level(logging.WARNING, logger=llm_mod.__name__):
        price_for("mystery-model-1")
        price_for("mystery-model-1")
        price_for("claude-haiku-4-5")  # 表にあるモデルは警告しない
    warned = [r for r in caplog.records if "料金表に無いモデル" in r.getMessage()]
    assert len(warned) == 1 and "mystery-model-1" in warned[0].getMessage()
