"""設計書を Markdown にする（doc-to-markdown の CLI `doc2md` を別プロセスで呼ぶ）."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import PermanentError, RetryableError

DOC2MD_TIMEOUT = 600

# 子プロセス（doc2md）に渡す環境変数. API キーなどの秘密を受け継がせないよう、決まったものだけを渡す
CHILD_ENV_NAMES = (
    "PATH", "HOME", "USERPROFILE", "TEMP", "TMP", "TMPDIR", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "TZ",
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "APPDATA", "LOCALAPPDATA",  # Windows で Python・LibreOffice が動くのに要る
    "PYTHONIOENCODING", "PYTHONUTF8", "VIRTUAL_ENV",
)  # fmt: skip
CHILD_ENV_PREFIXES = ("DEVTOOLS_", "DOC2MD_")
# DEVTOOLS_* でも、秘密らしい名前は渡さない
_SECRET_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")


def child_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    """doc2md に渡す環境変数. 決まった名前と DEVTOOLS_*・DOC2MD_* だけ（秘密らしい名前は除く）."""
    src = os.environ if environ is None else environ
    names = {n.upper() for n in CHILD_ENV_NAMES}
    out = {}
    for k, v in src.items():
        up = k.upper()
        if up in names or (up.startswith(CHILD_ENV_PREFIXES) and not any(h in up for h in _SECRET_HINTS)):
            out[k] = v
    return out


@dataclass
class Conversion:
    markdown: str
    warnings: list[str] = field(default_factory=list)


class ToolRunner:
    """外部コマンドを、シェルを通さず・時間の上限つきで実行する. 試験では差し替える."""

    def run(self, args: list[str], cwd: Path, timeout: int) -> tuple[int, str, str]:
        try:
            p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False, env=child_env())
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
    if code == 2:
        # 壊れた入力（拡張子だけ Office のテキスト、途中で切れた zip 等）・使い方の誤り. 同じ入力でやり直しても変わらないので再試行しない
        raise PermanentError(f"doc2md: 入力の誤りのため変換できませんでした（終了コード 2）: {stderr_summary(stderr, workdir)}")
    if code == 3:
        raise RetryableError(f"doc2md の実行時エラー（終了コード 3）: {stderr_summary(stderr, workdir)}")
    mds = sorted(out.glob("*.md"))
    if code == 1 or not mds:
        raise PermanentError("doc2md: 変換できませんでした" + (f"（{'; '.join(warnings[:3])}）" if warnings else ""))
    return Conversion(markdown=mds[0].read_text(encoding="utf-8"), warnings=warnings)


_ERROR_LINE = re.compile(r"^error:\s*", re.M)
STDERR_SUMMARY_MAX = 300


def stderr_summary(stderr: str, workdir: Path | None = None) -> str:
    """doc2md の stderr から、ジョブのエラーに残す要点を作る.

    - `error: ...` の行（devtools-common の run_main が書く理由）があれば最後のものを、無ければ最後の空でない行を使う
    - 作業フォルダ（一時フォルダ）のパスは外す（ファイル名だけ残す）
    - メールアドレス・電話番号は伏せる（devtools_common.llm.Masker. RP_MASK_PII に依らず、ジョブのエラーは常に伏せる）
    """
    from devtools_common.llm import Masker

    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    errors = [ln for ln in lines if _ERROR_LINE.match(ln)]
    line = _ERROR_LINE.sub("", errors[-1]) if errors else (lines[-1] if lines else "")
    if workdir is not None:
        for base in {str(workdir), workdir.as_posix()}:
            for sub in ("input", "out"):
                line = line.replace(f"{base}{os.sep}{sub}{os.sep}", "").replace(f"{base}/{sub}/", "")
            line = line.replace(base, "")
    line = Masker().text(line)
    if len(line) > STDERR_SUMMARY_MAX:
        line = line[: STDERR_SUMMARY_MAX - 1] + "…"
    return line or "（理由は出力されませんでした）"


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
