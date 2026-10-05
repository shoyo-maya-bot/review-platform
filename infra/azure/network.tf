# ネットワーク。
# VNet に 2 つのサブネットを作る。
#   - snet-aca: Container Apps 環境（ワークロードプロファイル型）用。Microsoft.App/environments に委任する。
#   - snet-pe : プライベートエンドポイント用（Storage・Key Vault・ACR）。
# プライベート DNS ゾーンを VNet にリンクして、*.core.windows.net などを VNet の中の IP に向ける。

resource "azurerm_virtual_network" "this" {
  name                = "vnet-${local.prefix}"
  location            = azurerm_resource_group.this.location
  resource_group_name = azurerm_resource_group.this.name
  address_space       = [var.vnet_address_space]
  tags                = local.tags
}

resource "azurerm_subnet" "aca" {
  name                 = "snet-aca"
  resource_group_name  = azurerm_resource_group.this.name
  virtual_network_name = azurerm_virtual_network.this.name
  address_prefixes     = [var.aca_subnet_cidr]

  delegation {
    name = "aca-environments"

    service_delegation {
      name    = "Microsoft.App/environments"
      actions = ["Microsoft.Network/virtualNetworks/subnets/join/action"]
    }
  }
}

resource "azurerm_subnet" "private_endpoints" {
  name                 = "snet-pe"
  resource_group_name  = azurerm_resource_group.this.name
  virtual_network_name = azurerm_virtual_network.this.name
  address_prefixes     = [var.pe_subnet_cidr]

  # プライベートエンドポイントに NSG を効かせたくなったら NetworkSecurityGroupEnabled にする。
  private_endpoint_network_policies = "Disabled"
}

# ---------------------------------------------------------------
# プライベート DNS ゾーン
# ---------------------------------------------------------------

locals {
  # いつも作るゾーン（ストレージの 3 つ）と、変数しだいで作るゾーン。
  private_dns_zones = merge(
    {
      blob  = "privatelink.blob.core.windows.net"
      queue = "privatelink.queue.core.windows.net"
      table = "privatelink.table.core.windows.net"
    },
    var.key_vault_private_endpoint_enabled ? { vault = "privatelink.vaultcore.azure.net" } : {},
    var.acr_private_endpoint_enabled ? { acr = "privatelink.azurecr.io" } : {},
  )
}

resource "azurerm_private_dns_zone" "this" {
  for_each = local.private_dns_zones

  name                = each.value
  resource_group_name = azurerm_resource_group.this.name
  tags                = local.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "this" {
  for_each = local.private_dns_zones

  name                 = "link-${each.key}"
  private_dns_zone_id  = azurerm_private_dns_zone.this[each.key].id
  virtual_network_id   = azurerm_virtual_network.this.id
  registration_enabled = false
  tags                 = local.tags
}
