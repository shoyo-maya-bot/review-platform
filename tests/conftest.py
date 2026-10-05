from __future__ import annotations

import hashlib
import io
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from review_platform.config import Settings
from review_platform.factory import services
from review_platform.pipeline.convert import ToolRunner

TOKEN = "rp_test-token"
TOKEN2 = "rp_other-user"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return replace(
        Settings(),
        data_dir=tmp_path / "data",
        auth="token",
        api_token_hashes=(hashlib.sha256(TOKEN.encode()).hexdigest(), hashlib.sha256(TOKEN2.encode()).hexdigest()),
        llm="fake",
        rate_limit_per_minute=1000,
    )


@pytest.fixture
def svc(settings):
    return services(settings)


def xlsx_bytes() -> bytes:
    """中身は問わない ZIP（Office の形だけ）. 変換は FakeRunner が受け持つ."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
    return buf.getvalue()


class FakeRunner(ToolRunner):
    """doc2md の代わり. 決まった Markdown と変換レポートを書き出す."""

    def __init__(self, code: int = 0, markdown: str = "# 項目定義\n\n| 項目 | 桁 |\n|---|---|\n| 会員ID | 10 |\n", warnings: list[str] | None = None):
        self.code, self.markdown, self.warnings = code, markdown, warnings or []
        self.calls: list[list[str]] = []

    def run(self, args, cwd, timeout):
        import json

        self.calls.append(args)
        out = Path(args[args.index("-d") + 1])
        report = Path(args[args.index("-o") + 1])
        if self.code in (0,):
            (out / (Path(args[1]).stem + ".md")).write_text(self.markdown, encoding="utf-8")
        findings = [{"rule_id": "loss.raw_text", "severity": "warning", "message": w} for w in self.warnings]
        report.write_text(json.dumps({"findings": findings}), encoding="utf-8")
        return self.code, "", "boom" if self.code else ""
