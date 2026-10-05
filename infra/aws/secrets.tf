# シークレット（Secrets Manager）。
# Terraform は「入れ物」だけを作る。値は tfstate に残さないため、Terraform の外で入れる。
#   aws secretsmanager put-secret-value --secret-id <ARN> --secret-string '<値>'
# 値が入るまで、ワーカーのタスクは起動に失敗する（docs/AWS.md を参照）。

resource "aws_secretsmanager_secret" "anthropic_api_key" {
  count = var.enable_anthropic_api_key_secret ? 1 : 0

  name                    = "${local.name}/anthropic-api-key"
  description             = "ANTHROPIC_API_KEY for the review-platform worker"
  recovery_window_in_days = var.secret_recovery_window_days
}

resource "aws_secretsmanager_secret" "slack_webhook_url" {
  count = var.enable_slack_webhook_secret ? 1 : 0

  name                    = "${local.name}/slack-webhook-url"
  description             = "RP_SLACK_WEBHOOK_URL for the review-platform worker"
  recovery_window_in_days = var.secret_recovery_window_days
}
