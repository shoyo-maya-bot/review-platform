"""設計書（Markdown）の AI レビュー. ふだんは standard モデル 1 回. MUST が出たら deep モデルがその指摘だけを検証する."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from importlib import resources

from auto_review.findings import Finding, _json_blocks, apply_verdicts, merge, ordered, parse_findings, parse_verdicts

from ..adapters.llm import LLMError, cost
from ..ports import LLMClient
from . import PermanentError, RetryableError

log = logging.getLogger(__name__)

CHARS_PER_TOKEN = 3.0
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


# モデルが決まった 4 種以外の重要度を返したときの読み替え. 知らない値は下げずに MUST として扱い、要確認の印を付ける
SEVERITY_SYNONYMS = {
    "MUST": "MUST", "CRITICAL": "MUST", "BLOCKER": "MUST", "BLOCKING": "MUST", "HIGH": "MUST", "MAJOR": "MUST",
    "SEVERE": "MUST", "ERROR": "MUST", "必須": "MUST", "重大": "MUST", "高": "MUST",
    "WANT": "WANT", "SHOULD": "WANT", "MEDIUM": "WANT", "MODERATE": "WANT", "WARNING": "WANT", "WARN": "WANT",
    "推奨": "WANT", "中": "WANT",
    "NITS": "NITS", "NIT": "NITS", "NITPICK": "NITS", "MINOR": "NITS", "LOW": "NITS", "TRIVIAL": "NITS",
    "INFO": "NITS", "STYLE": "NITS", "軽微": "NITS", "低": "NITS",
    "Q": "Q", "QUESTION": "Q", "QUERY": "Q", "質問": "Q", "確認": "Q",
}  # fmt: skip
UNKNOWN_SEVERITY_NOTE = "要確認: AI が想定外の重要度「{raw}」を返したため、MUST として扱いました"


def normalize_severity(raw: object) -> str | None:
    """重要度を MUST / WANT / NITS / Q に読み替える. 読めない値は None（呼び出し側で要確認にする）."""
    key = str(raw or "").strip().upper()
    return SEVERITY_SYNONYMS.get(key)


def _raw_items(text: str) -> list[dict]:
    """parse_findings と同じ順で、モデルが返した指摘の生の dict を取り出す（id の番号と対応する）."""
    items: list[dict] = []
    for data in _json_blocks(text):
        if isinstance(data, dict):
            data = data.get("findings", [])
        if isinstance(data, list):
            items += [x for x in data if isinstance(x, dict)]
    return items


def parse_document_findings(text: str, model: str, prefix: str) -> tuple[list[Finding], dict[str, str]]:
    """指摘を読み、重要度を読み替える. auto-review の既定は知らない重要度を黙って WANT に下げるので、ここで上書きする."""
    found = parse_findings(text, model, prefix)
    raw = _raw_items(text)
    unknown: dict[str, str] = {}
    for f in found:
        i = int(f.id[len(prefix) :]) - 1
        value = raw[i].get("severity") if 0 <= i < len(raw) else None
        sev = normalize_severity(value)
        if sev is None:
            shown = str(value or "（空）").strip() or "（空）"
            f.severity = "MUST"
            f.status = "uncertain"
            f.verdict_note = UNKNOWN_SEVERITY_NOTE.format(raw=shown)
            unknown[f.id] = shown
        else:
            f.severity = sev
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


def _estimate(model: str, chars: int, out_tokens: int) -> float:
    return cost(model, int(chars / CHARS_PER_TOKEN) + 1, out_tokens)


class DocumentReviewer:
    def __init__(self, llm: LLMClient, model_standard: str, model_deep: str, budget_usd: float):
        self.llm = llm
        self.std = model_standard
        self.deep = model_deep
        self.budget = budget_usd

    def _call(self, step: str, model: str, system: str, user: str, out: ReviewOutcome) -> str:
        try:
            r = self.llm.complete(model, system, user, OUTPUT_TOKENS[step])
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
        if _estimate(self.std, len(system) + len(user), OUTPUT_TOKENS["standard"]) > self.budget:
            raise PermanentError(f"見積もりが 1 ジョブの上限 ${self.budget:.2f} を超えます（文書を分けるか RP_BUDGET_PER_JOB_USD を見直してください）")
        text = self._call("standard", self.std, system, user, out)
        found, unknown = parse_document_findings(text, self.std, "s")
        if not found and "findings" not in text:
            raise PermanentError("AI の出力を読めませんでした（JSON がありません）")
        if unknown:
            out.notes.append(f"要確認: AI が想定外の重要度を返した指摘が {len(unknown)} 件あります（MUST として扱いました）")

        musts = [f for f in found if f.severity == "MUST"]
        if musts and self.deep:
            vsys = _prompt("design-verify.md", 3)
            vuser = user + "\n\n## 一次レビューの指摘（データ）\n\n" + wrap_findings(musts[:8])
            if out.cost_usd + _estimate(self.deep, len(vsys) + len(vuser), OUTPUT_TOKENS["verify"]) > self.budget:
                out.notes.append(f"予算 ${self.budget:.2f} を超える見込みのため、上位モデルの検証を省きました")
            else:
                try:
                    vtext = self._call("verify", self.deep, vsys, vuser, out)
                except RetryableError as e:
                    out.notes.append(f"上位モデルの検証に失敗したため、一次の結果だけを出します（{e}）")
                else:
                    apply_verdicts(found, _normalize_verdicts(parse_verdicts(vtext)), self.deep)
                    extra, extra_unknown = parse_document_findings(vtext, self.deep, "d")
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


__all__ = ["DocumentReviewer", "Finding", "ReviewOutcome", "neutralize", "normalize_severity", "parse_document_findings", "wrap_document", "wrap_findings"]
