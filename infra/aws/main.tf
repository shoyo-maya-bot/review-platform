# 共通の値と、参照だけするデータソース。
# リソースは関心ごとにファイルを分けている（network.tf、storage.tf など）。

data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

data "aws_availability_zones" "available" {
  state = "available"

  # ローカルゾーンなどを除き、通常の AZ だけを使う。
  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}

locals {
  name       = "${var.project_name}-${var.env}"
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition

  # 2 つの AZ を使う。
  azs = slice(data.aws_availability_zones.available.names, 0, 2)

  container_port = 8080

  # Cognito の発行者と公開鍵の URL。
  jwt_issuer   = "https://${aws_cognito_user_pool.this.endpoint}"
  jwt_jwks_url = "https://${aws_cognito_user_pool.this.endpoint}/.well-known/jwks.json"

  # 通知の方式。Slack のシークレットを有効にしたときだけ slack。
  notify = var.enable_slack_webhook_secret ? "slack" : "none"

  # API とワーカーの両方に渡す環境変数（docs/CONFIG.md の名前のまま）。
  # 同じ設定を両方に渡し、役割の違いは起動コマンドだけにする。
  common_environment = merge(
    {
      RP_BACKEND            = "aws"
      RP_LOG_LEVEL          = var.log_level
      RP_MAX_UPLOAD_MB      = tostring(var.max_upload_mb)
      RP_ALLOWED_EXTENSIONS = var.allowed_extensions
      RP_JOB_TTL_DAYS       = tostring(var.job_ttl_days)
      RP_PUBLIC_BASE_URL    = var.public_base_url

      RP_AUTH         = "jwt"
      RP_JWT_ISSUER   = local.jwt_issuer
      RP_JWT_AUDIENCE = aws_cognito_user_pool_client.this.id
      RP_JWT_JWKS_URL = local.jwt_jwks_url

      AWS_REGION       = var.region
      RP_S3_BUCKET     = aws_s3_bucket.data.bucket
      RP_SQS_QUEUE_URL = aws_sqs_queue.jobs.url
      RP_DDB_TABLE     = aws_dynamodb_table.jobs.name

      RP_LLM                        = var.llm_provider
      RP_LLM_MODEL_DEEP             = var.llm_model_deep
      RP_BUDGET_PER_JOB_USD         = tostring(var.budget_per_job_usd)
      RP_BUDGET_DAILY_USD           = tostring(var.budget_daily_usd)
      RP_BUDGET_DAILY_PER_OWNER_USD = tostring(var.budget_daily_per_owner_usd)

      RP_NOTIFY = local.notify

      RP_HOST = "0.0.0.0"
      RP_PORT = tostring(local.container_port)
      # タスクには ALB からしか届かない（セキュリティグループで制限）ので、X-Forwarded-* を信用する。
      RP_FORWARDED_ALLOW_IPS = "*"
      # アプリの keep-alive は ALB のアイドルタイムアウトより長くする（短いと ALB が使い回す接続をアプリが先に閉じて 502 になる）。
      RP_KEEP_ALIVE_SECONDS = tostring(var.alb_idle_timeout + 5)

      # ルートファイルシステムを読み取り専用にするので、一時ファイルとホームを /tmp に向ける。
      TMPDIR = "/tmp"
      HOME   = "/tmp"
    },
    # 空ならアプリの既定値を使う。
    var.llm_model_standard == "" ? {} : { RP_LLM_MODEL_STANDARD = var.llm_model_standard },
    var.llm_provider == "anthropic" ? { RP_LLM_BASE_URL = var.llm_base_url } : {},
  )

  # ECS の形（name / value の配列）に直す。キーの順に並ぶので差分が安定する。
  environment_list = [for k, v in local.common_environment : { name = k, value = v }]

  # シークレットは使う側（ワーカー）だけに渡す。
  worker_secrets = concat(
    var.enable_anthropic_api_key_secret ? [{
      name      = "ANTHROPIC_API_KEY"
      valueFrom = aws_secretsmanager_secret.anthropic_api_key[0].arn
    }] : [],
    var.enable_slack_webhook_secret ? [{
      name      = "RP_SLACK_WEBHOOK_URL"
      valueFrom = aws_secretsmanager_secret.slack_webhook_url[0].arn
    }] : [],
  )

  secret_arns = concat(
    aws_secretsmanager_secret.anthropic_api_key[*].arn,
    aws_secretsmanager_secret.slack_webhook_url[*].arn,
  )
}
