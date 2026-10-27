"""第 7 回の指摘: 課金されたかもしれない失敗（5xx・タイムアウトなど）で、日次の予約を 0 に戻さない（費用を少なく数えない）."""

from __future__ import annotations

from dataclasses import replace

import pytest
from test_cli_llm_notify import _anthropic, _err, _Server

from conftest import FakeRunner
from review_platform.adapters.llm import FakeLLM, LLMError, status_maybe_billed
from review_platform.adapters.notify import NoNotifier
from review_platform.domain import JobStatus
from review_platform.worker import Worker, _CostMeter, _today

DOC = "# 設計\n本文".encode()
LIMITS = {"budget_per_job_usd": 5.0, "budget_daily_usd": 10.0, "budget_daily_per_owner_usd": 10.0}


class _Failing(FakeLLM):
    """呼ばれると決まった例外を出す. name を変えて有料の LLM に見せられる."""

    def __init__(self, exc: Exception, name: str = "fake"):
        super().__init__()
        self.exc, self.name = exc, name

    def complete(self, model, system, user, max_tokens, schema=None):
        self.calls.append({})
        raise self.exc


def _run(settings, svc, llm, **limits):
    s = replace(settings, **limits) if limits else settings
    job = svc.submit("alice", "a.md", DOC)
    Worker(s, svc.blobs, svc.queue, svc.repo, llm, NoNotifier(), FakeRunner()).run_once(wait_seconds=1)
    return svc.repo.get(job.job_id)


@pytest.fixture
def fixed_estimate(monkeypatch):
    monkeypatch.setattr(_CostMeter, "estimate", lambda self, *a: 0.25)
    return 0.25


def test_maybe_billed_failure_keeps_the_reservation(settings, svc, fixed_estimate):
    done = _run(settings, svc, _Failing(LLMError("LLM の API が 500 を返しました", maybe_billed=True)), **LIMITS)
    assert done.status == JobStatus.FAILED
    # 以前は予約を 0 に戻していた（課金されていても日次の集計に残らない）
    assert svc.repo.spent(_today(), "alice") == pytest.approx((fixed_estimate, fixed_estimate))
    assert done.cost_usd == pytest.approx(fixed_estimate)


def test_retryable_maybe_billed_failure_is_counted_before_the_retry(settings, svc, fixed_estimate):
    """やり直す失敗でも、その試行の見積もり額をジョブと日次の集計に残す（次の試行の予算から引かれる）."""
    done = _run(settings, svc, _Failing(LLMError("timeout", retryable=True, maybe_billed=True)), **LIMITS)
    assert done.status == JobStatus.QUEUED
    assert done.cost_usd == pytest.approx(fixed_estimate)
    assert svc.repo.spent(_today(), "alice")[0] == pytest.approx(fixed_estimate)


def test_not_billed_failure_releases_the_reservation(settings, svc, fixed_estimate):
    """窓口が処理せずに断った失敗（400 など）は、従来どおり予約を戻す."""
    done = _run(settings, svc, _Failing(LLMError("LLM の API が 400 を返しました", maybe_billed=False)), **LIMITS)
    assert done.status == JobStatus.FAILED
    assert svc.repo.spent(_today(), "alice") == pytest.approx((0.0, 0.0))


def test_known_billed_cost_larger_than_estimate_is_kept(settings, svc, fixed_estimate):
    done = _run(settings, svc, _Failing(LLMError("max_tokens", cost_usd=0.4, maybe_billed=True)), **LIMITS)
    assert svc.repo.spent(_today(), "alice") == pytest.approx((0.4, 0.4))
    assert done.cost_usd == pytest.approx(0.4)


def test_unexpected_exception_from_paid_llm_is_counted(settings, svc, fixed_estimate):
    """有料の LLM の想定外の例外（botocore の読み取りのタイムアウトなど）も、安全側に見積もり額で数える."""
    _run(settings, svc, _Failing(TimeoutError("read timed out"), name="bedrock"), **LIMITS)
    assert svc.repo.spent(_today(), "alice")[0] == pytest.approx(fixed_estimate)


def test_unexpected_exception_from_free_llm_is_not_counted(settings, svc, fixed_estimate):
    _run(settings, svc, _Failing(TimeoutError("read timed out")), **LIMITS)
    assert svc.repo.spent(_today(), "alice")[0] == pytest.approx(0.0)


@pytest.mark.parametrize(
    ("status", "billed"),
    [(400, False), (401, False), (403, False), (404, False), (413, False), (429, False), (408, True), (500, True), (503, True), (529, True)],
)
def test_status_maybe_billed(status, billed):
    assert status_maybe_billed(status) is billed


def test_anthropic_adapter_marks_maybe_billed():
    with pytest.raises(LLMError) as e:
        _anthropic(_Server(_err(503))).complete("m", "s", "u", 1)
    assert e.value.maybe_billed
    with pytest.raises(LLMError) as e:
        _anthropic(_Server(_err(400, text="bad"))).complete("m", "s", "u", 1)
    assert not e.value.maybe_billed


@pytest.mark.parametrize(("code", "billed"), [("ThrottlingException", False), ("ValidationException", False), ("ModelTimeoutException", True), ("InternalServerException", True)])
def test_bedrock_adapter_marks_maybe_billed(monkeypatch, code, billed):
    from botocore.exceptions import ClientError

    from review_platform.adapters.llm import BedrockLLM

    llm = BedrockLLM("ap-northeast-1")

    def fail(**kw):
        raise ClientError({"Error": {"Code": code, "Message": "x"}}, "InvokeModel")

    monkeypatch.setattr(llm.client, "invoke_model", fail)
    with pytest.raises(LLMError) as e:
        llm.complete("m", "s", "u", 1)
    assert e.value.maybe_billed is billed
