"""doc2md の子プロセスに秘密の環境変数を渡さない."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from review_platform.pipeline.convert import ToolRunner, child_env


def test_child_env_keeps_only_needed_names():
    env = {
        "PATH": "/usr/bin",
        "HOME": "/tmp",
        "LANG": "C.UTF-8",
        "ANTHROPIC_API_KEY": "sk-secret",
        "AWS_SECRET_ACCESS_KEY": "aws-secret",
        "RP_API_TOKENS": "rp_x",
        "AZURE_CLIENT_SECRET": "az",
        "DEVTOOLS_LOG_LEVEL": "INFO",
        "DEVTOOLS_API_KEY": "dt-secret",
        "Path": "C:\\Windows",  # Windows は大文字小文字を区別しない
    }
    out = child_env(env)
    assert out == {"PATH": "/usr/bin", "HOME": "/tmp", "LANG": "C.UTF-8", "DEVTOOLS_LOG_LEVEL": "INFO", "Path": "C:\\Windows"}


def test_runner_does_not_pass_api_keys_to_child(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("DEVTOOLS_TRACE", "1")
    code, out, _ = ToolRunner().run([sys.executable, "-c", "import json, os; print(json.dumps(dict(os.environ)))"], tmp_path, 30)
    seen = json.loads(out)
    assert code == 0 and "ANTHROPIC_API_KEY" not in seen and seen.get("DEVTOOLS_TRACE") == "1"
