"""依存の固定（コミット SHA）の食い違いを防ぐ."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _sha(spec: str, repo: str) -> str:
    m = re.search(rf"{re.escape(repo)}\.git@([0-9a-f]{{40}})", spec)
    assert m, f"{repo} がコミット SHA で固定されていません: {spec}"
    return m.group(1)


def test_doc2md_ref_matches_tools_extra():
    """イメージの doc2md（Dockerfile の DOC2MD_REF）と、ローカル用の tools extra は同じ版にする."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    (tools,) = project["optional-dependencies"]["tools"]
    m = re.search(r"^ARG DOC2MD_REF=([0-9a-f]{40})$", (ROOT / "Dockerfile").read_text(encoding="utf-8"), re.M)
    assert m, "Dockerfile に ARG DOC2MD_REF=<40 桁の SHA> がありません"
    assert _sha(tools, "doc-to-markdown") == m.group(1)


def test_dependencies_are_pinned_by_sha():
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    git = [d for d in deps if "git+" in d]
    assert {"agent-lab", "devtools-common"} <= {re.search(r"/([\w-]+)\.git@", d).group(1) for d in git}
    for d in git:
        _sha(d, re.search(r"/([\w-]+)\.git@", d).group(1))
