# コンテナレジストリ（ACR）。
# 既定は Basic（安い）。Basic/Standard は公開エンドポイントを閉じられないが、
# 管理者ユーザーを無効にし、pull はマネージド ID（AcrPull）だけにする。
# 閉じたいときは acr_sku = "Premium" と acr_private_endpoint_enabled = true にする。
# そのときは admin_cidrs の端末からだけ push でき、Container Apps はプライベートエンドポイントから pull する。

resource "azurerm_container_registry" "this" {
  name                = local.acr_name
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
  sku                 = var.acr_sku

  admin_enabled = false

  # Basic/Standard では公開エンドポイントを止められないので true のまま。
  public_network_access_enabled = true

  # 使われなくなったタグなしイメージを消す（Premium だけの機能）。
  retention_policy_in_days = var.acr_sku == "Premium" ? 7 : null

  # IP の制限は Premium だけ。プライベートエンドポイントを付けたときだけ拒否を既定にする
  # （付けないまま拒否にすると、Container Apps から pull できなくなる）。
  dynamic "network_rule_set" {
    for_each = var.acr_private_endpoint_enabled ? [1] : []

    content {
      default_action = "Deny"

      dynamic "ip_rule" {
        for_each = var.admin_cidrs

        content {
          action   = "Allow"
          ip_range = ip_rule.value
        }
      }
    }
  }

  tags = local.tags

  lifecycle {
    precondition {
      condition     = !var.acr_private_endpoint_enabled || var.acr_sku == "Premium"
      error_message = "ACR のプライベートエンドポイントには acr_sku = \"Premium\" が必要です。"
    }
  }
}

resource "azurerm_private_endpoint" "acr" {
  count = var.acr_private_endpoint_enabled ? 1 : 0

  name                          = "pe-${local.prefix}-acr"
  location                      = azurerm_resource_group.this.location
  resource_group_name           = azurerm_resource_group.this.name
  subnet_id                     = azurerm_subnet.private_endpoints.id
  custom_network_interface_name = "nic-pe-${local.prefix}-acr"

  private_service_connection {
    name                           = "psc-acr"
    private_connection_resource_id = azurerm_container_registry.this.id
    subresource_names              = ["registry"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "default"
    private_dns_zone_ids = [azurerm_private_dns_zone.this["acr"].id]
  }

  tags = local.tags
}
