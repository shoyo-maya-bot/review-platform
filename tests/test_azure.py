"""Azure の実装を Azurite（Azure Storage のエミュレーター）で試す. CI では Azurite をサービスとして起動して必ず流す.

RP_TEST_AZURITE に Azurite の接続文字列を入れると動く（無ければ飛ばす）.
"""

from __future__ import annotations

import os
import secrets

import pytest

CONN = os.environ.get("RP_TEST_AZURITE", "")
pytestmark = [pytest.mark.azure, pytest.mark.skipif(not CONN, reason="RP_TEST_AZURITE が無い（Azurite が無い）")]


@pytest.fixture
def names():
    sfx = secrets.token_hex(3)
    return f"c{sfx}", f"q{sfx}", f"t{sfx}"


@pytest.fixture
def created(names):
    from azure.data.tables import TableServiceClient
    from azure.storage.blob import BlobServiceClient
    from azure.storage.queue import QueueServiceClient

    c, q, t = names
    BlobServiceClient.from_connection_string(CONN).create_container(c)
    qs = QueueServiceClient.from_connection_string(CONN)
    qs.create_queue(q)
    qs.create_queue(f"{q}-poison")
    TableServiceClient.from_connection_string(CONN).create_table(t)
    return names


def test_blob(created):
    from contract import blob_contract
    from review_platform.adapters.azure import AzureBlobStore

    blob_contract(AzureBlobStore(created[0], connection_string=CONN))


def test_queue_and_poison(created):
    from azure.storage.queue import QueueClient

    from contract import queue_contract
    from review_platform.adapters.azure import AzureQueue

    q = AzureQueue(created[1], connection_string=CONN, max_dequeue=2)
    queue_contract(q)
    q.send("20260101000000000000-0123456789abcdef")
    for _ in range(2):
        m = q.receive(1, 0, 1)[0]
        q.extend(m, 0)
    assert q.receive(1, 0, 1) == []  # 3 回目は poison へ
    poison = QueueClient.from_connection_string(CONN, f"{created[1]}-poison")
    assert len(list(poison.receive_messages(max_messages=5))) == 1


def test_table(created):
    from contract import rate_contract, rate_race_contract, repo_contract, reserve_contract, reserve_race_contract, spend_contract
    from review_platform.adapters.azure import AzureJobRepository
    from review_platform.domain import Job, JobKind

    repo = AzureJobRepository(created[2], connection_string=CONN)
    repo_contract(repo)
    spend_contract(repo)
    reserve_contract(repo)
    rate_contract(repo)
    rate_race_contract(repo)
    reserve_race_contract(repo)
    old = Job.create("x", JobKind.DOCUMENT, "o.md", 1)
    old.expires_at = 1
    repo.create(old)
    assert repo.delete_expired(now_epoch=10) == 1
