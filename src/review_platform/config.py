"""設定. 環境変数だけを読む（名前と意味は docs/CONFIG.md が正）."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


class ConfigError(ValueError):
    """設定の誤り. 起動時に止める."""


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int, lo: int, hi: int) -> int:
    raw = _env(name, str(default))
    try:
        v = int(raw)
    except ValueError as e:
        raise ConfigError(f"{name} は整数で指定してください（{raw!r}）") from e
    if not lo <= v <= hi:
        raise ConfigError(f"{name} は {lo}〜{hi} で指定してください（{v}）")
    return v


def _float(name: str, default: float) -> float:
    raw = _env(name, str(default))
    try:
        v = float(raw)
    except ValueError as e:
        raise ConfigError(f"{name} は数値で指定してください（{raw!r}）") from e
    if v < 0:
        raise ConfigError(f"{name} は 0 以上で指定してください（{v}）")
    return v


def _choice(name: str, default: str, choices: tuple[str, ...]) -> str:
    v = _env(name, default).lower()
    if v not in choices:
        raise ConfigError(f"{name} は {' / '.join(choices)} のどれかです（{v!r}）")
    return v


def _bool(name: str, default: bool) -> bool:
    v = _env(name, "1" if default else "0").lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} は 1 / 0（true / false）で指定してください（{v!r}）")


def _csv(name: str, default: str = "") -> tuple[str, ...]:
    return tuple(x.strip() for x in _env(name, default).split(",") if x.strip())


@dataclass(frozen=True)
class Settings:
    backend: str = "local"
    log_level: str = "INFO"
    max_upload_bytes: int = 10 * 1024 * 1024
    allowed_extensions: tuple[str, ...] = (".xlsx", ".xlsm", ".docx", ".pptx", ".pdf", ".md", ".diff", ".patch")
    job_ttl_days: int = 30
    public_base_url: str = ""
    rate_limit_per_minute: int = 30

    auth: str = "token"
    api_token_hashes: tuple[str, ...] = ()
    jwt_issuer: str = ""
    jwt_audience: str = ""
    jwt_jwks_url: str = ""

    data_dir: Path = field(default_factory=lambda: Path(".rp-data"))

    aws_region: str = ""
    s3_bucket: str = ""
    sqs_queue_url: str = ""
    ddb_table: str = ""
    aws_endpoint_url: str = ""

    azure_blob_url: str = ""
    azure_queue_url: str = ""
    azure_table_url: str = ""
    azure_container: str = "review"
    azure_queue_name: str = "jobs"
    azure_table_name: str = "jobs"
    azure_connection_string: str = ""

    llm: str = "none"
    llm_base_url: str = "https://api.anthropic.com"
    anthropic_api_key: str = ""
    llm_model_standard: str = "claude-sonnet-5-5"
    llm_model_deep: str = "claude-opus-5-5"
    budget_per_job_usd: float = 1.0
    mask_pii: bool = True  # LLM に送る前にメールアドレス・電話番号を伏せる

    notify: str = "none"
    slack_webhook_url: str = ""

    host: str = "127.0.0.1"
    port: int = 8080
    forwarded_allow_ips: str = "127.0.0.1"

    @staticmethod
    def from_env() -> Settings:
        s = Settings(
            backend=_choice("RP_BACKEND", "local", ("local", "aws", "azure")),
            log_level=_choice("RP_LOG_LEVEL", "INFO", ("debug", "info", "warning", "error")).upper(),
            max_upload_bytes=_int("RP_MAX_UPLOAD_MB", 10, 1, 100) * 1024 * 1024,
            allowed_extensions=tuple(e.lower() if e.startswith(".") else f".{e.lower()}" for e in _csv("RP_ALLOWED_EXTENSIONS", ",".join(Settings.allowed_extensions))),
            job_ttl_days=_int("RP_JOB_TTL_DAYS", 30, 1, 3650),
            public_base_url=_env("RP_PUBLIC_BASE_URL").rstrip("/"),
            rate_limit_per_minute=_int("RP_RATE_LIMIT_PER_MINUTE", 30, 1, 10000),
            auth=_choice("RP_AUTH", "token", ("none", "token", "jwt")),
            api_token_hashes=tuple(h.lower() for h in _csv("RP_API_TOKENS_SHA256")),
            jwt_issuer=_env("RP_JWT_ISSUER").rstrip("/"),
            jwt_audience=_env("RP_JWT_AUDIENCE"),
            jwt_jwks_url=_env("RP_JWT_JWKS_URL"),
            data_dir=Path(_env("RP_DATA_DIR", ".rp-data")),
            aws_region=_env("AWS_REGION") or _env("AWS_DEFAULT_REGION"),
            s3_bucket=_env("RP_S3_BUCKET"),
            sqs_queue_url=_env("RP_SQS_QUEUE_URL"),
            ddb_table=_env("RP_DDB_TABLE"),
            aws_endpoint_url=_env("RP_AWS_ENDPOINT_URL"),
            azure_blob_url=_env("RP_AZURE_BLOB_URL").rstrip("/"),
            azure_queue_url=_env("RP_AZURE_QUEUE_URL").rstrip("/"),
            azure_table_url=_env("RP_AZURE_TABLE_URL").rstrip("/"),
            azure_container=_env("RP_AZURE_CONTAINER", "review"),
            azure_queue_name=_env("RP_AZURE_QUEUE_NAME", "jobs"),
            azure_table_name=_env("RP_AZURE_TABLE_NAME", "jobs"),
            azure_connection_string=_env("RP_AZURE_CONNECTION_STRING"),
            llm=_choice("RP_LLM", "none", ("none", "fake", "anthropic", "bedrock")),
            llm_base_url=_env("RP_LLM_BASE_URL", "https://api.anthropic.com").rstrip("/"),
            anthropic_api_key=_env("ANTHROPIC_API_KEY"),
            llm_model_standard=_env("RP_LLM_MODEL_STANDARD", "claude-sonnet-5-5"),
            llm_model_deep=_env("RP_LLM_MODEL_DEEP", "claude-opus-5-5"),
            budget_per_job_usd=_float("RP_BUDGET_PER_JOB_USD", 1.0),
            mask_pii=_bool("RP_MASK_PII", True),
            notify=_choice("RP_NOTIFY", "none", ("none", "slack")),
            slack_webhook_url=_env("RP_SLACK_WEBHOOK_URL"),
            host=_env("RP_HOST", "127.0.0.1"),
            port=_int("RP_PORT", 8080, 1, 65535),
            forwarded_allow_ips=_env("RP_FORWARDED_ALLOW_IPS", "127.0.0.1"),
        )
        s.validate()
        return s

    def validate(self, role: str = "any") -> None:
        """組み合わせの誤りを起動時に止める. role は api / worker / any."""
        if self.auth == "none" and not (self.backend == "local" and self.host in ("127.0.0.1", "localhost", "::1")):
            raise ConfigError("RP_AUTH=none は、RP_BACKEND=local かつ 127.0.0.1 で待ち受けるときだけ使えます")
        if role == "api" and self.auth == "token" and not self.api_token_hashes:
            raise ConfigError("RP_AUTH=token のときは RP_API_TOKENS_SHA256 を指定してください（`review-platform token` で作れます）")
        if self.auth == "jwt" and not (self.jwt_issuer and self.jwt_audience and self.jwt_jwks_url):
            raise ConfigError("RP_AUTH=jwt のときは RP_JWT_ISSUER・RP_JWT_AUDIENCE・RP_JWT_JWKS_URL がすべて必要です")
        for h in self.api_token_hashes:
            if len(h) != 64 or any(c not in "0123456789abcdef" for c in h):
                raise ConfigError("RP_API_TOKENS_SHA256 には SHA-256 の 16 進（64 文字）を並べてください。トークンそのものは置かないでください")
        if self.backend == "aws" and not (self.s3_bucket and self.sqs_queue_url and self.ddb_table):
            raise ConfigError("RP_BACKEND=aws のときは RP_S3_BUCKET・RP_SQS_QUEUE_URL・RP_DDB_TABLE が必要です")
        if self.backend == "azure" and not (self.azure_connection_string or (self.azure_blob_url and self.azure_queue_url and self.azure_table_url)):
            raise ConfigError("RP_BACKEND=azure のときは RP_AZURE_BLOB_URL・RP_AZURE_QUEUE_URL・RP_AZURE_TABLE_URL（または Azurite 用の接続文字列）が必要です")
        if role == "worker" and self.llm == "anthropic" and not self.anthropic_api_key:
            raise ConfigError("RP_LLM=anthropic のときは ANTHROPIC_API_KEY が必要です")
        if self.notify == "slack" and role == "worker" and not self.slack_webhook_url.startswith("https://"):
            raise ConfigError("RP_NOTIFY=slack のときは https で始まる RP_SLACK_WEBHOOK_URL が必要です")
        if self.llm_base_url and not _https_or_loopback(self.llm_base_url):
            raise ConfigError("RP_LLM_BASE_URL は https で指定してください（ローカルの試験だけ http://localhost を許す）")


def _https_or_loopback(url: str) -> bool:
    """https か、ローカルの試験用の http://localhost・127.0.0.1 だけを許す（ホスト名は完全一致で比べる）."""
    u = urlparse(url)
    if u.scheme == "https" and u.hostname:
        return True
    return u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1", "::1")
