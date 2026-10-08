# Container Apps。
# 1 つのイメージを、起動コマンドだけ変えて 2 つのアプリにする（CONFIG.md の「コンテナの起動」）。
#   - api    : review-platform api（HTTP API と /mcp、ポート 8080、外部 ingress）
#   - worker : review-platform worker（ingress なし、キューの長さで KEDA が台数を増減）
#
# アプリは deploy_apps = true のときだけ作る。
# 先にイメージを ACR に push し、Key Vault にシークレットを入れておかないと、作成に失敗するため。

resource "azurerm_container_app_environment" "this" {
  name                = "cae-${local.prefix}"
  location            = azurerm_resource_group.this.location
  resource_group_name = azurerm_resource_group.this.name

  logs_destination           = "log-analytics"
  log_analytics_workspace_id = azurerm_log_analytics_workspace.this.id

  infrastructure_subnet_id           = azurerm_subnet.aca.id
  infrastructure_resource_group_name = "rg-${local.prefix}-aca-infra"
  internal_load_balancer_enabled     = var.internal_only

  workload_profile {
    name                  = "Consumption"
    workload_profile_type = "Consumption"
  }

  tags = local.tags
}

# 内部専用（internal_only = true）のときは、環境の既定ドメインを VNet の中で引けるようにする。
resource "azurerm_private_dns_zone" "aca" {
  count = var.internal_only ? 1 : 0

  name                = azurerm_container_app_environment.this.default_domain
  resource_group_name = azurerm_resource_group.this.name
  tags                = local.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "aca" {
  count = var.internal_only ? 1 : 0

  name                 = "link-aca"
  private_dns_zone_id  = azurerm_private_dns_zone.aca[0].id
  virtual_network_id   = azurerm_virtual_network.this.id
  registration_enabled = false
  tags                 = local.tags
}

resource "azurerm_private_dns_a_record" "aca_wildcard" {
  count = var.internal_only ? 1 : 0

  name                = "*"
  private_dns_zone_id = azurerm_private_dns_zone.aca[0].id
  ttl                 = 300
  records             = [azurerm_container_app_environment.this.static_ip_address]
  tags                = local.tags
}

# ---------------------------------------------------------------
# 環境変数（CONFIG.md の契約どおり）
# ---------------------------------------------------------------

locals {
  image = "${azurerm_container_registry.this.login_server}/${var.image_repository}:${var.image_tag}"

  # Container Apps の FQDN は「アプリ名.環境の既定ドメイン」になる。
  api_fqdn        = "${local.api_name}.${azurerm_container_app_environment.this.default_domain}"
  public_base_url = var.public_base_url != "" ? var.public_base_url : "https://${local.api_fqdn}"

  # api と worker に同じ値を渡す（worker は認証の値を使わないが、起動時の確認で困らないように揃える）。
  app_env = {
    RP_BACKEND         = "azure"
    RP_LOG_LEVEL       = var.log_level
    RP_MAX_UPLOAD_MB   = tostring(var.max_upload_mb)
    RP_JOB_TTL_DAYS    = tostring(var.job_ttl_days)
    RP_PUBLIC_BASE_URL = local.public_base_url

    RP_HOST = "0.0.0.0"
    RP_PORT = "8080"
    # アプリには Container Apps の ingress からしか届かないので、X-Forwarded-* を信用する。
    RP_FORWARDED_ALLOW_IPS = "*"

    RP_AUTH         = "jwt"
    RP_JWT_ISSUER   = var.jwt_issuer
    RP_JWT_AUDIENCE = var.jwt_audience
    RP_JWT_JWKS_URL = var.jwt_jwks_url

    # 末尾の / は付けない（CONFIG.md の書き方に合わせる）。
    RP_AZURE_BLOB_URL   = trimsuffix(azurerm_storage_account.this.primary_blob_endpoint, "/")
    RP_AZURE_QUEUE_URL  = trimsuffix(azurerm_storage_account.this.primary_queue_endpoint, "/")
    RP_AZURE_TABLE_URL  = trimsuffix(azurerm_storage_account.this.primary_table_endpoint, "/")
    RP_AZURE_CONTAINER  = local.blob_container_name
    RP_AZURE_QUEUE_NAME = local.queue_name
    RP_AZURE_TABLE_NAME = local.table_name
    AZURE_CLIENT_ID     = azurerm_user_assigned_identity.app.client_id

    RP_LLM                        = var.llm_mode
    RP_LLM_BASE_URL               = var.llm_base_url
    RP_LLM_MODEL_STANDARD         = var.llm_model_standard
    RP_LLM_MODEL_DEEP             = var.llm_model_deep
    RP_BUDGET_PER_JOB_USD         = tostring(var.budget_per_job_usd)
    RP_BUDGET_DAILY_USD           = tostring(var.budget_daily_usd)
    RP_BUDGET_DAILY_PER_OWNER_USD = tostring(var.budget_daily_per_owner_usd)

    RP_NOTIFY = var.notify
  }

  # Key Vault から読むシークレット。キー = Container App の中のシークレット名。
  app_secrets = merge(
    var.llm_mode == "anthropic" ? {
      (local.secret_anthropic_api_key) = {
        env_name = "ANTHROPIC_API_KEY"
        kv_id    = local.kv_secret_id_anthropic_api_key
      }
    } : {},
    var.notify == "slack" ? {
      (local.secret_slack_webhook_url) = {
        env_name = "RP_SLACK_WEBHOOK_URL"
        kv_id    = local.kv_secret_id_slack_webhook_url
      }
    } : {},
  )
}

# ---------------------------------------------------------------
# api（HTTP API と MCP）
# ---------------------------------------------------------------

resource "azurerm_container_app" "api" {
  count = var.deploy_apps ? 1 : 0

  name                         = local.api_name
  container_app_environment_id = azurerm_container_app_environment.this.id
  resource_group_name          = azurerm_resource_group.this.name
  revision_mode                = "Single"
  workload_profile_name        = "Consumption"

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.app.id]
  }

  registry {
    server   = azurerm_container_registry.this.login_server
    identity = azurerm_user_assigned_identity.app.id
  }

  dynamic "secret" {
    for_each = local.app_secrets

    content {
      name                = secret.key
      identity            = azurerm_user_assigned_identity.app.id
      key_vault_secret_id = secret.value.kv_id
    }
  }

  ingress {
    external_enabled           = true
    target_port                = 8080
    transport                  = "auto"
    allow_insecure_connections = false

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }

    # 許可リスト。Allow と Deny は混ぜられない。リストにない送信元は拒否される。
    dynamic "ip_security_restriction" {
      for_each = { for i, c in var.allowed_cidrs : tostring(i) => c }

      content {
        name             = "allow-${ip_security_restriction.key}"
        action           = "Allow"
        ip_address_range = ip_security_restriction.value
      }
    }
  }

  template {
    min_replicas = var.api_min_replicas
    max_replicas = var.api_max_replicas

    container {
      name    = "api"
      image   = local.image
      command = ["review-platform", "api"]
      cpu     = var.api_cpu
      memory  = var.api_memory

      dynamic "env" {
        for_each = local.app_env

        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = local.app_secrets

        content {
          name        = env.value.env_name
          secret_name = env.key
        }
      }

      liveness_probe {
        transport               = "HTTP"
        port                    = 8080
        path                    = "/healthz"
        initial_delay           = 10
        interval_seconds        = 30
        timeout                 = 5
        failure_count_threshold = 3
      }

      readiness_probe {
        transport               = "HTTP"
        port                    = 8080
        path                    = "/healthz"
        interval_seconds        = 10
        timeout                 = 5
        failure_count_threshold = 3
        success_count_threshold = 1
      }
    }

    http_scale_rule {
      name                = "http"
      concurrent_requests = "20"
    }
  }

  tags = local.tags

  # アプリを作る前に、権限と Key Vault・ストレージ・ACR への経路をそろえる。
  depends_on = [
    azurerm_role_assignment.app_acr_pull,
    azurerm_role_assignment.app_kv_secrets_user,
    azurerm_role_assignment.app_blob,
    azurerm_role_assignment.app_queue,
    azurerm_role_assignment.app_table,
    azurerm_private_endpoint.storage,
    azurerm_private_endpoint.key_vault,
    azurerm_private_endpoint.acr,
    azurerm_private_dns_zone_virtual_network_link.this,
  ]
}

# ---------------------------------------------------------------
# worker（キューを読んで処理する）
# ---------------------------------------------------------------

resource "azurerm_container_app" "worker" {
  count = var.deploy_apps ? 1 : 0

  name                         = local.worker_name
  container_app_environment_id = azurerm_container_app_environment.this.id
  resource_group_name          = azurerm_resource_group.this.name
  revision_mode                = "Single"
  workload_profile_name        = "Consumption"

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.app.id]
  }

  registry {
    server   = azurerm_container_registry.this.login_server
    identity = azurerm_user_assigned_identity.app.id
  }

  dynamic "secret" {
    for_each = local.app_secrets

    content {
      name                = secret.key
      identity            = azurerm_user_assigned_identity.app.id
      key_vault_secret_id = secret.value.kv_id
    }
  }

  # ingress は付けない（外から呼ばれない）。

  template {
    min_replicas = var.worker_min_replicas
    max_replicas = var.worker_max_replicas

    # 台数を減らすときに、処理中のジョブを終える時間を与える。
    termination_grace_period_seconds = 300
    cooldown_period_in_seconds       = 300
    polling_interval_in_seconds      = 30

    container {
      name    = "worker"
      image   = local.image
      command = ["review-platform", "worker"]
      cpu     = var.worker_cpu
      memory  = var.worker_memory

      dynamic "env" {
        for_each = local.app_env

        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = local.app_secrets

        content {
          name        = env.value.env_name
          secret_name = env.key
        }
      }
    }

    # KEDA の azure-queue。共有キーは無効なので、接続文字列ではなくマネージド ID で読む。
    # キュー jobs の長さ ÷ queueLength の台数まで増やす。
    custom_scale_rule {
      name             = "jobs-queue"
      custom_rule_type = "azure-queue"
      identity_id      = azurerm_user_assigned_identity.app.id

      metadata = {
        accountName = azurerm_storage_account.this.name
        queueName   = local.queue_name
        queueLength = tostring(var.worker_queue_length)
        cloud       = "AzurePublicCloud"
      }
    }
  }

  tags = local.tags

  # アプリを作る前に、権限と Key Vault・ストレージ・ACR への経路をそろえる。
  depends_on = [
    azurerm_role_assignment.app_acr_pull,
    azurerm_role_assignment.app_kv_secrets_user,
    azurerm_role_assignment.app_blob,
    azurerm_role_assignment.app_queue,
    azurerm_role_assignment.app_table,
    azurerm_private_endpoint.storage,
    azurerm_private_endpoint.key_vault,
    azurerm_private_endpoint.acr,
    azurerm_private_dns_zone_virtual_network_link.this,
  ]
}
