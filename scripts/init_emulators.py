"""エミュレーター（moto・Azurite）に、バケット・キュー・表を作る（docker compose の init で使う）.

    python scripts/init_emulators.py aws      # RP_AWS_ENDPOINT_URL の moto に
    python scripts/init_emulators.py azure    # RP_AZURE_CONNECTION_STRING の Azurite に

クラウドでは Terraform が作るので、これは使わない.
"""

from __future__ import annotations

import contextlib
import os
import sys
import time


def aws() -> None:
    import boto3

    ep, region = os.environ["RP_AWS_ENDPOINT_URL"], os.environ.get("AWS_REGION", "ap-northeast-1")
    s3 = boto3.client("s3", endpoint_url=ep, region_name=region)
    s3.create_bucket(Bucket=os.environ["RP_S3_BUCKET"], CreateBucketConfiguration={"LocationConstraint": region})
    sqs = boto3.client("sqs", endpoint_url=ep, region_name=region)
    name = os.environ["RP_SQS_QUEUE_URL"].rstrip("/").rsplit("/", 1)[-1]
    sqs.create_queue(QueueName=name, Attributes={"VisibilityTimeout": "900"})
    boto3.client("dynamodb", endpoint_url=ep, region_name=region).create_table(
        TableName=os.environ["RP_DDB_TABLE"],
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "job_id", "AttributeType": "S"}, {"AttributeName": "owner", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "owner-job-index",
                "KeySchema": [{"AttributeName": "owner", "KeyType": "HASH"}, {"AttributeName": "job_id", "KeyType": "RANGE"}],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    )


def azure() -> None:
    from azure.core.exceptions import ResourceExistsError
    from azure.data.tables import TableServiceClient
    from azure.storage.blob import BlobServiceClient
    from azure.storage.queue import QueueServiceClient

    cs = os.environ["RP_AZURE_CONNECTION_STRING"]
    q = os.environ.get("RP_AZURE_QUEUE_NAME", "jobs")
    for fn in (
        lambda: BlobServiceClient.from_connection_string(cs).create_container(os.environ.get("RP_AZURE_CONTAINER", "review")),
        lambda: QueueServiceClient.from_connection_string(cs).create_queue(q),
        lambda: QueueServiceClient.from_connection_string(cs).create_queue(f"{q}-poison"),
        lambda: TableServiceClient.from_connection_string(cs).create_table(os.environ.get("RP_AZURE_TABLE_NAME", "jobs")),
    ):
        with contextlib.suppress(ResourceExistsError):
            fn()


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else ""
    for attempt in range(30):  # エミュレーターが立ち上がるのを待つ
        try:
            {"aws": aws, "azure": azure}[target]()
            print(f"{target}: 作成しました")
            sys.exit(0)
        except KeyError:
            print("使い方: init_emulators.py aws|azure", file=sys.stderr)
            sys.exit(2)
        except Exception as e:  # noqa: BLE001
            if "AlreadyExists" in type(e).__name__ or "ResourceInUse" in str(e) or "BucketAlreadyOwnedByYou" in str(e):
                print(f"{target}: 作成済みです")
                sys.exit(0)
            print(f"待っています（{attempt + 1}/30）: {type(e).__name__}", file=sys.stderr)
            time.sleep(2)
    sys.exit(1)
