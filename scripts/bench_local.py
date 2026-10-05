"""ローカル実装（ファイルと SQLite）での簡単な性能の目安. 費用はかからない（LLM は fake）.

  python scripts/bench_local.py --jobs 100

HTTP の受付（POST /v1/jobs）の応答時間と、ワーカーが全件を終えるまでの時間を測り、Markdown の表で出す.
CI では結果を Job Summary に書くだけで、合否は付けない（共有ランナーは速さがぶれるため）.
本番の負荷試験の代わりにはならない. 遅くなる変更に気づくための目安.
"""

from __future__ import annotations

import argparse
import hashlib
import statistics
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from starlette.testclient import TestClient

from review_platform.adapters.llm import FakeLLM
from review_platform.adapters.notify import NoNotifier
from review_platform.api import create_app
from review_platform.config import Settings
from review_platform.domain import JobStatus
from review_platform.factory import services
from review_platform.worker import Worker

TOKEN = "bench-token-" + "0" * 32
DOC = ("# 会員 API 設計\n\n| 項目 | 型 | 桁 |\n|---|---|---|\n" + "| 項目 | 文字列 | 10 |\n" * 200).encode("utf-8")


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--jobs", type=int, default=100)
    args = ap.parse_args()

    with tempfile.TemporaryDirectory(prefix="rp-bench-") as tmp:
        s = replace(
            Settings(),
            data_dir=Path(tmp),
            auth="token",
            api_token_hashes=(hashlib.sha256(TOKEN.encode()).hexdigest(),),
            llm="fake",
            rate_limit_per_minute=100000,
        )
        svc = services(s)
        headers = {"authorization": f"Bearer {TOKEN}"}
        lat: list[float] = []
        with TestClient(create_app(s, svc, mcp_enabled=False)) as c:
            t0 = time.perf_counter()
            for i in range(args.jobs):
                t = time.perf_counter()
                r = c.post("/v1/jobs", headers=headers, files={"file": (f"design-{i}.md", DOC)})
                lat.append((time.perf_counter() - t) * 1000)
                assert r.status_code == 202, r.text
            submit_total = time.perf_counter() - t0

            w = Worker(s, svc.blobs, svc.queue, svc.repo, FakeLLM(), NoNotifier())
            t0 = time.perf_counter()
            while w.run_once(wait_seconds=0):
                pass
            work_total = time.perf_counter() - t0

            t = time.perf_counter()
            r = c.get("/v1/jobs?limit=100", headers=headers)
            list_ms = (time.perf_counter() - t) * 1000
            done = [j for j in r.json()["jobs"] if j["status"] == JobStatus.SUCCEEDED.value]

        assert len(done) == min(args.jobs, 100), f"終わっていないジョブがある: {len(done)}"
        print(f"## ローカル実装の性能の目安（{args.jobs} 件、LLM は fake）\n")
        print("| 項目 | 値 |\n|---|---|")
        print(f"| 受付の応答 p50 / p95 / 最大 | {statistics.median(lat):.1f} / {pct(lat, 95):.1f} / {max(lat):.1f} ms |")
        print(f"| 受付のスループット | {args.jobs / submit_total:.0f} 件/秒 |")
        print(f"| ワーカー（1 台）が全件を終える時間 | {work_total:.2f} 秒（{args.jobs / work_total:.0f} 件/秒） |")
        print(f"| 一覧（100 件）の応答 | {list_ms:.1f} ms |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
