# マネージド ID と権限。
# api と worker は同じユーザー割り当てマネージド ID を使う（アプリは AZURE_CLIENT_ID で選ぶ）。
# 権限は「必要なものだけ・できるだけ狭い範囲に」付ける。
#   - Blob   : コンテナ review だけ
#   - Queue  : キュー jobs と jobs-poison だけ
#   - Table  : テーブル jobs だけ
#   - ACR    : イメージの pull だけ
#   - Key Vault: シークレットの読み取りだけ

resource "azurerm_user_assigned_identity" "app" {
  name                = "id-${local.prefix}-app"
  location            = azurerm_resource_group.this.location
  resource_group_name = azurerm_resource_group.this.name
  tags                = local.tags
}

locals {
  # データプレーンの権限を付ける範囲（ARM の ID）。自分で組み立てると、プロバイダの版の差に左右されない。
  blob_container_scope = "${azurerm_storage_account.this.id}/blobServices/default/containers/${azurerm_storage_container.review.name}"
  queue_scopes = {
    "jobs"        = "${azurerm_storage_account.this.id}/queueServices/default/queues/${azurerm_storage_queue.jobs.name}"
    "jobs-poison" = "${azurerm_storage_account.this.id}/queueServices/default/queues/${azurerm_storage_queue.jobs_poison.name}"
  }
  table_scope = "${azurerm_storage_account.this.id}/tableServices/default/tables/${azurerm_storage_table.jobs.name}"
}

resource "azurerm_role_assignment" "app_blob" {
  scope                = local.blob_container_scope
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
  principal_type       = "ServicePrincipal"
}

# 送る・受け取る・消す・別キューへ移す、のために Contributor を付ける
# （Message Processor / Sender の両方を含む）。KEDA のキュー長の読み取りもこれで足りる。
resource "azurerm_role_assignment" "app_queue" {
  for_each = local.queue_scopes

  scope                = each.value
  role_definition_name = "Storage Queue Data Contributor"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "app_table" {
  scope                = local.table_scope
  role_definition_name = "Storage Table Data Contributor"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "app_acr_pull" {
  scope                = azurerm_container_registry.this.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_role_assignment" "app_kv_secrets_user" {
  scope                = azurerm_key_vault.this.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
  principal_type       = "ServicePrincipal"
}

# terraform を実行した人に、シークレットを入れる権限を付ける。
# RBAC 型の Key Vault では、所有者（Owner）でもシークレットの読み書きはできないため。
resource "azurerm_role_assignment" "runner_kv_secrets_officer" {
  count = var.grant_runner_key_vault_secrets_officer ? 1 : 0

  scope                = azurerm_key_vault.this.id
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = data.azurerm_client_config.current.object_id
}
