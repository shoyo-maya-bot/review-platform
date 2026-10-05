"""LLM の呼び出し. Messages API と同じ形の窓口（Anthropic・社内の中継・Microsoft Foundry など）と Amazon Bedrock.

出力が上限で切れた（stop_reason=max_tokens）ときは、指摘 0 件と区別できるよう LLMError にする.
"""

from __future__ import annotations

import json
import re
import time

import httpx

from ..ports import LLMReply

# 単価（USD / 100 万トークン）. モデル ID の一部で引く. 2026-10-02 時点の公開価格. 知らないモデルは高い側で見積もる
PRICES: dict[str, tuple[float, float]] = {
    "haiku": (1.0, 5.0),
    "sonnet": (2.0, 10.0),
    "opus": (4.0, 20.0),
}
UNKNOWN_PRICE = (4.0, 20.0)


class LLMError(RuntimeError):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


def price_for(model: str) -> tuple[float, float]:
    m = model.lower()
    for k, v in PRICES.items():
        if k in m:
            return v
    return UNKNOWN_PRICE


def cost(model: str, input_tokens: int, output_tokens: int) -> float:
    pin, pout = price_for(model)
    return (input_tokens * pin + output_tokens * pout) / 1_000_000


def _check_stop(stop_reason: str) -> None:
    if stop_reason == "max_tokens":
        raise LLMError("出力が上限で切れました（max_tokens）. 結果は使えません", retryable=False)


class NoLLM:
    name = "none"

    def complete(self, model: str, system: str, user: str, max_tokens: int) -> LLMReply:
        raise LLMError("RP_LLM=none のため、AI レビューは行いません")


class FakeLLM:
    """試験用. 決まった返事を返す（`replies` を順に、無くなったら最後の 1 つを繰り返す）."""

    name = "fake"

    def __init__(self, replies: list[str] | None = None):
        self.replies = list(replies or ['```json\n{"findings": []}\n```'])
        self.calls: list[dict] = []

    def complete(self, model: str, system: str, user: str, max_tokens: int) -> LLMReply:
        self.calls.append({"model": model, "system": system, "user": user, "max_tokens": max_tokens})
        text = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        tin, tout = len(system + user) // 3, len(text) // 3
        return LLMReply(text=text, input_tokens=tin, output_tokens=tout, cost_usd=cost(model, tin, tout))


class AnthropicLLM:
    """POST {base_url}/v1/messages（x-api-key）. 429・5xx・接続の失敗は指数的に待って 3 回まで再試行（同じ入力なので重複の害は費用だけ）."""

    name = "anthropic"

    def __init__(self, api_key: str, base_url: str = "https://api.anthropic.com", timeout: float = 600, client: httpx.Client | None = None):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.http = client or httpx.Client(timeout=timeout)

    def complete(self, model: str, system: str, user: str, max_tokens: int) -> LLMReply:
        body = {"model": model, "max_tokens": max_tokens, "system": system, "messages": [{"role": "user", "content": user}]}
        headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        last: Exception | None = None
        for attempt in range(4):
            try:
                res = self.http.post(f"{self.base_url}/v1/messages", headers=headers, json=body)
            except httpx.TransportError as e:
                last = e
            else:
                if res.status_code == 200:
                    data = res.json()
                    return _reply(model, data)
                if res.status_code not in (408, 409, 429, 500, 502, 503, 504, 529):
                    raise LLMError(f"LLM の API が {res.status_code} を返しました: {_safe(res.text)}")
                last = LLMError(f"LLM の API が {res.status_code} を返しました", retryable=True)
            time.sleep(min(30, 2**attempt))
        raise LLMError(f"LLM の API に繰り返し失敗しました: {last}", retryable=True)


class BedrockLLM:
    """Amazon Bedrock の Claude（InvokeModel、Messages の形）. model には推論プロファイル ID かモデル ID を渡す."""

    name = "bedrock"

    def __init__(self, region: str, endpoint_url: str = ""):
        import boto3
        from botocore.config import Config

        kwargs = {"region_name": region or None, "config": Config(retries={"max_attempts": 5, "mode": "adaptive"}, read_timeout=600)}
        if endpoint_url:
            kwargs["endpoint_url"] = endpoint_url
        self.client = boto3.client("bedrock-runtime", **kwargs)

    def complete(self, model: str, system: str, user: str, max_tokens: int) -> LLMReply:
        from botocore.exceptions import ClientError

        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        try:
            res = self.client.invoke_model(modelId=model, body=json.dumps(body), contentType="application/json", accept="application/json")
        except ClientError as e:
            code = str(e.response.get("Error", {}).get("Code", ""))
            raise LLMError(f"Bedrock の呼び出しに失敗しました: {code}", retryable=code in ("ThrottlingException", "ServiceUnavailableException", "ModelNotReadyException")) from e
        return _reply(model, json.loads(res["body"].read()))


def _reply(model: str, data: dict) -> LLMReply:
    text = "".join(c.get("text", "") for c in data.get("content", []) if c.get("type") == "text")
    u = data.get("usage", {})
    tin = int(u.get("input_tokens", 0)) + int(u.get("cache_read_input_tokens", 0) or 0) + int(u.get("cache_creation_input_tokens", 0) or 0)
    tout = int(u.get("output_tokens", 0))
    stop = str(data.get("stop_reason", ""))
    _check_stop(stop)
    return LLMReply(text=text, input_tokens=tin, output_tokens=tout, cost_usd=cost(model, tin, tout), stop_reason=stop)


def _safe(text: str) -> str:
    """エラーの本文をログに出すとき、キーらしき文字列を伏せて短くする."""
    return re.sub(r"(sk-[A-Za-z0-9_-]{6})[A-Za-z0-9_-]+", r"\1…", text)[:300]
