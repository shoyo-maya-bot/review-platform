"""1 行 1 JSON のログ. 本文・トークン・ファイルの中身は出さない（extra に渡した値だけを足す）."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

_STD = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        d = {
            "time": datetime.fromtimestamp(record.created, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for k, v in vars(record).items():
            if k not in _STD and not k.startswith("_"):
                d[k] = v
        if record.exc_info:
            d["exception"] = self.formatException(record.exc_info)[-2000:]
        return json.dumps(d, ensure_ascii=False, default=str)


def setup(level: str = "INFO") -> None:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(level)
    for noisy in ("botocore", "boto3", "urllib3", "azure", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel("WARNING")
