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
