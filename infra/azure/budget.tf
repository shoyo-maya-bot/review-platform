# 予算。リソースグループの月の費用が 80% と 100% を超えたらメールで知らせる。
# 予算は「知らせる」だけで、止めはしない。
# 注意: Container Apps 環境が作る基盤用のリソースグループ（rg-<prefix>-aca-infra）は対象外。

resource "azurerm_consumption_budget_resource_group" "this" {
  name              = "budget-${local.prefix}"
  resource_group_id = azurerm_resource_group.this.id

  amount     = var.budget_amount
  time_grain = "Monthly"

  time_period {
    start_date = var.budget_start_date
  }

  notification {
    enabled        = true
    threshold      = 80
    operator       = "GreaterThanOrEqualTo"
    threshold_type = "Actual"
    contact_emails = var.alert_emails
  }

  notification {
    enabled        = true
    threshold      = 100
    operator       = "GreaterThanOrEqualTo"
    threshold_type = "Actual"
    contact_emails = var.alert_emails
  }

  # 予測で 100% を超えそうなときも早めに知らせる。
  notification {
    enabled        = true
    threshold      = 100
    operator       = "GreaterThanOrEqualTo"
    threshold_type = "Forecasted"
    contact_emails = var.alert_emails
  }
}
