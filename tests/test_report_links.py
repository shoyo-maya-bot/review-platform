"""結果の Markdown の無害化: AI の出力に混ざった Markdown のリンク・画像は、文字だけを残す（外の URL を開かせない・読み込ませない）."""

from __future__ import annotations

import re

import pytest

from review_platform.pipeline.report import clean, strip_links

LINK = re.compile(r"\]\(|\]\[")


@pytest.mark.parametrize(
    ("raw", "text"),
    [
        ("詳しくは [こちら](https://evil.example/x) を参照", "詳しくは こちら を参照"),
        ("![追跡](https://evil.example/pixel.png)", "追跡"),
        ("![](https://evil.example/pixel.png)", ""),
        ("[説明](https://evil.example/a_(b)) の通り", "説明 の通り"),
        ("[参照形式][ref] です", "参照形式 です"),
        ("![画像][logo]", "画像"),
        ("[[入れ子](https://a.example)](https://b.example)", "入れ子"),
    ],
)
def test_strip_links_keeps_only_text(raw, text):
    assert strip_links(raw) == text


@pytest.mark.parametrize(
    "raw",
    [
        "[こちら](https://evil.example/x)",
        "![x](https://evil.example/p.png)",
        "[a][r] と [r]: https://evil.example",
        "[[入れ子](https://a.example)](https://b.example)",
    ],
)
def test_clean_leaves_no_link_or_image(raw):
    out = clean(raw)
    assert "evil.example/x" not in out and "p.png" not in out and "a.example" not in out
    assert not LINK.search(out)
    assert "![" not in out
    # 残った角かっこは文字として出す（参照定義とつながらない）
    assert re.search(r"(?<!\\)[\[\]]", out) is None


def test_clean_still_escapes_html_and_mentions():
    out = clean("<img src=x> @channel | 表")
    assert "<img" not in out and "@​channel" in out and "\\|" in out


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        ("詳しくは https://evil.example/x?a=1 を参照", "詳しくは hxxps://evil\\[.\\]example/x?a=1 を参照"),
        ("http://evil.example", "hxxp://evil\\[.\\]example"),
        ("HTTPS://Evil.Example/p", "HxxPS://Evil\\[.\\]Example/p"),
        ("www.evil.example/login", "www\\[.\\]evil\\[.\\]example/login"),
        ("(www.evil.example)", "(www\\[.\\]evil\\[.\\]example)"),
        ("ftp://files.example/a", "ftp://files\\[.\\]example/a"),
        ("<https://evil.example>", "&lt;hxxps://evil\\[.\\]example&gt;"),
    ],
)
def test_clean_defangs_bare_urls_and_www(raw, shown):
    """第 7 回の指摘: Markdown のリンクの形でない生の URL・www. も、表示で自動リンクにならない形にする."""
    out = clean(raw)
    assert out == shown
    assert not re.search(r"(?i)\b(?:https?://|www\.)", out)


def test_clean_keeps_text_without_urls():
    assert clean("設計書 3.2 節の api.v1 を参照") == "設計書 3.2 節の api.v1 を参照"


def test_markdown_report_has_no_autolinks():
    from review_platform.domain import Job, JobKind
    from review_platform.pipeline.report import markdown

    job = Job.create("alice", JobKind.DOCUMENT, "a.md", 7, title="設計 https://evil.example/t")
    rep = {
        "job": job.public_view(),
        "conversion_warnings": ["www.evil.example を参照"],
        "notes": [],
        "findings": [
            {"severity": "MUST", "status": "confirmed", "location": "1", "title": "t", "detail": "http://evil.example/a",
             "evidence": "e", "models": [], "verdict_note": ""},
        ],
        "rejected": [],
        "calls": [],
        "cost_usd": 0.0,
    }  # fmt: skip
    md = markdown(rep)
    assert not re.search(r"(?i)\b(?:https?://|www\.)", md)
