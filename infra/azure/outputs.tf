# 出力。デプロイ手順（docs/AZURE.md）で使う値。

output "resource_group_name" {
  description = "リソースグループ名。"
  value       = azurerm_resource_group.this.name
}

output "acr_login_server" {
  description = "ACR のログインサーバー。docker push の宛先。"
  value       = azurerm_container_registry.this.login_server
}

output "acr_name" {
  description = "ACR の名前（az acr login / az acr build で使う）。"
  value       = azurerm_container_registry.this.name
}

output "image" {
  description = "Container Apps が使うイメージ。"
  value       = local.image
}

output "key_vault_name" {
  description = "Key Vault の名前。シークレットを入れるときに使う。"
  value       = azurerm_key_vault.this.name
}

output "key_vault_secret_names" {
  description = "入れる必要があるシークレットの名前。"
  value       = [for k, v in local.app_secrets : k]
}

output "storage_account_name" {
  description = "ストレージアカウント名。"
  value       = azurerm_storage_account.this.name
}

output "managed_identity_client_id" {
  description = "アプリが使うマネージド ID のクライアント ID（AZURE_CLIENT_ID）。"
  value       = azurerm_user_assigned_identity.app.client_id
}

output "container_app_environment_default_domain" {
  description = "Container Apps 環境の既定ドメイン。"
  value       = azurerm_container_app_environment.this.default_domain
}

output "container_app_environment_static_ip" {
  description = "Container Apps 環境の IP（internal_only = true なら VNet の中の IP）。"
  value       = azurerm_container_app_environment.this.static_ip_address
}

output "api_url" {
  description = "API の URL（deploy_apps = false のときは、作った後の予定の URL）。"
  value       = "https://${local.api_fqdn}"
}

output "mcp_url" {
  description = "MCP の URL。VS Code などの MCP クライアントに登録する。"
  value       = "https://${local.api_fqdn}/mcp"
}

output "log_analytics_workspace_id" {
  description = "Log Analytics のワークスペース ID（ログの検索に使う GUID）。"
  value       = azurerm_log_analytics_workspace.this.workspace_id
}
