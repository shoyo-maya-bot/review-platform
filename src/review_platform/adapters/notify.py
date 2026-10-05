"""ジョブの完了を知らせる. Slack・Mattermost の Incoming Webhook と同じ形（{"text": …}）."""

from __future__ import annotations

import logging

import httpx

from ..domain import Job

log = logging.getLogger(__name__)


class NoNotifier:
    def notify(self, job: Job, url: str) -> None:
        return None


def _plain(s: str) -> str:
    """利用者が付けた文字列から、全員への通知（<!channel> など）やリンクの書式を取り除く."""
    return s.replace("<", "‹").replace(">", "›").replace("@", "＠")[:200]


class SlackNotifier:
    def __init__(self, webhook_url: str, client: httpx.Client | None = None):
        self.url = webhook_url
        self.http = client or httpx.Client(timeout=10)

    def notify(self, job: Job, url: str) -> None:
        s = job.summary
        counts = " / ".join(f"{k} {s.get('severity', {}).get(k, 0)}" for k in ("MUST", "WANT", "NITS", "Q"))
        title = _plain(job.title or job.filename)
        text = f"レビューが{'終わりました' if job.status.value == 'succeeded' else '失敗しました'}: {title}（{counts}）"
        if url:
            text += f"\n{url}"
        try:
            # 投稿は再試行しない（二重に投稿されるより、1 回落ちるほうがまし）
            self.http.post(self.url, json={"text": text}).raise_for_status()
        except httpx.HTTPError as e:
            log.warning("notify failed", extra={"job_id": job.job_id, "error": type(e).__name__})
