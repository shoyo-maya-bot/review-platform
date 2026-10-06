"""ドキュメントの相対リンクが切れていないこと（ネットワークは使わない）."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
FENCE = re.compile(r"```.*?```", re.S)


def _anchors(md: Path) -> set[str]:
    out = set()
    for line in FENCE.sub("", md.read_text(encoding="utf-8")).splitlines():
        m = re.match(r"^#{1,6}\s+(.*)$", line)
        if m:
            slug = re.sub(r"[^\w\- ]", "", m.group(1).strip().lower()).replace(" ", "-")
            out.add(slug)
    return out


def test_relative_links_resolve():
    broken = []
    for md in [ROOT / "README.md", ROOT / "AGENTS.md", *sorted((ROOT / "docs").glob("*.md"))]:
        text = FENCE.sub("", md.read_text(encoding="utf-8"))
        for target in LINK.findall(text):
            if re.match(r"^[a-z]+:", target) or target.startswith("mailto:"):
                continue
            path, _, anchor = target.partition("#")
            dest = (md.parent / path).resolve() if path else md
            if not dest.exists():
                broken.append(f"{md.relative_to(ROOT)} -> {target}")
            elif anchor and dest.suffix == ".md" and anchor not in _anchors(dest):
                broken.append(f"{md.relative_to(ROOT)} -> {target}（見出しが無い）")
    assert broken == []
