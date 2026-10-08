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


def build(
    job: Job,
    findings: list,
    calls: list,
    notes: list[str],
    warnings: list[str] | None = None,
    routing: dict | None = None,
    partial: dict[str, int] | None = None,
) -> dict[str, Any]:
    """結果. partial は一部だけをレビューしたとき {"reviewed_chars", "total_chars"}（summary の partial が true になる）."""
    shown = [f for f in findings if f.status != "rejected"]
    rejected = [f for f in findings if f.status == "rejected"]
    return {
        "schema": "review-platform/report@1",
        "job": job.public_view(),
        "routing": routing or None,
        "conversion_warnings": list(warnings or []),
        "notes": list(notes),
        "partial": dict(partial) if partial else None,
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
        # 一部だけをレビューした（長すぎる文書の残りは見ていない）. submit --wait はこのとき終了コード 7（MUST ありなら 1）
        "partial": bool(report.get("partial")),
    }


_MENTION = re.compile(r"[@＠](channel|here|everyone|all)\b", re.I)
# Markdown のリンク・画像（インライン `[文字](URL)`・`![代替](URL)` と参照 `[文字][名前]`）. 文字だけを残す
_INLINE_LINK = re.compile(r"!?\[([^\[\]]*)\]\([^()]*(?:\([^()]*\)[^()]*)*\)")
_REF_LINK = re.compile(r"!?\[([^\[\]]*)\]\[[^\[\]]*\]")


def strip_links(s: str) -> str:
    """Markdown のリンク・画像を、その文字（画像は代替テキスト）だけにする. 外の URL を開かせない・読み込ませない."""
    prev = None
    while prev != s:  # `[[a](x)](y)` のような入れ子も外す
        prev = s
        s = _INLINE_LINK.sub(lambda m: m.group(1), s)
        s = _REF_LINK.sub(lambda m: m.group(1), s)
    return s


def clean(s: str, limit: int = 2000) -> str:
    """表のセルに入れる文字列を無害化する（HTML・リンク・画像・メンション・表の区切り・改行）."""
    s = strip_links((s or "")[:limit])
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # 残った角かっこも文字として出す（別の場所の参照定義 `[名前]: URL` とつながってリンクにならないように）
    s = s.replace("[", "\\[").replace("]", "\\]")
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
    if p := report.get("partial"):
        lines.append(f"- **範囲: 一部だけ**（全 {p['total_chars']:,} 文字のうち先頭の {p['reviewed_chars']:,} 文字. 残りはレビューしていません）")
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
