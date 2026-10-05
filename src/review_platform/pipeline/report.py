"""結果（JSON と Markdown）を作る. Markdown は人が読むので、AI の出力に混ざった HTML・メンション・画像を無害化する."""

from __future__ import annotations

import re
from typing import Any

from ..domain import Job

SEVERITIES = ("MUST", "WANT", "NITS", "Q")
STATUS_LABEL = {"confirmed": "検証済み", "uncertain": "要確認", "unverified": "未検証", "rejected": "却下"}


def _f(f) -> dict[str, Any]:
    return {
        "severity": f.severity,
        "status": f.status,
        "location": f.location,
        "title": f.title,
        "detail": f.detail,
        "evidence": f.evidence,
        "models": list(f.models),
        "verdict_note": f.verdict_note,
    }


def _call(c) -> dict[str, Any]:
    return {k: getattr(c, k) for k in ("step", "model", "input_tokens", "output_tokens", "cost_usd")}


def build(job: Job, findings: list, calls: list, notes: list[str], warnings: list[str] | None = None, routing: dict | None = None) -> dict[str, Any]:
    shown = [f for f in findings if f.status != "rejected"]
    rejected = [f for f in findings if f.status == "rejected"]
    return {
        "schema": "review-platform/report@1",
        "job": job.public_view(),
        "routing": routing or None,
        "conversion_warnings": list(warnings or []),
        "notes": list(notes),
        "findings": [_f(f) for f in shown],
        "rejected": [_f(f) for f in rejected],
        "calls": [_call(c) for c in calls],
        "cost_usd": round(sum(c.cost_usd for c in calls), 4),
    }


def summary(report: dict[str, Any]) -> dict[str, Any]:
    sev = {k: 0 for k in SEVERITIES}
    for f in report["findings"]:
        sev[f["severity"]] = sev.get(f["severity"], 0) + 1
    return {
        "severity": sev,
        "findings": len(report["findings"]),
        "rejected": len(report["rejected"]),
        "open_must": sev["MUST"],
        "conversion_warnings": len(report["conversion_warnings"]),
    }


_MENTION = re.compile(r"[@＠](channel|here|everyone|all)\b", re.I)


def clean(s: str, limit: int = 2000) -> str:
    """表のセルに入れる文字列を無害化する（HTML・画像・メンション・表の区切り・改行）."""
    s = (s or "")[:limit]
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    s = s.replace("![", "!\\[")
    s = _MENTION.sub(lambda m: "@\u200b" + m.group(1), s)
    s = s.replace("|", "\\|").replace("\r", " ").replace("\n", " ")
    return s


def markdown(report: dict[str, Any]) -> str:
    j = report["job"]
    lines = [f"# レビュー結果: {clean(j['title'] or j['filename'], 200)}", ""]
    lines.append(f"- ジョブ: `{j['job_id']}`（{j['kind']}）")
    s = summary(report)
    lines.append("- 件数: " + " / ".join(f"{k} {v}" for k, v in s["severity"].items()) + f"（検証で却下 {s['rejected']}）")
    lines.append(f"- 費用: ${report['cost_usd']:.3f}")
    lines.append("")
    lines.append("> AI の指摘は下書きです。採否は人が決めてください。")
    lines.append("")
    if report["conversion_warnings"]:
        lines += ["## 変換の注意", ""] + [f"- {clean(w, 300)}" for w in report["conversion_warnings"]] + [""]
    if report["notes"]:
        lines += ["## メモ", ""] + [f"- {clean(n, 300)}" for n in report["notes"]] + [""]
    lines += ["## 指摘", ""]
    if report["findings"]:
        lines += ["| 状態 | 重要度 | 場所 | 指摘 | 根拠 |", "|---|---|---|---|---|"]
        for f in report["findings"]:
            body = f"**{clean(f['title'], 200)}** {clean(f['detail'])}"
            if f["verdict_note"]:
                body += f"<br>検証: {clean(f['verdict_note'], 500)}"
            lines.append(f"| {STATUS_LABEL.get(f['status'], f['status'])} | {f['severity']} | {clean(f['location'], 200)} | {body} | {clean(f['evidence'], 500)} |")
    else:
        lines.append("指摘はありません。")
    if report["rejected"]:
        lines += ["", f"<details><summary>検証で却下した指摘（{len(report['rejected'])} 件）</summary>", ""]
        lines += [f"- [{f['severity']}] {clean(f['location'], 200)} {clean(f['title'], 200)} — {clean(f['verdict_note'], 300)}" for f in report["rejected"]]
        lines += ["", "</details>"]
    return "\n".join(lines) + "\n"
