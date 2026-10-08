"""コードの差分のレビュー. agent-lab の auto-review（危なさで振り分け、必要なときだけ上位モデルが検証）をそのまま使う."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from auto_review.backends import BackendError, Reply, price
from auto_review.diff import parse_diff
from auto_review.findings import UnreadableOutput
from auto_review.review import PROMPTS, Reviewer, build_context
from auto_review.router import load_config, plan
from auto_review.tokens import DEFAULT_CHARS_PER_TOKEN, estimate_tokens

from ..adapters.llm import LLMError, price_for
from ..ports import LLMClient
from . import PermanentError, RetryableError


class _RetryableBackendError(BackendError):
    pass


class _Backend:
    """review-platform の LLM（Messages API の窓口・Bedrock）を auto-review の呼び出し先の形に合わせる."""

    accepts_schema = True  # auto-review はスキーマを渡す（窓口が対応していれば道具の強制で構造化出力にする）

    def __init__(self, llm: LLMClient):
        self.llm = llm
        self.name = llm.name
        # auto-review は見積もりの前にこの伏せ字をかける（実際に送るのと同じ. RP_MASK_PII で決まる. None なら伏せない）
        self.masker = getattr(llm, "masker", None)

    def call(self, model: dict, system: str, user: str, max_tokens: int, role: str = "", schema: dict | None = None) -> Reply:  # role は auto-review が渡す（記録の再生用）
        t0 = time.monotonic()
        try:
            r = self.llm.complete(model["id"], system, user, max_tokens, schema=schema)
        except LLMError as e:
            # 費用（max_tokens で切れた呼び出しなど）は worker の _CostMeter が数える
            raise (_RetryableBackendError if e.retryable else BackendError)(str(e)) from e
        return Reply(r.text, r.input_tokens, r.output_tokens, r.cost_usd, time.monotonic() - t0)


@dataclass
class DiffOutcome:
    findings: list = field(default_factory=list)
    calls: list = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    routing: dict[str, Any] = field(default_factory=dict)

    @property
    def cost_usd(self) -> float:
        return round(sum(c.cost_usd for c in self.calls), 4)


def _set_model(cfg: dict, key: str, model_id: str) -> None:
    """モデル ID を差し替え、単価もそのモデルのものにする（router.yaml の単価のままだと見積もりが外れる）."""
    pin, pout = price_for(model_id)
    cfg["models"][key].update(id=model_id, input_per_mtok=pin, output_per_mtok=pout)


def build_config(model_standard: str, model_deep: str, budget_usd: float) -> dict:
    cfg = load_config()
    _set_model(cfg, "standard", model_standard)
    if model_deep:
        _set_model(cfg, "deep", model_deep)
    else:
        cfg["escalation"]["deep_score"] = 10**9  # 上位モデルを使わない
        cfg["escalation"]["verify_must"] = False
    cfg["budget"]["per_pr_usd"] = budget_usd
    return cfg


def estimate_first_call(cfg: dict, diff_text: str, changes: list, risky_files: list[str]) -> float:
    """一次レビュー（standard モデル）の見積もり（USD）. Reviewer.auto と同じ文脈（差分 + 変更ファイル）とプロンプトで数え、出力は上限いっぱいとみなす."""
    budget = cfg["budget"]
    ctx = build_context(diff_text, changes, None, cfg, risky_files, budget["context_max_tokens"])
    system = (PROMPTS / "review.md").read_text(encoding="utf-8")
    tokens = estimate_tokens(system + ctx, budget.get("chars_per_token", DEFAULT_CHARS_PER_TOKEN))
    return price(cfg["models"]["standard"], tokens, budget["output_tokens"]["standard"])


def review_diff(diff_text: str, llm: LLMClient, model_standard: str, model_deep: str, budget_usd: float, title: str = "") -> DiffOutcome:
    cfg = build_config(model_standard, model_deep, budget_usd)
    changes = parse_diff(diff_text)
    if not changes:
        raise PermanentError("差分にファイルの変更がありません")
    p = plan(changes, cfg)
    note = f"# PR: {title}\n\n" if title else ""
    if p.tier != "skip":
        # auto-review の予算の確認は上位モデルの前だけ. 一次の呼び出しも、呼ぶ前に見積もりで確かめる（予算 0 でも呼んでしまわないように）
        est = estimate_first_call(cfg, note + diff_text, changes, p.risky_files)
        if est > budget_usd:
            raise PermanentError(
                f"見積もり ${est:.2f} が使える予算 ${budget_usd:.2f}（1 ジョブの上限・日次の上限の残り）を超えるため、AI レビューを行いませんでした"
                "（差分を分けるか、RP_BUDGET_PER_JOB_USD・日次の上限を見直してください）"
            )
    try:
        result = Reviewer(cfg, _Backend(llm), repo=None).auto(note + diff_text, changes, p)
    except _RetryableBackendError as e:
        raise RetryableError(str(e)) from e
    except BackendError as e:
        raise PermanentError(str(e)) from e
    except UnreadableOutput as e:  # 壊れた JSON・スキーマに合わない出力を「指摘 0 件」にしない
        raise PermanentError(f"AI の出力を読めませんでした（{e}）") from e
    if getattr(result, "over_budget", False):
        # auto-review が予算超過の見込みでモデルを呼ばなかった. 「指摘 0 件」の成功にせず、理由を残して失敗にする
        why = " / ".join(result.notes) or "予算を超える見込みです"
        raise PermanentError(f"見積もりが使える予算 ${budget_usd:.2f}（1 ジョブの上限・日次の上限の残り）を超えるため、AI レビューを行いませんでした（{why}）")
    return DiffOutcome(findings=result.findings, calls=result.calls, notes=result.notes, routing=p.to_dict())
