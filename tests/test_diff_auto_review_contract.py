"""auto-review（agent-lab）の振る舞いのうち、review-platform の差分のレビューが頼っているものを固定する.

- deep 段（最初から deep の差分・MUST の検証）の出力が読めないときは、ジョブを失敗にせず standard の結果を注記付きで返す
- review-platform はラベルを渡さないので、`Plan.escalate` は常に True（standard の MUST は上位モデルで検証する）
- 文書だけの変更でも、秘密情報らしき値があれば AI レビューを省かない（予算の確認を通ってから呼ぶ）
"""

from __future__ import annotations

import json

import pytest

from review_platform.adapters.llm import FakeLLM
from review_platform.pipeline import PermanentError
from review_platform.pipeline.diffreview import build_config, review_diff

STD, DEEP = "claude-sonnet-5-5", "claude-opus-5-5"
MUST = '```json\n{"findings": [{"severity": "MUST", "file": "src/Pay.java", "line": 2, "title": "金額が int", "detail": "d"}]}\n```'
BROKEN_DEEP = '```json\n{"verdicts": [{"id": "s1", "verdict": "confirm"},]}\n```'  # 末尾のカンマ

SMALL = """diff --git a/src/Pay.java b/src/Pay.java
--- a/src/Pay.java
+++ b/src/Pay.java
@@ -1,2 +1,3 @@
 class Pay {
+  int amount;
 }
"""


def _risky_diff() -> str:
    """最初から deep になる差分（危ないパス・大きさ・秘密らしき値で deep_score 以上）."""
    body = "".join(f"+  String password = 'p{i}';\n" for i in range(80))
    files = []
    for name in ("src/main/java/x/auth/LoginController.java", "src/main/resources/db/migration/V2__users.sql", "src/main/java/x/PaymentService.java"):
        files.append(f"diff --git a/{name} b/{name}\n--- a/{name}\n+++ b/{name}\n@@ -1,1 +1,81 @@\n class A {{\n{body}")
    return "".join(files)


def test_deep_tier_unreadable_output_returns_standard_result_with_note():
    from auto_review.diff import parse_diff
    from auto_review.router import plan

    diff = _risky_diff()
    assert plan(parse_diff(diff), build_config(STD, DEEP, 50.0)).tier == "deep"
    llm = FakeLLM([MUST, BROKEN_DEEP])
    out = review_diff(diff, llm, STD, DEEP, budget_usd=50.0)
    assert [c["model"] for c in llm.calls] == [STD, DEEP]
    assert [f.title for f in out.findings] == ["金額が int"]  # standard の結果が残る（ジョブは失敗にしない）
    assert any("deep モデルの出力を読めなかった" in n for n in out.notes)


def test_verify_step_unreadable_output_returns_standard_result_with_note():
    llm = FakeLLM([MUST, BROKEN_DEEP])
    out = review_diff(SMALL, llm, STD, DEEP, budget_usd=5.0)
    assert out.routing["tier"] == "standard"
    assert [c["model"] for c in llm.calls] == [STD, DEEP]
    assert [f.title for f in out.findings] == ["金額が int"]
    assert any("deep モデルの出力を読めなかった" in n for n in out.notes)


def test_standard_unreadable_output_still_fails_job():
    """一次（standard）の出力が読めないときは、これまでどおり失敗（指摘 0 件の成功にしない）."""
    with pytest.raises(PermanentError, match="読めませんでした"):
        review_diff(SMALL, FakeLLM([BROKEN_DEEP]), STD, DEEP, budget_usd=5.0)


def test_review_platform_does_not_pin_tier_by_label_so_musts_are_verified():
    """review-platform は plan にラベルを渡さない. `review:standard` の「上位モデルに上げない」は効かず、MUST は検証する."""
    from auto_review.diff import parse_diff
    from auto_review.router import plan

    p = plan(parse_diff(SMALL), build_config(STD, DEEP, 5.0))
    assert p.escalate is True and p.forced_by is None
    verify = '```json\n{"verdicts": [{"id": "s1", "verdict": "confirm", "reason": "r"}], "findings": []}\n```'
    llm = FakeLLM([MUST, verify])
    review_diff(SMALL, llm, STD, DEEP, budget_usd=5.0)
    assert [c["model"] for c in llm.calls] == [STD, DEEP]


DOC_SECRET = """diff --git a/docs/setup.md b/docs/setup.md
--- a/docs/setup.md
+++ b/docs/setup.md
@@ -1,1 +1,2 @@
 # 手順
+password = example-not-real
"""
DOC_ONLY = DOC_SECRET.replace("password = example-not-real", "手順を足す")


def test_docs_only_change_is_skipped_but_not_when_it_contains_a_secret():
    llm = FakeLLM(['```json\n{"findings": []}\n```'])
    out = review_diff(DOC_ONLY, llm, STD, DEEP, budget_usd=5.0)
    assert out.routing["tier"] == "skip" and llm.calls == []
    out = review_diff(DOC_SECRET, llm, STD, DEEP, budget_usd=5.0)
    assert out.routing["tier"] != "skip" and len(llm.calls) == 1 and "docs/setup.md" in out.routing["risky_files"]


def test_docs_only_secret_change_still_respects_budget():
    """省かなくなった文書の変更も、一次の呼び出しの前の予算の確認を通る（予算 0 なら呼ばない）."""
    llm = FakeLLM()
    with pytest.raises(PermanentError, match="見積もり"):
        review_diff(DOC_SECRET, llm, STD, DEEP, budget_usd=0.0)
    assert llm.calls == []


def test_routing_is_serializable():
    out = review_diff(DOC_ONLY, FakeLLM(), STD, DEEP, budget_usd=5.0)
    assert set(json.loads(json.dumps(out.routing))) == {"tier", "score", "reasons", "risky_files", "forced_by"}
