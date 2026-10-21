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


def test_docker_base_image_is_pinned_by_digest():
    """ベースイメージはダイジェストで固定する（タグの付け替えで中身が変わらないように）. ビルド段と実行段は同じ."""
    froms = re.findall(r"^FROM\s+(\S+)", (ROOT / "Dockerfile").read_text(encoding="utf-8"), re.M)
    assert len(froms) == 2
    for f in froms:
        assert re.search(r"@sha256:[0-9a-f]{64}$", f), f"ダイジェストで固定されていません: {f}"
    assert len(set(froms)) == 1


def test_docker_pip_installs_use_constraints():
    """イメージの pip install はすべて constraints（-c）で版をそろえる（pip を消すための uninstall は除く）."""
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    installs = re.findall(r"pip install [^\n]*", text)
    assert installs and all(" -c " in i for i in installs), installs
    assert "COPY pyproject.toml README.md LICENSE constraints.txt" in text
    m = re.search(r"^ADD https://raw\.githubusercontent\.com/shoyo-maya-bot/doc-to-markdown/\$\{DOC2MD_REF\}/constraints\.txt ", text, re.M)
    assert m, "doc2md の constraints は DOC2MD_REF と同じ SHA から取る"


def _requires_devtools_common_sha(dist: str) -> str | None:
    """入っている dist が要求する devtools-common の SHA（入っていなければ None）."""
    from importlib import metadata

    try:
        reqs = metadata.requires(dist) or []
    except metadata.PackageNotFoundError:
        return None
    for r in reqs:
        if r.lower().startswith("devtools-common") and "git+" in r:
            return _sha(r, "devtools-common")
    return None


def test_installed_libraries_pin_the_same_devtools_common():
    """auto-review と（入っていれば）tools の doc-to-markdown が固定している devtools-common は、本体と同じ SHA.

    違うと pip install -e ".[tools]" が解決できない（同じ venv に入らない）. 入っていない dist は確かめない.
    """
    import pytest

    deps = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    (ours,) = [_sha(d, "devtools-common") for d in deps if "devtools-common.git@" in d]
    checked = 0
    for dist in ("auto-review", "doc-to-markdown"):
        sha = _requires_devtools_common_sha(dist)
        if sha is None:
            continue
        checked += 1
        assert sha == ours, f"{dist} は devtools-common {sha[:7]} を固定（本体は {ours[:7]}）"
    if not checked:
        pytest.skip("auto-review・doc-to-markdown が入っていない")


def test_docker_doc2md_constraints_are_used_as_is():
    """イメージの doc2md は、DOC2MD_REF の constraints.txt をそのまま使う（書き換えない）.

    DOC2MD_REF（28ee561 以降）の constraints は pillow 13.0.0 を固定しているので、#15 の pillow 12 → 13.0.0 の置き換えは消した.
    """
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert not re.search(r"^RUN sed [^\n]*/tmp/doc2md-constraints\.txt", text, re.M), "doc2md の constraints を書き換えない"
    assert "pillow==12" not in text
