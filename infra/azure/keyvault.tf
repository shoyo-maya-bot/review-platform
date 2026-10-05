# Key Vault。API キーなどの秘密情報を置く。
# 権限は RBAC 型（アクセスポリシーは使わない）。削除保護と論理削除を有効にする。
#
# シークレットの「値」は Terraform で作らない（状態ファイルに平文で残るため）。
# 人が az CLI で入れる（docs/AZURE.md）。Container App は「バージョンなしの ID」で参照するので、
# 値を入れ替えると、次のリビジョン（または 30 分ほどの再取得）で新しい値が使われる。
#   https://<vault>.vault.azure.net/secrets/anthropic-api-key
#   https://<vault>.vault.azure.net/secrets/slack-webhook-url（notify = slack のとき）

resource "azurerm_key_vault" "this" {
  name                = local.key_vault_name
  location            = azurerm_resource_group.this.location
  resource_group_name = azurerm_resource_group.this.name
  tenant_id           = data.azurerm_client_config.current.tenant_id
  sku_name            = "standard"

  rbac_authorization_enabled = true
  purge_protection_enabled   = true
  soft_delete_retention_days = 7

  public_network_access_enabled = var.key_vault_public_network_access_enabled

  # 公開エンドポイントを開けても、admin_cidrs 以外は拒否する。
  # プライベートエンドポイントからの通信は、この規則に関係なく届く。
  network_acls {
    default_action = "Deny"
    bypass         = "AzureServices"
    ip_rules       = var.admin_cidrs
  }

  tags = local.tags

  lifecycle {
    precondition {
      condition     = var.key_vault_public_network_access_enabled || var.key_vault_private_endpoint_enabled
      error_message = "Key Vault の公開アクセスを閉じるなら、key_vault_private_endpoint_enabled = true にしてください（Container Apps が読めなくなります）。"
    }
  }
}

resource "azurerm_private_endpoint" "key_vault" {
  count = var.key_vault_private_endpoint_enabled ? 1 : 0

  name                          = "pe-${local.prefix}-kv"
  location                      = azurerm_resource_group.this.location
  resource_group_name           = azurerm_resource_group.this.name
  subnet_id                     = azurerm_subnet.private_endpoints.id
  custom_network_interface_name = "nic-pe-${local.prefix}-kv"

  private_service_connection {
    name                           = "psc-kv"
    private_connection_resource_id = azurerm_key_vault.this.id
    subresource_names              = ["vault"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "default"
    private_dns_zone_ids = [azurerm_private_dns_zone.this["vault"].id]
  }

  tags = local.tags
}

locals {
  # vault_uri は末尾に / が付いている（https://<name>.vault.azure.net/）。
  kv_secret_id_anthropic_api_key = "${azurerm_key_vault.this.vault_uri}secrets/${local.secret_anthropic_api_key}"
  kv_secret_id_slack_webhook_url = "${azurerm_key_vault.this.vault_uri}secrets/${local.secret_slack_webhook_url}"
}
