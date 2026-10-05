# 監視。
#   - Log Analytics: Container Apps のログ（1 行 1 JSON）を集める。保持 30 日。
#   - アラート 1: api / worker のログに ERROR が出たら知らせる。
#   - アラート 2: jobs-poison にメッセージが入ったら知らせる（Storage のリソースログで検知）。
#   - アラート 3: キュー全体の滞留が多いときに知らせる（メトリック。アカウント単位でしか取れない）。

resource "azurerm_log_analytics_workspace" "this" {
  name                = "log-${local.prefix}"
  location            = azurerm_resource_group.this.location
  resource_group_name = azurerm_resource_group.this.name
  sku                 = "PerGB2018"
  retention_in_days   = var.log_retention_days
  daily_quota_gb      = var.log_daily_quota_gb
  tags                = local.tags
}

resource "azurerm_monitor_action_group" "this" {
  name                = "ag-${local.prefix}"
  resource_group_name = azurerm_resource_group.this.name
  short_name          = "rp-${var.env}"

  dynamic "email_receiver" {
    for_each = { for i, e in var.alert_emails : tostring(i) => e }

    content {
      name                    = "email-${email_receiver.key}"
      email_address           = email_receiver.value
      use_common_alert_schema = true
    }
  }

  tags = local.tags
}

# ---------------------------------------------------------------
# アラート 1: ERROR ログ
# ---------------------------------------------------------------

# 15 分ごとに、直近 15 分の api / worker のログから ERROR を含む行を数える。
# ログのテーブル（ContainerAppConsoleLogs_CL）は最初のログが届くまで存在しないので、
# 作るときのクエリ検査は飛ばす。
resource "azurerm_monitor_scheduled_query_rules_alert_v2" "app_errors" {
  name                = "alert-${local.prefix}-app-errors"
  location            = azurerm_resource_group.this.location
  resource_group_name = azurerm_resource_group.this.name
  description         = "api / worker のログに ERROR が出た。"
  severity            = 2
  enabled             = true

  scopes               = [azurerm_log_analytics_workspace.this.id]
  evaluation_frequency = "PT15M"
  window_duration      = "PT15M"

  criteria {
    query                   = <<-KQL
      ContainerAppConsoleLogs_CL
      | where ContainerAppName_s in ("${local.api_name}", "${local.worker_name}")
      | where Log_s has "ERROR"
    KQL
    time_aggregation_method = "Count"
    operator                = "GreaterThan"
    threshold               = 0

    failing_periods {
      minimum_failing_periods_to_trigger_alert = 1
      number_of_evaluation_periods             = 1
    }
  }

  auto_mitigation_enabled = true
  skip_query_validation   = true

  action {
    action_groups = [azurerm_monitor_action_group.this.id]
  }

  tags = local.tags
}

# ---------------------------------------------------------------
# アラート 2: jobs-poison に入った
# ---------------------------------------------------------------

# Storage のメトリックはキューごとに分かれない。
# そこで、キューへの書き込みのログ（StorageWrite）を Log Analytics に送り、
# jobs-poison への PutMessage を数える。
resource "azurerm_monitor_diagnostic_setting" "queue" {
  name                       = "diag-queue-to-law"
  target_resource_id         = "${azurerm_storage_account.this.id}/queueServices/default"
  log_analytics_workspace_id = azurerm_log_analytics_workspace.this.id

  enabled_log {
    category = "StorageWrite"
  }
}

resource "azurerm_monitor_scheduled_query_rules_alert_v2" "poison_queue" {
  name                = "alert-${local.prefix}-poison-queue"
  location            = azurerm_resource_group.this.location
  resource_group_name = azurerm_resource_group.this.name
  description         = "処理に何度も失敗したジョブが ${local.poison_queue_name} に移された。"
  severity            = 2
  enabled             = true

  scopes               = [azurerm_log_analytics_workspace.this.id]
  evaluation_frequency = "PT15M"
  window_duration      = "PT15M"

  criteria {
    query                   = <<-KQL
      StorageQueueLogs
      | where OperationName == "PutMessage"
      | where Uri contains "/${local.poison_queue_name}/messages"
    KQL
    time_aggregation_method = "Count"
    operator                = "GreaterThan"
    threshold               = 0

    failing_periods {
      minimum_failing_periods_to_trigger_alert = 1
      number_of_evaluation_periods             = 1
    }
  }

  auto_mitigation_enabled = true
  skip_query_validation   = true

  action {
    action_groups = [azurerm_monitor_action_group.this.id]
  }

  tags = local.tags

  depends_on = [azurerm_monitor_diagnostic_setting.queue]
}

# ---------------------------------------------------------------
# アラート 3: キューの滞留（アカウント全体）
# ---------------------------------------------------------------

# QueueMessageCount は 1 時間ごとの容量メトリック。jobs と jobs-poison の合計になる。
resource "azurerm_monitor_metric_alert" "queue_backlog" {
  name                = "alert-${local.prefix}-queue-backlog"
  resource_group_name = azurerm_resource_group.this.name
  scopes              = ["${azurerm_storage_account.this.id}/queueServices/default"]
  description         = "キューに残っているメッセージが多い（worker が止まっているか、追いついていない）。"
  severity            = 3
  frequency           = "PT1H"
  window_size         = "PT6H"
  auto_mitigate       = true

  criteria {
    metric_namespace = "Microsoft.Storage/storageAccounts/queueServices"
    metric_name      = "QueueMessageCount"
    aggregation      = "Average"
    operator         = "GreaterThan"
    threshold        = var.queue_backlog_threshold
  }

  action {
    action_group_id = azurerm_monitor_action_group.this.id
  }

  tags = local.tags
}
