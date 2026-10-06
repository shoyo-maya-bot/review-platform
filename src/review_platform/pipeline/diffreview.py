"""コードの差分のレビュー. agent-lab の auto-review（危なさで振り分け、必要なときだけ上位モデルが検証）をそのまま使う."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from auto_review.backends import BackendError, Reply
from auto_review.diff import parse_diff
from auto_review.review import Reviewer
from auto_review.router import load_config, plan

from ..adapters.llm import LLMError, price_for
from ..ports import LLMClient
from . import PermanentError, RetryableError


class _RetryableBackendError(BackendError):
    pass


class _Backend:
    """review-platform の LLM（Messages API の窓口・Bedrock）を auto-review の呼び出し先の形に合わせる."""

    def __init__(self, llm: LLMClient):
        self.llm = llm
        self.name = llm.name

    def call(self, model: dict, system: str, user: str, max_tokens: int, role: str = "") -> Reply:  # role は auto-review が渡す（記録の再生用）
        t0 = time.monotonic()
        try:
            r = self.llm.complete(model["id"], system, user, max_tokens)
        except LLMError as e:
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


def review_diff(diff_text: str, llm: LLMClient, model_standard: str, model_deep: str, budget_usd: float, title: str = "") -> DiffOutcome:
    cfg = build_config(model_standard, model_deep, budget_usd)
    changes = parse_diff(diff_text)
    if not changes:
        raise PermanentError("差分にファイルの変更がありません")
    p = plan(changes, cfg)
    note = f"# PR: {title}\n\n" if title else ""
    try:
        result = Reviewer(cfg, _Backend(llm), repo=None).auto(note + diff_text, changes, p)
    except _RetryableBackendError as e:
        raise RetryableError(str(e)) from e
    except BackendError as e:
        raise PermanentError(str(e)) from e
    return DiffOutcome(findings=result.findings, calls=result.calls, notes=result.notes, routing=p.to_dict())
