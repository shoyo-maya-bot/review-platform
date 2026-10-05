"""設計書（Markdown）の AI レビュー. ふだんは standard モデル 1 回. MUST が出たら deep モデルがその指摘だけを検証する."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from importlib import resources

from auto_review.findings import Finding, apply_verdicts, merge, ordered, parse_findings, parse_verdicts

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


def wrap_document(name: str, markdown: str) -> str:
    """信用しないデータとして区切る. 区切りの閉じタグが本文にあれば無害化する."""
    safe_name = name.replace('"', "'").replace("<", "‹").replace(">", "›")
    body = markdown.replace("</document>", "<\\/document>")
    return f'<document name="{safe_name}">\n{body}\n</document>'


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
        found = parse_findings(text, self.std, "s")
        if not found and "findings" not in text:
            raise PermanentError("AI の出力を読めませんでした（JSON がありません）")

        musts = [f for f in found if f.severity == "MUST"]
        if musts and self.deep:
            vsys = _prompt("design-verify.md", 3)
            listing = "\n".join(f"- id={f.id} [{f.severity}] {f.file}: {f.title} — {f.detail}" for f in musts[:8])
            vuser = user + "\n\n## 一次レビューの指摘（データ）\n\n" + listing
            if out.cost_usd + _estimate(self.deep, len(vsys) + len(vuser), OUTPUT_TOKENS["verify"]) > self.budget:
                out.notes.append(f"予算 ${self.budget:.2f} を超える見込みのため、上位モデルの検証を省きました")
            else:
                try:
                    vtext = self._call("verify", self.deep, vsys, vuser, out)
                except RetryableError as e:
                    out.notes.append(f"上位モデルの検証に失敗したため、一次の結果だけを出します（{e}）")
                else:
                    apply_verdicts(found, parse_verdicts(vtext), self.deep)
                    found = merge(found, parse_findings(vtext, self.deep, "d"))
                    out.notes.append(f"MUST {len(musts[:8])} 件を上位モデルで検証しました")
        out.findings = ordered(found)
        return out


__all__ = ["DocumentReviewer", "Finding", "ReviewOutcome", "wrap_document"]
