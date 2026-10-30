"""ドキュメントと実際の動き（環境変数と既定値・終了コード・対応 Python・CI の版）のずれを機械で見つける."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from review_platform import cli
from review_platform.config import Settings

ROOT = Path(__file__).resolve().parents[1]
ENV = re.compile(r"\bRP_[A-Z0-9_]+")
# アプリの設定ではないもの（README の「開発」に書く）
NOT_SETTINGS = {"RP_TEST_AZURITE"}


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def config_rows() -> dict[str, str]:
    """docs/CONFIG.md の表の行 `| `RP_X` | 既定 | 意味 |` → {変数: 既定（（空）は ""）}."""
    rows = {}
    for line in read("docs/CONFIG.md").splitlines():
        m = re.match(r"^\|\s*`(RP_[A-Z0-9_]+)`\s*\|\s*([^|]*)\|", line)
        if m:
            raw = m.group(2).strip()
            rows[m.group(1)] = "" if raw == "（空）" else raw.strip("`")
    return rows


def test_every_env_var_in_code_is_documented_and_vice_versa():
    used = set()
    for p in (ROOT / "src").rglob("*.py"):
        used |= {v for v in ENV.findall(p.read_text(encoding="utf-8")) if not v.endswith("_")}  # `RP_BUDGET_*` は除く
    documented = set(config_rows())
    assert used - NOT_SETTINGS - documented == set(), "コードで読むのに docs/CONFIG.md に無い"
    assert documented - used == set(), "docs/CONFIG.md にあるのにコードで読まない"


@pytest.mark.parametrize(("name", "default"), sorted(config_rows().items()))
def test_documented_default_matches_settings(monkeypatch, name, default):
    """CONFIG.md の既定を明示しても、何も設定しないときと同じ設定になる（既定値の書き違いを見つける）."""
    if name == "RP_TOKEN":  # CLI の変数（Settings ではない）
        return
    for k in [k for k in os.environ if k.startswith("RP_") or k == "ANTHROPIC_API_KEY"]:
        monkeypatch.delenv(k)
    base = Settings.from_env()
    monkeypatch.setenv(name, default)
    assert Settings.from_env() == base, f"{name} の既定が docs/CONFIG.md（{default!r}）と違う"


def test_readme_python_version_matches_pyproject():
    m = re.search(r'^requires-python\s*=\s*">=\s*(3\.\d+)"', read("pyproject.toml"), re.M)
    assert m
    assert f"Python {m.group(1)} 以上" in read("README.md")


def test_readme_ci_versions_match_workflow_and_image():
    ci = read(".github/workflows/ci.yml")
    matrix = re.search(r"python:\s*\[([^\]]*)\]", ci)
    assert matrix
    linux = [v.strip().strip("'\"") for v in matrix.group(1).split(",")]
    image = re.search(r"^FROM python:(3\.\d+)-", read("Dockerfile"), re.M)
    assert image
    windows = re.search(r"windows.*?python-version:\s*\"?(3\.\d+)", ci, re.S)
    assert windows
    text = read("README.md")
    expected = f"Linux（Python {'・'.join(linux)}. イメージは {image.group(1)}）と Windows（{windows.group(1)}）"
    assert expected in text


def test_readme_submit_exit_codes_match_cli():
    codes = {v for k, v in vars(cli).items() if k.startswith("EXIT_") and isinstance(v, int)}
    text = read("README.md")
    section = text[text.index("### submit の終了コード") : text.index("## VS Code")]
    table = {int(c) for c in re.findall(r"^\|\s*`(\d+)`\s*\|", section, re.M)}
    assert table == codes
    doc = cli.__doc__ or ""
    line = doc[doc.index("submit の終了コード") :]
    assert {int(c) for c in re.findall(r"(?:^|/|:)\s*(\d+)\s", line)} == codes


def test_readme_changelog_stays_short_and_links_the_full_log():
    """README の変更点は最近の要点だけ（長くなったら CHANGELOG.md に移す）. 全件は CHANGELOG.md."""
    text = read("README.md")
    section = text[text.index("## 変更点") : text.index("## セキュリティ・ライセンス")]
    assert "[CHANGELOG.md](CHANGELOG.md)" in section
    assert len(re.findall(r"^- ", section, re.M)) <= 6
    assert read("CHANGELOG.md").startswith("# 変更点")


def test_readme_submit_options_match_parser():
    """README の「submit のオプション」の表と、submit の引数（既定値を含む）が同じ."""
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if a.dest == "cmd")  # noqa: SLF001
    submit = sub.choices["submit"]
    flags = {a.option_strings[0]: a.default for a in submit._actions if a.option_strings and a.option_strings[0] != "-h"}  # noqa: SLF001
    text = read("README.md")
    section = text[text.index("### submit のオプション") : text.index("### submit の終了コード")]
    rows = dict(re.findall(r"^\|\s*`(--[a-z-]+)`\s*\|\s*([^|]*?)\s*\|", section, re.M))
    assert set(rows) == set(flags)
    for flag, default in flags.items():
        if isinstance(default, (str, int)) and not isinstance(default, bool):
            assert rows[flag] == f"`{default}`", flag
    assert f"timeout={60}" in read("src/review_platform/cli.py") and "1 回の通信の時間切れは 60 秒" in section
    assert f"最大 {cli.POLL_RETRIES} 回再試行" in section and f"{cli.POLL_INTERVAL:.0f} 秒ごと" in section
