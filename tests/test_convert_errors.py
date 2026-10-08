"""doc2md の終了コードの扱い. 2（壊れた入力・使い方の誤り）は再試行しない失敗にし、stderr の要点（伏せ字・一時フォルダのパスを外した後）をジョブのエラーに残す.
3（実行時エラー）は再試行し、その理由もジョブのエラーに残す."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import FakeRunner, xlsx_bytes
from review_platform.adapters.llm import FakeLLM
from review_platform.adapters.notify import NoNotifier
from review_platform.domain import JobStatus
from review_platform.pipeline import PermanentError, RetryableError
from review_platform.pipeline.convert import stderr_summary, to_markdown
from review_platform.worker import Worker


class StderrRunner(FakeRunner):
    """doc2md の代わり. devtools-common の run_main と同じ形（`error: <理由>`）で stderr に書き、決まった終了コードを返す.
    理由には入力ファイルのフルパス（一時フォルダの中）と、伏せるべき値を入れる."""

    def __init__(self, code: int, message: str):
        super().__init__(code=code)
        self.message = message

    def run(self, args, cwd, timeout):
        self.calls.append(args)
        report = Path(args[args.index("-o") + 1])
        report.write_text(json.dumps({"findings": []}), encoding="utf-8")
        src = args[1]
        return self.code, "", f"doc2md: 変換を始めます\nerror: {self.message.format(src=src)}\n"


BROKEN = "{src} は Office の形式ではありません（zip として読めません）. 作成者 taro@example.com / 090-1234-5678"


def test_exit_2_is_permanent_with_masked_reason(tmp_path):
    with pytest.raises(PermanentError) as ei:
        to_markdown("設計.xlsx", b"not a zip", tmp_path, StderrRunner(2, BROKEN))
    msg = str(ei.value)
    assert "終了コード 2" in msg and "設計.xlsx は Office の形式ではありません" in msg
    # 一時フォルダのパス・メールアドレス・電話番号はジョブのエラーに残さない
    assert str(tmp_path) not in msg and tmp_path.as_posix() not in msg
    assert "taro@example.com" not in msg and "090-1234-5678" not in msg
    # 前置きの行ではなく、理由の行（error: ...）を残す
    assert "変換を始めます" not in msg


def test_exit_3_is_retryable_with_reason(tmp_path):
    with pytest.raises(RetryableError) as ei:
        to_markdown("a.docx", b"PK", tmp_path, StderrRunner(3, "LibreOffice が応答しませんでした（{src}）"))
    assert "LibreOffice が応答しませんでした（a.docx）" in str(ei.value) and str(tmp_path) not in str(ei.value)


def test_stderr_summary_without_error_line_uses_last_line_and_limits_length():
    assert stderr_summary("") == "（理由は出力されませんでした）"
    assert stderr_summary("1 行目\n\n最後の行\n") == "最後の行"
    long = stderr_summary("error: " + "あ" * 1000)
    assert len(long) == 300 and long.endswith("…")


def _worker(settings, svc, runner):
    return Worker(settings, svc.blobs, svc.queue, svc.repo, FakeLLM(), NoNotifier(), runner)


def test_worker_does_not_retry_broken_input(settings, svc):
    """壊れた入力（doc2md の終了コード 2）は 1 回で failed にし、メッセージを消す（やり直さない）. 理由は伏せ字の後でエラーに残す."""
    job = svc.submit("alice", "a.xlsx", xlsx_bytes())
    runner = StderrRunner(2, BROKEN)
    _worker(settings, svc, runner).run_once(wait_seconds=1)
    done = svc.repo.get(job.job_id)
    assert done.status == JobStatus.FAILED and done.attempts == 1 and len(runner.calls) == 1
    assert "a.xlsx は Office の形式ではありません" in done.error and "taro@example.com" not in done.error and "/input/" not in done.error
    assert svc.queue.receive(1, 0, 900) == []  # メッセージは消えている（再配信されない）


def test_worker_keeps_reason_of_retryable_conversion_failure(settings, svc):
    """一時的な失敗（終了コード 3）で再試行するとき、ジョブのエラーに理由（stderr の要点）を残す（以前は例外の型の名前だけ）."""
    job = svc.submit("alice", "a.xlsx", xlsx_bytes())
    _worker(settings, svc, StderrRunner(3, "LibreOffice が応答しませんでした")).run_once(wait_seconds=1)
    j = svc.repo.get(job.job_id)
    assert j.status == JobStatus.QUEUED and "LibreOffice が応答しませんでした" in j.error
