"""設計書を Markdown にする（doc-to-markdown の CLI `doc2md` を別プロセスで呼ぶ）."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import PermanentError, RetryableError

DOC2MD_TIMEOUT = 600


@dataclass
class Conversion:
    markdown: str
    warnings: list[str] = field(default_factory=list)


class ToolRunner:
    """外部コマンドを、シェルを通さず・時間の上限つきで実行する. 試験では差し替える."""

    def run(self, args: list[str], cwd: Path, timeout: int) -> tuple[int, str, str]:
        try:
            p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)
        except FileNotFoundError as e:
            raise PermanentError(f"{args[0]} が見つかりません（イメージに入っていない）") from e
        except subprocess.TimeoutExpired as e:
            raise PermanentError(f"{args[0]} が {timeout} 秒で終わりませんでした") from e
        return p.returncode, p.stdout, p.stderr


def to_markdown(filename: str, data: bytes, workdir: Path, runner: ToolRunner | None = None) -> Conversion:
    if filename.lower().endswith(".md"):
        return Conversion(markdown=data.decode("utf-8"))
    runner = runner or ToolRunner()
    src = workdir / "input" / filename
    out = workdir / "out"
    src.parent.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    src.write_bytes(data)
    report = workdir / "doc2md-report.json"
    code, _stdout, stderr = runner.run(["doc2md", str(src), "-d", str(out), "-f", "json", "-o", str(report)], workdir, DOC2MD_TIMEOUT)
    warnings = _warnings(report)
    if code == 3:
        raise RetryableError(f"doc2md の実行時エラー: {stderr.strip()[-300:]}")
    if code == 2:
        raise PermanentError(f"doc2md: 入力か環境の誤り（終了コード 2）: {stderr.strip()[-300:]}")
    mds = sorted(out.glob("*.md"))
    if code == 1 or not mds:
        raise PermanentError("doc2md: 変換できませんでした" + (f"（{'; '.join(warnings[:3])}）" if warnings else ""))
    return Conversion(markdown=mds[0].read_text(encoding="utf-8"), warnings=warnings)


def _warnings(report: Path) -> list[str]:
    """変換ロス（loss.*）や注意（変更履歴・非表示の行・数式の未計算など）を、利用者に見せる文にする."""
    if not report.is_file():
        return []
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    out = []
    for f in data.get("findings", []):
        if str(f.get("severity", "")).lower() in ("warning", "error") or str(f.get("rule_id", "")).startswith("loss."):
            out.append(f"{f.get('rule_id', '')}: {str(f.get('message', ''))[:200]}".strip(": "))
    return out[:20]
