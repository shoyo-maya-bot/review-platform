"""設定から実装を組み立てる（クラウドの SDK は使うときだけ import する）."""

from __future__ import annotations

from .config import Settings
from .ports import BlobStore, JobQueue, JobRepository, LLMClient, Notifier
from .service import Services


def storage(s: Settings) -> tuple[BlobStore, JobQueue, JobRepository]:
    if s.backend == "aws":
        from .adapters.aws import DynamoJobRepository, S3BlobStore, SqsQueue

        return (
            S3BlobStore(s.s3_bucket, s.aws_region, s.aws_endpoint_url),
            SqsQueue(s.sqs_queue_url, s.aws_region, s.aws_endpoint_url),
            DynamoJobRepository(s.ddb_table, s.aws_region, s.aws_endpoint_url),
        )
    if s.backend == "azure":
        from .adapters.azure import AzureBlobStore, AzureJobRepository, AzureQueue

        cs = s.azure_connection_string
        return (
            AzureBlobStore(s.azure_container, s.azure_blob_url, cs),
            AzureQueue(s.azure_queue_name, s.azure_queue_url, cs),
            AzureJobRepository(s.azure_table_name, s.azure_table_url, cs),
        )
    from .adapters.local import FsBlobStore, SqliteJobRepository, SqliteQueue

    return FsBlobStore(s.data_dir), SqliteQueue(s.data_dir), SqliteJobRepository(s.data_dir)


def services(s: Settings) -> Services:
    blobs, queue, repo = storage(s)
    return Services(settings=s, blobs=blobs, queue=queue, repo=repo)


def llm(s: Settings) -> LLMClient:
    from .adapters import llm as m

    if s.llm == "anthropic":
        return m.AnthropicLLM(s.anthropic_api_key, s.llm_base_url)
    if s.llm == "bedrock":
        return m.BedrockLLM(s.aws_region, "")
    if s.llm == "fake":
        return m.FakeLLM()
    return m.NoLLM()


def notifier(s: Settings) -> Notifier:
    from .adapters import notify

    if s.notify == "slack":
        return notify.SlackNotifier(s.slack_webhook_url)
    return notify.NoNotifier()
