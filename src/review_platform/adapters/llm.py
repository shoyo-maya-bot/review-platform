"""LLM の呼び出し. Messages API と同じ形の窓口（Anthropic・社内の中継・Microsoft Foundry など）と Amazon Bedrock.

- anthropic: devtools-common の `devtools_common.llm.LLMClient` に任せる（429・5xx・529 は `retry-after` に従って再試行、
  呼び出しごとにモデル・トークン数・所要時間・request-id・費用を 1 行ログに出す。本文は出さない）
- 料金は共通の料金表（`devtools_common.llm.PRICES` / `price_for`）で引く。Bedrock の ID（`apac.anthropic.claude-…-v1:0`、
  推論プロファイルの ARN）も同じ表で引く。表に無いモデル（Bedrock の application-inference-profile の ARN など、ID からモデルが
  分からないもの）は料金表の最大の単価（UNKNOWN_PRICE）で見積もり、モデルごとに 1 回警告を出す
- 出力が上限で切れた（stop_reason=max_tokens）ときは、指摘 0 件と区別できるよう LLMError にする
- masker を渡すと、送る前に system と user の中のメールアドレス・電話番号を伏せる（factory が RP_MASK_PII で決める）
- schema を渡すと、そのスキーマを入力にした道具（RESULT_TOOL）を 1 つだけ渡して必ず使わせる（構造化出力）. 道具の入力を JSON の文字列で返す.
  道具を使わなかった返答は、読む側（auto_review.findings.load_output）がスキーマに合わないとして失敗にする
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

# 料金表に無いモデルの見積もりに使う単価（USD / 100 万トークン）. 料金表の入力・出力それぞれの最大値（費用を少なく数えないため）.
# キャッシュの書き込み・読み出しも入力の最大値で数える
UNKNOWN_PRICE = (max(p.input for p in PRICES.values()), max(p.output for p in PRICES.values()))
_warned_unknown: set[str] = set()
RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504, 529})
RESULT_TOOL = "submit_review"


def _tool_args(schema: dict | None) -> dict[str, Any]:
    """構造化出力のための tools / tool_choice. schema が無ければ空."""
    if schema is None:
        return {}
    tool = {"name": RESULT_TOOL, "description": "レビューの結果を、この道具の入力として返す。スキーマに厳密に従う。", "input_schema": schema}
    return {"tools": [tool], "tool_choice": {"type": "tool", "name": RESULT_TOOL}}


def _result_text(content: list[dict], schema: dict | None) -> str:
    """返答の本文. schema があれば道具の入力を JSON の文字列にする（道具を使っていなければ文章をそのまま返し、読む側で失敗にする）."""
    if schema is not None:
        for c in content:
            if c.get("type") == "tool_use" and c.get("name") == RESULT_TOOL:
                return json.dumps(c.get("input"), ensure_ascii=False)
    return "".join(c.get("text", "") for c in content if c.get("type") == "text")


class LLMError(RuntimeError):
    """LLM の呼び出しの失敗. cost_usd は、失敗しても窓口が課金した分（出力が max_tokens で切れたときなど）. 費用の集計に足す.

    maybe_billed は、課金されたかもしれないが額が分からない失敗（5xx・タイムアウト・接続の切断など. 窓口が処理した後に
    失敗したかもしれない）. 費用を少なく数えないよう、ワーカーは呼ぶ前の見積もり額（出力は上限いっぱい）で数える.
    送る前の失敗・窓口が処理せずに断った失敗（400・401・403・404・413・429 など）は False.
    """

    def __init__(self, message: str, retryable: bool = False, cost_usd: float = 0.0, maybe_billed: bool = False):
        super().__init__(message)
        self.retryable = retryable
        self.cost_usd = cost_usd
        self.maybe_billed = maybe_billed


def status_maybe_billed(status: int) -> bool:
    """HTTP の状態から、課金されたかもしれないか. 4xx（408 を除く）は窓口が処理せずに断ったものとみなす. それ以外は安全側に True."""
    return not (400 <= status < 500 and status != 408)


# Bedrock のエラーのうち、モデルが処理せずに断ったもの（課金されない）. ほかは安全側に「課金されたかもしれない」とみなす
BEDROCK_NOT_BILLED = frozenset(
    {
        "ThrottlingException",
        "ValidationException",
        "AccessDeniedException",
        "ResourceNotFoundException",
        "ServiceQuotaExceededException",
        "ModelNotReadyException",
        "UnrecognizedClientException",
        "ExpiredTokenException",
    }
)


def canonical_model(model: str) -> str:
    """料金表を引くためのモデル ID. Bedrock の地域の接頭辞（`apac.anthropic.`）や ARN を外す."""
    m = model.strip().lower().rsplit("/", 1)[-1]
    if "anthropic." in m:
        m = m.split("anthropic.", 1)[1]
    return m


def shared_price(model: str) -> Price | None:
    p = dc.price_for(canonical_model(model))
    if p is None and model not in _warned_unknown:
        _warned_unknown.add(model)
        log.warning(
            "料金表に無いモデルです. 費用は料金表の最大の単価（入力 $%s・出力 $%s / 100 万トークン）で見積もります: %s",
            UNKNOWN_PRICE[0],
            UNKNOWN_PRICE[1],
            model,
        )
    return p


def price_for(model: str) -> tuple[float, float]:
    """(入力, 出力) の単価（USD / 100 万トークン）. 共通の料金表に無いモデルは UNKNOWN_PRICE."""
    p = shared_price(model)
    return (p.input, p.output) if p else UNKNOWN_PRICE


def cost(model: str, input_tokens: int, output_tokens: int, cache_write_tokens: int = 0, cache_read_tokens: int = 0) -> float:
    """費用の見積もり（USD）. input_tokens はキャッシュを除いた入力. 料金表に無いモデルは料金表の最大の単価で見積もる."""
    p = shared_price(model)
    if p is None:
        pin, pout = UNKNOWN_PRICE
        return ((input_tokens + cache_write_tokens + cache_read_tokens) * pin + output_tokens * pout) / 1_000_000
    return (input_tokens * p.input + output_tokens * p.output + cache_write_tokens * p.cache_write + cache_read_tokens * p.cache_read) / 1_000_000


def _check_stop(stop_reason: str, cost_usd: float = 0.0) -> None:
    if stop_reason == "max_tokens":
        # 切れた呼び出しも課金される. 費用を持たせて、ジョブ・日次の集計から漏らさない
        raise LLMError("出力が上限で切れました（max_tokens）. 結果は使えません", retryable=False, cost_usd=cost_usd)


def _usage_cost(model: str, usage: Any, reported: float | None = None) -> float:
    """usage（devtools_common.llm.Usage）からの費用. 窓口・共通の料金表が出した値があればそれを使う."""
    if reported is not None:
        return reported
    return cost(model, usage.input_tokens, usage.output_tokens, usage.cache_creation_input_tokens, usage.cache_read_input_tokens)


def _mask(masker: Masker | None, system: str, user: str) -> tuple[str, str]:
    return (masker.text(system), masker.text(user)) if masker else (system, user)


class NoLLM:
    name = "none"

    def complete(self, model: str, system: str, user: str, max_tokens: int, schema: dict | None = None) -> LLMReply:
        raise LLMError("RP_LLM=none のため、AI レビューは行いません")


class FakeLLM:
    """試験用. 決まった返事を返す（`replies` を順に、無くなったら最後の 1 つを繰り返す）. masker を渡すと、受け取る前に伏せる（本番と同じ）."""

    name = "fake"

    def __init__(self, replies: list[str] | None = None, masker: Masker | None = None):
        self.replies = list(replies or ['```json\n{"findings": []}\n```'])
        self.masker = masker
        self.calls: list[dict] = []

    def complete(self, model: str, system: str, user: str, max_tokens: int, schema: dict | None = None) -> LLMReply:
        system, user = _mask(self.masker, system, user)
        self.calls.append({"model": model, "system": system, "user": user, "max_tokens": max_tokens, "schema": schema})
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

    def complete(self, model: str, system: str, user: str, max_tokens: int, schema: dict | None = None) -> LLMReply:
        try:
            comp = self.client(model).complete(user, system=system, max_tokens=max_tokens, **_tool_args(schema))
        except dc.MaxTokensError as e:
            c = e.completion
            raise LLMError(
                f"出力が上限で切れました（max_tokens）. 結果は使えません（request-id {c.request_id}）",
                retryable=False,
                cost_usd=_usage_cost(model, c.usage, c.cost_usd),
            ) from e
        except dc.APIStatusError as e:
            raise LLMError(
                f"LLM の API が {e.status} を返しました: {_safe(str(e))}",
                retryable=e.status in RETRYABLE_STATUS,
                maybe_billed=status_maybe_billed(e.status),
            ) from e
        except dc.APIConnectionError as e:  # タイムアウト・切断は、窓口が処理を終えた後かもしれない
            raise LLMError(f"LLM の API に繰り返し失敗しました: {_safe(str(e))}", retryable=True, maybe_billed=True) from e
        except dc.LLMError as e:
            raise LLMError(f"LLM の呼び出しに失敗しました: {_safe(str(e))}", maybe_billed=True) from e
        u = comp.usage
        tin = u.input_tokens + u.cache_read_input_tokens + u.cache_creation_input_tokens
        price = _usage_cost(model, u, comp.cost_usd)
        text = _result_text(list(comp.content), schema)
        return LLMReply(text=text, input_tokens=tin, output_tokens=u.output_tokens, cost_usd=price, stop_reason=str(comp.stop_reason or ""), request_id=comp.request_id or "")


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

    def complete(self, model: str, system: str, user: str, max_tokens: int, schema: dict | None = None) -> LLMReply:
        from botocore.exceptions import ClientError

        system, user = _mask(self.masker, system, user)
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            **_tool_args(schema),
        }
        t0 = time.monotonic()
        try:
            res = self.client.invoke_model(modelId=model, body=json.dumps(body), contentType="application/json", accept="application/json")
        except ClientError as e:
            code = str(e.response.get("Error", {}).get("Code", ""))
            raise LLMError(
                f"Bedrock の呼び出しに失敗しました: {code}",
                retryable=code in ("ThrottlingException", "ServiceUnavailableException", "ModelNotReadyException"),
                maybe_billed=code not in BEDROCK_NOT_BILLED,
            ) from e
        request_id = str((res.get("ResponseMetadata") or {}).get("RequestId") or "")
        return _reply(model, json.loads(res["body"].read()), request_id, int((time.monotonic() - t0) * 1000), schema)


def _reply(model: str, data: dict, request_id: str = "", duration_ms: int = 0, schema: dict | None = None) -> LLMReply:
    text = _result_text(list(data.get("content", [])), schema)
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
    _check_stop(stop, price)
    return LLMReply(text=text, input_tokens=tin, output_tokens=u.output_tokens, cost_usd=price, stop_reason=stop, request_id=request_id)


def _safe(text: str) -> str:
    """エラーの本文をログに出すとき、キーらしき文字列を伏せて短くする."""
    return re.sub(r"(sk-[A-Za-z0-9_-]{6})[A-Za-z0-9_-]+", r"\1…", text)[:300]
