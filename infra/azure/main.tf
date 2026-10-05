# 名前・タグの決まりと、リソースグループ。

locals {
  prefix = "rp-${var.env}"

  # 世界で一意にする必要がある名前（英小文字と数字だけ）。
  storage_account_name = "strp${var.env}${var.name_suffix}"
  acr_name             = "crrp${var.env}${var.name_suffix}"
  key_vault_name       = "kv-rp-${var.env}-${var.name_suffix}"

  # CONFIG.md の既定値に合わせた名前。
  blob_container_name = "review"
  queue_name          = "jobs"
  poison_queue_name   = "jobs-poison"
  table_name          = "jobs"

  # Key Vault のシークレット名。値は Terraform の外で入れる（docs/AZURE.md）。
  secret_anthropic_api_key = "anthropic-api-key"
  secret_slack_webhook_url = "slack-webhook-url"

  api_name    = "ca-${local.prefix}-api"
  worker_name = "ca-${local.prefix}-worker"

  tags = {
    Project     = "review-platform"
    Environment = var.env
  }
}

resource "azurerm_resource_group" "this" {
  name     = "rg-${local.prefix}"
  location = var.location
  tags     = local.tags
}
