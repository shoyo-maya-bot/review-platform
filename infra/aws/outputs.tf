# apply のあとに使う値。`terraform output` で見られる。

output "alb_dns_name" {
  description = "ALB の DNS 名。証明書のドメインから CNAME（または Route 53 のエイリアス）で向ける。"
  value       = aws_lb.this.dns_name
}

output "alb_zone_id" {
  description = "ALB のホストゾーン ID（Route 53 のエイリアスレコードに使う）。"
  value       = aws_lb.this.zone_id
}

output "api_base_url" {
  description = "API の URL。public_base_url を指定していればそれ、なければ ALB の DNS 名（証明書の名前と合わないので確認用）。"
  value       = var.public_base_url != "" ? var.public_base_url : "https://${aws_lb.this.dns_name}"
}

output "mcp_url" {
  description = "MCP の URL（API と同じポートの /mcp）。"
  value       = "${var.public_base_url != "" ? var.public_base_url : "https://${aws_lb.this.dns_name}"}/mcp"
}

output "ecr_repository_url" {
  description = "イメージを push する先。"
  value       = aws_ecr_repository.this.repository_url
}

output "ecs_cluster_name" {
  description = "ECS クラスター名。"
  value       = aws_ecs_cluster.this.name
}

output "ecs_service_names" {
  description = "ECS サービス名（api / worker）。"
  value = {
    api    = aws_ecs_service.api.name
    worker = aws_ecs_service.worker.name
  }
}

output "log_group_names" {
  description = "CloudWatch Logs のロググループ名。"
  value = {
    api    = aws_cloudwatch_log_group.api.name
    worker = aws_cloudwatch_log_group.worker.name
  }
}

output "s3_bucket" {
  description = "原本と結果を置くバケット（RP_S3_BUCKET）。"
  value       = aws_s3_bucket.data.bucket
}

output "sqs_queue_url" {
  description = "ジョブのキュー（RP_SQS_QUEUE_URL）。"
  value       = aws_sqs_queue.jobs.url
}

output "sqs_dlq_url" {
  description = "デッドレターキュー。"
  value       = aws_sqs_queue.jobs_dlq.url
}

output "dynamodb_table" {
  description = "ジョブの表（RP_DDB_TABLE）。"
  value       = aws_dynamodb_table.jobs.name
}

output "cognito_user_pool_id" {
  description = "Cognito ユーザープール ID。ユーザーを作るときに使う。"
  value       = aws_cognito_user_pool.this.id
}

output "cognito_app_client_id" {
  description = "アプリクライアント ID（RP_JWT_AUDIENCE）。"
  value       = aws_cognito_user_pool_client.this.id
}

output "cognito_domain" {
  description = "ログイン画面のドメイン（作ったときだけ）。"
  value       = var.cognito_domain_prefix == null ? null : "https://${var.cognito_domain_prefix}.auth.${var.region}.amazoncognito.com"
}

output "jwt_issuer" {
  description = "JWT の発行者（RP_JWT_ISSUER）。"
  value       = local.jwt_issuer
}

output "jwt_jwks_url" {
  description = "JWT の公開鍵の URL（RP_JWT_JWKS_URL）。"
  value       = local.jwt_jwks_url
}

output "anthropic_api_key_secret_arn" {
  description = "ANTHROPIC_API_KEY のシークレット ARN（作ったときだけ）。値は Terraform の外で入れる。"
  value       = one(aws_secretsmanager_secret.anthropic_api_key[*].arn)
}

output "slack_webhook_url_secret_arn" {
  description = "RP_SLACK_WEBHOOK_URL のシークレット ARN（作ったときだけ）。値は Terraform の外で入れる。"
  value       = one(aws_secretsmanager_secret.slack_webhook_url[*].arn)
}

output "alarm_topic_arn" {
  description = "アラームの通知先 SNS トピック。"
  value       = aws_sns_topic.alarms.arn
}
