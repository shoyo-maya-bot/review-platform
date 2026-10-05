# ストレージ。原本・結果（Blob）、ジョブのキュー（Queue）、ジョブの表（Table）を 1 つのアカウントに置く。
# 共有キーは無効。公開アクセスも無効。アプリはマネージド ID とプライベートエンドポイントで使う。
# コンテナ・キュー・テーブルは storage_account_id を指定して、管理プレーン（ARM）経由で作る。
# こうすると、Terraform を動かす端末から *.core.windows.net に届かなくても作れる。

resource "azurerm_storage_account" "this" {
  name                     = local.storage_account_name
  resource_group_name      = azurerm_resource_group.this.name
  location                 = azurerm_resource_group.this.location
  account_kind             = "StorageV2"
  account_tier             = "Standard"
  account_replication_type = var.storage_replication_type
  access_tier              = "Hot"

  min_tls_version                  = "TLS1_2"
  https_traffic_only_enabled       = true
  shared_access_key_enabled        = false
  default_to_oauth_authentication  = true
  public_network_access_enabled    = false
  allow_nested_items_to_be_public  = false
  cross_tenant_replication_enabled = false

  # 公開アクセスを止めているので効かないが、意図を残すために拒否を既定にする。
  network_rules {
    default_action = "Deny"
    bypass         = ["AzureServices"]
  }

  blob_properties {
    versioning_enabled = true

    delete_retention_policy {
      days = var.blob_soft_delete_days
    }

    container_delete_retention_policy {
      days = var.blob_soft_delete_days
    }
  }

  # queue_properties と static_website はデータプレーンを呼ぶので書かない
  # （versions.tf の data_plane_available = false と組で使う）。

  tags = local.tags
}

resource "azurerm_storage_container" "review" {
  name                  = local.blob_container_name
  storage_account_id    = azurerm_storage_account.this.id
  container_access_type = "private"
}

resource "azurerm_storage_queue" "jobs" {
  name               = local.queue_name
  storage_account_id = azurerm_storage_account.this.id
}

# Storage Queue にはデッドレターキューがない。
# アプリ（worker）は dequeue_count が 5 を超えたメッセージを、このキューへ移してから元のキューから消す。
resource "azurerm_storage_queue" "jobs_poison" {
  name               = local.poison_queue_name
  storage_account_id = azurerm_storage_account.this.id
}

resource "azurerm_storage_table" "jobs" {
  name               = local.table_name
  storage_account_id = azurerm_storage_account.this.id
}

# 原本（uploads/）と結果（results/）を job_ttl_days で消す。
# バージョニングを有効にしているので、本体を消すと「古いバージョン」として残る。
# 古いバージョンとスナップショットは、できてから blob_soft_delete_days で消す（その間は戻せる）。
# ライフサイクルは 1 日 1 回ほど動くので、消えるまで少し遅れることがある。
resource "azurerm_storage_management_policy" "this" {
  storage_account_id = azurerm_storage_account.this.id

  rule {
    name    = "expire-jobs"
    enabled = true

    filters {
      blob_types = ["blockBlob"]
      prefix_match = [
        "${local.blob_container_name}/uploads/",
        "${local.blob_container_name}/results/",
      ]
    }

    actions {
      base_blob {
        delete_after_days_since_modification_greater_than = var.job_ttl_days
      }

      version {
        delete_after_days_since_creation = var.blob_soft_delete_days
      }

      snapshot {
        delete_after_days_since_creation_greater_than = var.blob_soft_delete_days
      }
    }
  }
}

# ---------------------------------------------------------------
# プライベートエンドポイント（blob / queue / table）
# ---------------------------------------------------------------

locals {
  storage_subresources = toset(["blob", "queue", "table"])
}

resource "azurerm_private_endpoint" "storage" {
  for_each = local.storage_subresources

  name                          = "pe-${local.prefix}-st-${each.key}"
  location                      = azurerm_resource_group.this.location
  resource_group_name           = azurerm_resource_group.this.name
  subnet_id                     = azurerm_subnet.private_endpoints.id
  custom_network_interface_name = "nic-pe-${local.prefix}-st-${each.key}"

  private_service_connection {
    name                           = "psc-st-${each.key}"
    private_connection_resource_id = azurerm_storage_account.this.id
    subresource_names              = [each.key]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "default"
    private_dns_zone_ids = [azurerm_private_dns_zone.this[each.key].id]
  }

  tags = local.tags
}
