"""設計書（Markdown）の AI レビュー. ふだんは standard モデル 1 回. MUST が出たら deep モデルがその指摘だけを検証する."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from importlib import resources

from auto_review.findings import (
    FINDINGS_SCHEMA,
    VERDICTS_SCHEMA,
    Finding,
    UnreadableOutput,
    apply_verdicts,
    findings_from_data,
    load_output,
    merge,
    norm_severity,
    ordered,
    verdicts_from_data,
)
from auto_review.tokens import estimate_tokens

from ..adapters.llm import LLMError, cost
from ..ports import LLMClient
from . import PermanentError, RetryableError

log = logging.getLogger(__name__)

CHARS_PER_TOKEN = 3.0  # ASCII（英数字・記号）の何文字で 1 トークンか. ASCII 以外は 1 文字 1 トークンで数える（estimate_input_tokens）
MAX_DOC_CHARS = 180_000  # 約 6 万トークン. 超えた分は省き、その旨を結果に書く
OUTPUT_TOKENS = {"standard": 4000, "verify": 3000}
MAX_FINDINGS = 20


@dataclass
class Call:
    step: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float


@dataclass
class ReviewOutcome:
    findings: list[Finding] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def cost_usd(self) -> float:
        return round(sum(c.cost_usd for c in self.calls), 4)


def _prompt(name: str, max_findings: int) -> str:
    text = resources.files("review_platform.prompts").joinpath(name).read_text(encoding="utf-8")
    return text.replace("{MAX_FINDINGS}", str(max_findings))


# 区切りに使うタグ. 本文の中にこれらの閉じタグ（大文字・空白入りも）があれば無害化する
_DELIMITERS = ("document", "findings")
_CLOSING = re.compile(r"<\s*/\s*(" + "|".join(_DELIMITERS) + r")\s*>", re.I)


def neutralize(text: str) -> str:
    """区切りの閉じタグ（`</document>`・`</DOCUMENT>`・`</document >`・`< /document>` など）を無害な形に変える."""
    return _CLOSING.sub(lambda m: f"<\\/{m.group(1).lower()}>", text)


def wrap_document(name: str, markdown: str) -> str:
    """信用しないデータとして区切る. 区切りの閉じタグが本文にあれば無害化する."""
    safe_name = name.replace('"', "'").replace("<", "‹").replace(">", "›")
    return f'<document name="{safe_name}">\n{neutralize(markdown)}\n</document>'


def wrap_findings(findings: list[Finding]) -> str:
    """一次レビューの指摘も信用しないデータとして区切る（文書に仕込まれた指示が指摘を経由して届くのを防ぐ）."""
    listing = "\n".join(f"- id={f.id} [{f.severity}] {f.file}: {f.title} — {f.detail}" for f in findings)
    return f"<findings>\n{neutralize(listing)}\n</findings>"


UNKNOWN_SEVERITY_NOTE = "要確認: AI が想定外の重要度「{raw}」を返したため、MUST として扱いました"


def normalize_severity(raw: object) -> str | None:
    """重要度を MUST / WANT / NITS / Q に読み替える. 読めない値は None（呼び出し側で要確認にする）.

    読み替えの表は auto-review（`auto_review.findings.SEVERITY_ALIASES`）の 1 か所だけに置き、ここでは持たない.
    """
    sev, known = norm_severity(raw)
    return sev if known else None


def parse_document_findings(text: str, model: str, prefix: str) -> tuple[list[Finding], dict[str, str]]:
    """一次レビューの出力をスキーマで検証して読み、（指摘, 読めない重要度 {id: 元の値}）を返す.

    JSON が無い・壊れている・スキーマに合わないときは UnreadableOutput（黙って指摘 0 件にしない）.
    """
    items = load_output(text, FINDINGS_SCHEMA)["findings"]
    return _with_unknown(items, findings_from_data(items, model, prefix))


def _with_unknown(items: list[dict], found: list[Finding]) -> tuple[list[Finding], dict[str, str]]:
    unknown: dict[str, str] = {}
    for x, f in zip(items, found, strict=True):
        if normalize_severity(x.get("severity")) is None:
            shown = str(x.get("severity") or "").strip() or "（空）"
            f.severity, f.status = "MUST", "uncertain"
            f.verdict_note = UNKNOWN_SEVERITY_NOTE.format(raw=shown)
            unknown[f.id] = shown
    return found, unknown


def _normalize_verdicts(verdicts: dict[str, dict]) -> dict[str, dict]:
    """検証役の重要度も読み替える. 読めない値は捨て、一次の重要度のまま残す（黙って下げない）."""
    for v in verdicts.values():
        if "severity" in v:
            sev = normalize_severity(v["severity"])
            if sev is None:
                v.pop("severity")
            else:
                v["severity"] = sev
    return verdicts


def estimate_input_tokens(text: str) -> int:
    """入力トークンの見積もり. 日本語など ASCII 以外は 1 文字 1 トークン、ASCII は CHARS_PER_TOKEN 文字で 1 トークン.

    設計書は日本語が多く、全体を「3 文字で 1 トークン」で数えると 2〜3 倍少なく見積もって予算の確認が甘くなるため.
    """
    return estimate_tokens(text, CHARS_PER_TOKEN)


def _estimate(model: str, text: str, out_tokens: int) -> float:
    return cost(model, estimate_input_tokens(text), out_tokens)


class DocumentReviewer:
    def __init__(self, llm: LLMClient, model_standard: str, model_deep: str, budget_usd: float):
        self.llm = llm
        self.std = model_standard
        self.deep = model_deep
        self.budget = budget_usd

    def _call(self, step: str, model: str, system: str, user: str, out: ReviewOutcome) -> str:
        """モデルを呼ぶ. スキーマを渡し、対応する窓口（Messages API・Bedrock）では道具の強制で構造化出力にする.
        どちらの場合も、受け取った側でスキーマを検証する（parse_document_findings / _parse_verification）."""
        schema = VERDICTS_SCHEMA if step == "verify" else FINDINGS_SCHEMA
        try:
            r = self.llm.complete(model, system, user, OUTPUT_TOKENS[step], schema=schema)
        except LLMError as e:
            raise (RetryableError if e.retryable else PermanentError)(str(e)) from e
        out.calls.append(Call(step, model, r.input_tokens, r.output_tokens, round(r.cost_usd, 4)))
        return r.text

    def review(self, name: str, markdown: str) -> ReviewOutcome:
        out = ReviewOutcome()
        if len(markdown) > MAX_DOC_CHARS:
            out.notes.append(f"文書が長いため、先頭の約 {MAX_DOC_CHARS:,} 文字だけをレビューしました（残り {len(markdown) - MAX_DOC_CHARS:,} 文字）")
            markdown = markdown[:MAX_DOC_CHARS]
        system = _prompt("design-review.md", MAX_FINDINGS)
        user = wrap_document(name, markdown)
        if _estimate(self.std, system + user, OUTPUT_TOKENS["standard"]) > self.budget:
            raise PermanentError(f"見積もりが 1 ジョブの上限 ${self.budget:.2f} を超えます（文書を分けるか RP_BUDGET_PER_JOB_USD を見直してください）")
        text = self._call("standard", self.std, system, user, out)
        try:
            found, unknown = parse_document_findings(text, self.std, "s")
        except UnreadableOutput as e:
            # 壊れた JSON・形の違う出力を「指摘 0 件」にしない. ジョブを失敗にして理由を見せる
            raise PermanentError(f"AI の出力を読めませんでした（{e}）") from e
        if unknown:
            out.notes.append(f"要確認: AI が想定外の重要度を返した指摘が {len(unknown)} 件あります（MUST として扱いました）")

        musts = [f for f in found if f.severity == "MUST"]
        if musts and self.deep:
            vsys = _prompt("design-verify.md", 3)
            vuser = user + "\n\n## 一次レビューの指摘（データ）\n\n" + wrap_findings(musts[:8])
            if out.cost_usd + _estimate(self.deep, vsys + vuser, OUTPUT_TOKENS["verify"]) > self.budget:
                out.notes.append(f"予算 ${self.budget:.2f} を超える見込みのため、上位モデルの検証を省きました")
            else:
                try:
                    vtext = self._call("verify", self.deep, vsys, vuser, out)
                except RetryableError as e:
                    out.notes.append(f"上位モデルの検証に失敗したため、一次の結果だけを出します（{e}）")
                else:
                    try:
                        verdicts, extra, extra_unknown = _parse_verification(vtext, self.deep)
                    except UnreadableOutput as e:
                        # 検証役の出力が読めないときは、一次の結果を検証なしとして出し、その旨を残す（黙って検証済みにしない）
                        out.notes.append(f"上位モデルの検証の出力を読めなかったため、一次の結果だけを出します（{e}）")
                        out.findings = ordered(found)
                        return out
                    apply_verdicts(found, _normalize_verdicts(verdicts), self.deep)
                    found = merge(found, extra)
                    if extra_unknown:
                        out.notes.append(f"要確認: 上位モデルが想定外の重要度を返した指摘が {len(extra_unknown)} 件あります（MUST として扱いました）")
                    for f in found:
                        # 検証の判定で印が消えないよう、却下されなかったものには付け直す
                        if f.id in unknown and f.status != "rejected" and "要確認" not in f.verdict_note:
                            f.verdict_note = " / ".join(x for x in (UNKNOWN_SEVERITY_NOTE.format(raw=unknown[f.id]), f.verdict_note) if x)
                    out.notes.append(f"MUST {len(musts[:8])} 件を上位モデルで検証しました")
        out.findings = ordered(found)
        return out


def _parse_verification(text: str, model: str) -> tuple[dict[str, dict], list[Finding], dict[str, str]]:
    """検証役の出力をスキーマで検証して読む. 読めなければ UnreadableOutput."""
    data = load_output(text, VERDICTS_SCHEMA)
    items = data.get("findings", [])
    extra, unknown = _with_unknown(items, findings_from_data(items, model, "d"))
    return verdicts_from_data(data["verdicts"]), extra, unknown


__all__ = [
    "DocumentReviewer",
    "Finding",
    "ReviewOutcome",
    "estimate_input_tokens",
    "neutralize",
    "normalize_severity",
    "parse_document_findings",
    "wrap_document",
    "wrap_findings",
]
