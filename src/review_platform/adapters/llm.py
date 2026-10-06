"""LLM の呼び出し. Messages API と同じ形の窓口（Anthropic・社内の中継・Microsoft Foundry など）と Amazon Bedrock.

- anthropic: devtools-common の `devtools_common.llm.LLMClient` に任せる（429・5xx・529 は `retry-after` に従って再試行、
  呼び出しごとにモデル・トークン数・所要時間・request-id・費用を 1 行ログに出す。本文は出さない）
- 料金は共通の料金表（`devtools_common.llm.PRICES` / `price_for`）で引く。Bedrock の ID（`apac.anthropic.claude-…-v1:0`、
  推論プロファイルの ARN）も同じ表で引く。表に無いモデルは高い側（UNKNOWN_PRICE）で見積もる
- 出力が上限で切れた（stop_reason=max_tokens）ときは、指摘 0 件と区別できるよう LLMError にする
- masker を渡すと、送る前に system と user の中のメールアドレス・電話番号を伏せる（factory が RP_MASK_PII で決める）
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from typing import Any

from devtools_common import llm as dc
from devtools_common.llm import PRICES, Masker, Price

from ..ports import LLMReply

log = logging.getLogger(__name__)

__all__ = ["PRICES", "UNKNOWN_PRICE", "AnthropicLLM", "BedrockLLM", "FakeLLM", "LLMError", "Masker", "NoLLM", "Price", "cost", "price_for"]

# 料金表に無いモデルの見積もりに使う単価（USD / 100 万トークン）. 現行の最上位モデル（claude-opus-5-5）と同じ高い側で見積もる
UNKNOWN_PRICE = (4.0, 20.0)
RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504, 529})


class LLMError(RuntimeError):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


def canonical_model(model: str) -> str:
    """料金表を引くためのモデル ID. Bedrock の地域の接頭辞（`apac.anthropic.`）や ARN を外す."""
    m = model.strip().lower().rsplit("/", 1)[-1]
    if "anthropic." in m:
        m = m.split("anthropic.", 1)[1]
    return m


def shared_price(model: str) -> Price | None:
    return dc.price_for(canonical_model(model))


def price_for(model: str) -> tuple[float, float]:
    """(入力, 出力) の単価（USD / 100 万トークン）. 共通の料金表に無いモデルは UNKNOWN_PRICE."""
    p = shared_price(model)
    return (p.input, p.output) if p else UNKNOWN_PRICE


def cost(model: str, input_tokens: int, output_tokens: int, cache_write_tokens: int = 0, cache_read_tokens: int = 0) -> float:
    """費用の見積もり（USD）. input_tokens はキャッシュを除いた入力. 料金表に無いモデルは高い側の単価で見積もる."""
    p = shared_price(model)
    if p is None:
        pin, pout = UNKNOWN_PRICE
        return ((input_tokens + cache_write_tokens + cache_read_tokens) * pin + output_tokens * pout) / 1_000_000
    return (input_tokens * p.input + output_tokens * p.output + cache_write_tokens * p.cache_write + cache_read_tokens * p.cache_read) / 1_000_000


def _check_stop(stop_reason: str) -> None:
    if stop_reason == "max_tokens":
        raise LLMError("出力が上限で切れました（max_tokens）. 結果は使えません", retryable=False)


def _mask(masker: Masker | None, system: str, user: str) -> tuple[str, str]:
    return (masker.text(system), masker.text(user)) if masker else (system, user)


class NoLLM:
    name = "none"

    def complete(self, model: str, system: str, user: str, max_tokens: int) -> LLMReply:
        raise LLMError("RP_LLM=none のため、AI レビューは行いません")


class FakeLLM:
    """試験用. 決まった返事を返す（`replies` を順に、無くなったら最後の 1 つを繰り返す）. masker を渡すと、受け取る前に伏せる（本番と同じ）."""

    name = "fake"

    def __init__(self, replies: list[str] | None = None, masker: Masker | None = None):
        self.replies = list(replies or ['```json\n{"findings": []}\n```'])
        self.masker = masker
        self.calls: list[dict] = []

    def complete(self, model: str, system: str, user: str, max_tokens: int) -> LLMReply:
        system, user = _mask(self.masker, system, user)
        self.calls.append({"model": model, "system": system, "user": user, "max_tokens": max_tokens})
        text = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        tin, tout = len(system + user) // 3, len(text) // 3
        return LLMReply(text=text, input_tokens=tin, output_tokens=tout, cost_usd=cost(model, tin, tout))


class AnthropicLLM:
    """POST {base_url}/v1/messages. 中身は devtools_common.llm.LLMClient（モデルごとに 1 つ作って使い回す）.

    429・5xx・529・接続の失敗は LLMClient が `retry-after`（無ければ指数バックオフ）に従って max_retries 回まで再試行する
    （同じ入力なので重複の害は費用だけ）。それでも失敗したら retryable=True の LLMError にし、ジョブのやり直しに回す。
    opener・sleep は試験用（通信と待ち時間を差し替える）。
    """

    name = "anthropic"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.anthropic.com",
        timeout: float = 600,
        *,
        max_retries: int = 3,
        masker: Masker | None = None,
        opener: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] | None = None,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout, self.max_retries, self.masker = timeout, max_retries, masker
        self._extra: dict[str, Any] = {k: v for k, v in (("opener", opener), ("sleep", sleep)) if v is not None}
        self._clients: dict[str, dc.LLMClient] = {}

    def client(self, model: str) -> dc.LLMClient:
        if model not in self._clients:
            self._clients[model] = dc.LLMClient(
                model,
                api_key=self.api_key,
                auth_token="",
                base_url=self.base_url,
                timeout=self.timeout,
                max_retries=self.max_retries,
                masker=self.masker,
                **self._extra,
            )
        return self._clients[model]

    def complete(self, model: str, system: str, user: str, max_tokens: int) -> LLMReply:
        try:
            comp = self.client(model).complete(user, system=system, max_tokens=max_tokens)
        except dc.MaxTokensError as e:
            raise LLMError(f"出力が上限で切れました（max_tokens）. 結果は使えません（request-id {e.completion.request_id}）", retryable=False) from e
        except dc.APIStatusError as e:
            raise LLMError(f"LLM の API が {e.status} を返しました: {_safe(str(e))}", retryable=e.status in RETRYABLE_STATUS) from e
        except dc.APIConnectionError as e:
            raise LLMError(f"LLM の API に繰り返し失敗しました: {_safe(str(e))}", retryable=True) from e
        except dc.LLMError as e:
            raise LLMError(f"LLM の呼び出しに失敗しました: {_safe(str(e))}") from e
        u = comp.usage
        tin = u.input_tokens + u.cache_read_input_tokens + u.cache_creation_input_tokens
        price = comp.cost_usd if comp.cost_usd is not None else cost(model, u.input_tokens, u.output_tokens, u.cache_creation_input_tokens, u.cache_read_input_tokens)
        return LLMReply(text=comp.text, input_tokens=tin, output_tokens=u.output_tokens, cost_usd=price, stop_reason=str(comp.stop_reason or ""), request_id=comp.request_id or "")


class BedrockLLM:
    """Amazon Bedrock の Claude（InvokeModel、Messages の形）. model には推論プロファイル ID かモデル ID を渡す."""

    name = "bedrock"

    def __init__(self, region: str, endpoint_url: str = "", masker: Masker | None = None):
        import boto3
        from botocore.config import Config

        kwargs = {"region_name": region or None, "config": Config(retries={"max_attempts": 5, "mode": "adaptive"}, read_timeout=600)}
        if endpoint_url:
            kwargs["endpoint_url"] = endpoint_url
        self.client = boto3.client("bedrock-runtime", **kwargs)
        self.masker = masker

    def complete(self, model: str, system: str, user: str, max_tokens: int) -> LLMReply:
        from botocore.exceptions import ClientError

        system, user = _mask(self.masker, system, user)
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        t0 = time.monotonic()
        try:
            res = self.client.invoke_model(modelId=model, body=json.dumps(body), contentType="application/json", accept="application/json")
        except ClientError as e:
            code = str(e.response.get("Error", {}).get("Code", ""))
            raise LLMError(f"Bedrock の呼び出しに失敗しました: {code}", retryable=code in ("ThrottlingException", "ServiceUnavailableException", "ModelNotReadyException")) from e
        request_id = str((res.get("ResponseMetadata") or {}).get("RequestId") or "")
        return _reply(model, json.loads(res["body"].read()), request_id, int((time.monotonic() - t0) * 1000))


def _reply(model: str, data: dict, request_id: str = "", duration_ms: int = 0) -> LLMReply:
    text = "".join(c.get("text", "") for c in data.get("content", []) if c.get("type") == "text")
    u = dc.Usage.from_api(data.get("usage"))
    tin = u.input_tokens + u.cache_read_input_tokens + u.cache_creation_input_tokens
    stop = str(data.get("stop_reason", ""))
    price = cost(model, u.input_tokens, u.output_tokens, u.cache_creation_input_tokens, u.cache_read_input_tokens)
    log.info(
        "llm call",
        extra={
            "llm": {
                "model": model,
                "input_tokens": u.input_tokens,
                "output_tokens": u.output_tokens,
                "cache_creation_input_tokens": u.cache_creation_input_tokens,
                "cache_read_input_tokens": u.cache_read_input_tokens,
                "duration_ms": duration_ms,
                "request_id": request_id or None,
                "cost_usd": price,
                "stop_reason": stop,
            }
        },
    )
    _check_stop(stop)
    return LLMReply(text=text, input_tokens=tin, output_tokens=u.output_tokens, cost_usd=price, stop_reason=stop, request_id=request_id)


def _safe(text: str) -> str:
    """エラーの本文をログに出すとき、キーらしき文字列を伏せて短くする."""
    return re.sub(r"(sk-[A-Za-z0-9_-]{6})[A-Za-z0-9_-]+", r"\1…", text)[:300]
