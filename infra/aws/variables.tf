# ---------------------------------------------------------------------------
# 基本
# ---------------------------------------------------------------------------

variable "region" {
  description = "作るリージョン。Bedrock で使うモデルが使えるリージョンを選ぶ。"
  type        = string
  default     = "ap-northeast-1"

  validation {
    condition     = can(regex("^[a-z]{2}(-[a-z]+)+-[0-9]$", var.region))
    error_message = "region は ap-northeast-1 のような形で指定してください。"
  }
}

variable "env" {
  description = "環境名（dev / stg / prod など）。名前とタグに使う。"
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,9}$", var.env))
    error_message = "env は英小文字で始まる 2〜10 文字（英小文字・数字・ハイフン）にしてください。"
  }
}

variable "project_name" {
  description = "リソース名の頭に付ける名前。"
  type        = string
  default     = "review-platform"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,19}$", var.project_name))
    error_message = "project_name は英小文字で始まる 3〜20 文字（英小文字・数字・ハイフン）にしてください。"
  }
}

# ---------------------------------------------------------------------------
# ネットワーク
# ---------------------------------------------------------------------------

variable "vpc_cidr" {
  description = "VPC の CIDR。/16〜/22 を想定。サブネットはここから自動で切り出す。"
  type        = string
  default     = "10.40.0.0/16"

  validation {
    condition     = can(regex("^[0-9.]+/(1[6-9]|2[0-2])$", var.vpc_cidr)) && can(cidrnetmask(var.vpc_cidr))
    error_message = "vpc_cidr は /16〜/22 の IPv4 CIDR にしてください。"
  }
}

variable "allowed_cidrs" {
  description = "ALB（443）に接続してよい送信元 CIDR の一覧。既定はなし。必ず指定する。"
  type        = list(string)
  default     = []

  validation {
    condition     = length(var.allowed_cidrs) > 0 && alltrue([for c in var.allowed_cidrs : can(cidrnetmask(c))])
    error_message = "allowed_cidrs に 1 つ以上の IPv4 CIDR（例: 203.0.113.0/24）を指定してください。"
  }
}

variable "interface_endpoint_az_count" {
  description = "インターフェース型 VPC エンドポイントを置く AZ の数。1 にすると費用が約半分になるが、その AZ の障害に弱くなる。"
  type        = number
  default     = 2

  validation {
    condition     = contains([1, 2], var.interface_endpoint_az_count)
    error_message = "interface_endpoint_az_count は 1 か 2 にしてください。"
  }
}

variable "enable_cognito_idp_endpoint" {
  description = "Cognito（cognito-idp）のインターフェース型エンドポイントを作るか。API が JWKS を取りに行く経路に使う（docs/AWS.md の既知の妥協を参照）。"
  type        = bool
  default     = true
}

variable "extra_egress_cidrs" {
  description = "タスクから 443 で外へ出てよい CIDR。社内プロキシや、自分で用意した NAT を通すときだけ使う。既定はなし（閉域）。"
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for c in var.extra_egress_cidrs : can(cidrnetmask(c))])
    error_message = "extra_egress_cidrs は IPv4 CIDR の一覧にしてください。"
  }
}

variable "enable_flow_logs" {
  description = "VPC フローログを CloudWatch Logs に送るか。"
  type        = bool
  default     = true
}

# ---------------------------------------------------------------------------
# 保存（S3・DynamoDB）
# ---------------------------------------------------------------------------

variable "job_ttl_days" {
  description = "ジョブと結果を消すまでの日数。S3 のライフサイクルとアプリの RP_JOB_TTL_DAYS に同じ値を使う。"
  type        = number
  default     = 30

  validation {
    condition     = var.job_ttl_days >= 1 && var.job_ttl_days <= 3650 && floor(var.job_ttl_days) == var.job_ttl_days
    error_message = "job_ttl_days は 1〜3650 の整数にしてください。"
  }
}

variable "s3_kms_key_arn" {
  description = "S3 を自分の KMS キー（SSE-KMS）で暗号化するときのキー ARN。null なら SSE-S3（AES256）。"
  type        = string
  default     = null

  validation {
    condition     = var.s3_kms_key_arn == null || can(regex("^arn:aws[a-zA-Z-]*:kms:[a-z0-9-]+:[0-9]{12}:key/.+$", var.s3_kms_key_arn))
    error_message = "s3_kms_key_arn は KMS キーの ARN（arn:aws:kms:...:key/...）か null にしてください。"
  }
}

variable "force_destroy" {
  description = "destroy のとき、中身が残っている S3 バケットと ECR リポジトリも消すか。学習用なら true が楽。本番は false。"
  type        = bool
  default     = false
}

variable "dynamodb_deletion_protection" {
  description = "DynamoDB の削除保護。true だと destroy できないので、学習用は false。"
  type        = bool
  default     = false
}

# ---------------------------------------------------------------------------
# コンテナ（ECS Fargate）
# ---------------------------------------------------------------------------

variable "image" {
  description = "コンテナイメージの完全な URI（タグかダイジェスト付き）。例: <account>.dkr.ecr.<region>.amazonaws.com/review-platform-dev:1.0.0"
  type        = string

  validation {
    condition     = can(regex("(@sha256:[a-f0-9]{64}|:[A-Za-z0-9_][A-Za-z0-9._-]{0,127})$", var.image)) && !endswith(var.image, ":latest")
    error_message = "image にはタグ（latest 以外）かダイジェストを付けてください。"
  }
}

variable "cpu_architecture" {
  description = "イメージの CPU アーキテクチャ。X86_64 か ARM64。ARM64 は少し安い。"
  type        = string
  default     = "X86_64"

  validation {
    condition     = contains(["X86_64", "ARM64"], var.cpu_architecture)
    error_message = "cpu_architecture は X86_64 か ARM64 にしてください。"
  }
}

variable "api_cpu" {
  description = "API タスクの CPU ユニット（1024 = 1 vCPU）。"
  type        = number
  default     = 512

  validation {
    condition     = contains([256, 512, 1024, 2048, 4096], var.api_cpu)
    error_message = "api_cpu は 256 / 512 / 1024 / 2048 / 4096 のどれかにしてください。"
  }
}

variable "api_memory" {
  description = "API タスクのメモリ（MiB）。CPU との組み合わせは Fargate の決まりに従う。"
  type        = number
  default     = 1024

  validation {
    condition     = var.api_memory >= 512 && var.api_memory <= 30720
    error_message = "api_memory は 512〜30720 MiB にしてください。"
  }
}

variable "worker_cpu" {
  description = "ワーカータスクの CPU ユニット（1024 = 1 vCPU）。変換で重くなるので API より大きめ。"
  type        = number
  default     = 1024

  validation {
    condition     = contains([256, 512, 1024, 2048, 4096], var.worker_cpu)
    error_message = "worker_cpu は 256 / 512 / 1024 / 2048 / 4096 のどれかにしてください。"
  }
}

variable "worker_memory" {
  description = "ワーカータスクのメモリ（MiB）。"
  type        = number
  default     = 2048

  validation {
    condition     = var.worker_memory >= 512 && var.worker_memory <= 30720
    error_message = "worker_memory は 512〜30720 MiB にしてください。"
  }
}

variable "api_desired_count" {
  description = "API タスクの数。本番で AZ 障害に備えるなら 2。"
  type        = number
  default     = 1

  validation {
    condition     = var.api_desired_count >= 1 && var.api_desired_count <= 10
    error_message = "api_desired_count は 1〜10 にしてください。"
  }
}

variable "worker_min_count" {
  description = "ワーカーの最小台数。0 ならジョブがないとき止まる（安い）。1 なら待ち時間が短い。"
  type        = number
  default     = 0

  validation {
    condition     = contains([0, 1], var.worker_min_count)
    error_message = "worker_min_count は 0 か 1 にしてください。"
  }
}

variable "worker_max_count" {
  description = "ワーカーの最大台数。LLM の費用の上限にもなる。"
  type        = number
  default     = 3

  validation {
    condition     = var.worker_max_count >= 1 && var.worker_max_count <= 20
    error_message = "worker_max_count は 1〜20 にしてください。"
  }
}

variable "worker_scale_in_minutes" {
  description = "キューが空（待ちも処理中も 0）の状態がこの分数続いたら、ワーカーを最小台数に戻す。"
  type        = number
  default     = 10

  validation {
    condition     = var.worker_scale_in_minutes >= 2 && var.worker_scale_in_minutes <= 60
    error_message = "worker_scale_in_minutes は 2〜60 にしてください。"
  }
}

variable "worker_use_spot" {
  description = "ワーカーを Fargate Spot で動かすか。安いが途中で止まることがある（ジョブはキューに戻ってやり直し）。"
  type        = bool
  default     = false
}

variable "log_retention_days" {
  description = "CloudWatch Logs の保存日数。"
  type        = number
  default     = 30

  validation {
    condition     = contains([1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653], var.log_retention_days)
    error_message = "log_retention_days は CloudWatch Logs が受け付ける日数（1, 3, 5, 7, 14, 30, 60, 90, ...）にしてください。"
  }
}

# ---------------------------------------------------------------------------
# ALB
# ---------------------------------------------------------------------------

variable "acm_certificate_arn" {
  description = "ALB の HTTPS に使う ACM 証明書の ARN（同じリージョンのもの）。"
  type        = string

  validation {
    condition     = can(regex("^arn:aws[a-zA-Z-]*:acm:[a-z0-9-]+:[0-9]{12}:certificate/.+$", var.acm_certificate_arn))
    error_message = "acm_certificate_arn は ACM 証明書の ARN にしてください。"
  }
}

variable "alb_idle_timeout" {
  description = "ALB のアイドルタイムアウト（秒）。MCP の長い応答を切らないよう少し長め。"
  type        = number
  default     = 120

  validation {
    condition     = var.alb_idle_timeout >= 1 && var.alb_idle_timeout <= 4000
    error_message = "alb_idle_timeout は 1〜4000 秒にしてください。"
  }
}

variable "alb_deletion_protection" {
  description = "ALB の削除保護。true だと destroy できないので、学習用は false。"
  type        = bool
  default     = false
}

# ---------------------------------------------------------------------------
# アプリの設定（docs/CONFIG.md の環境変数に渡す）
# ---------------------------------------------------------------------------

variable "public_base_url" {
  description = "利用者がアクセスする URL（例: https://review.example.com）。RP_PUBLIC_BASE_URL に渡す。空でもよい。"
  type        = string
  default     = ""

  validation {
    condition     = var.public_base_url == "" || can(regex("^https://[^/]+", var.public_base_url))
    error_message = "public_base_url は https:// で始まる URL か空文字にしてください。"
  }
}

variable "log_level" {
  description = "RP_LOG_LEVEL。"
  type        = string
  default     = "INFO"

  validation {
    condition     = contains(["DEBUG", "INFO", "WARNING", "ERROR"], var.log_level)
    error_message = "log_level は DEBUG / INFO / WARNING / ERROR のどれかにしてください。"
  }
}

variable "max_upload_mb" {
  description = "RP_MAX_UPLOAD_MB。受け付けるファイルの上限（MB）。"
  type        = number
  default     = 10

  validation {
    condition     = var.max_upload_mb >= 1 && var.max_upload_mb <= 100
    error_message = "max_upload_mb は 1〜100 にしてください。"
  }
}

variable "allowed_extensions" {
  description = "RP_ALLOWED_EXTENSIONS。受け付ける拡張子（カンマ区切り）。"
  type        = string
  default     = ".xlsx,.xlsm,.docx,.pptx,.pdf,.md,.diff,.patch"
}

variable "llm_provider" {
  description = "RP_LLM。bedrock（既定）/ anthropic / fake / none。"
  type        = string
  default     = "bedrock"

  validation {
    condition     = contains(["bedrock", "anthropic", "fake", "none"], var.llm_provider)
    error_message = "llm_provider は bedrock / anthropic / fake / none のどれかにしてください。"
  }
}

variable "llm_model_standard" {
  description = "RP_LLM_MODEL_STANDARD。Bedrock では推論プロファイル ID。空ならアプリの既定値を使う（bedrock では空にできない）。"
  type        = string
  default     = ""
}

variable "llm_model_deep" {
  description = "RP_LLM_MODEL_DEEP。検証に使う上位モデル。空なら検証しない。"
  type        = string
  default     = ""
}

variable "llm_base_url" {
  description = "RP_LLM_BASE_URL。llm_provider = anthropic のときだけ渡す。"
  type        = string
  default     = "https://api.anthropic.com"

  validation {
    condition     = can(regex("^https://", var.llm_base_url))
    error_message = "llm_base_url は https:// で始めてください。"
  }
}

variable "budget_per_job_usd" {
  description = "RP_BUDGET_PER_JOB_USD。1 ジョブの LLM 費用の上限（USD）。既定 0 = 有料の LLM（bedrock・anthropic）を呼ばない。呼ぶのは 3 つの上限をすべて 0 より大きくしたときだけ。"
  type        = number
  default     = 0

  validation {
    condition     = var.budget_per_job_usd >= 0 && var.budget_per_job_usd <= 100
    error_message = "budget_per_job_usd は 0 以上 100 以下にしてください（0 は有料の LLM を呼ばない）。"
  }
}

variable "budget_daily_usd" {
  description = "RP_BUDGET_DAILY_USD。1 日（UTC）の LLM 費用の上限（全体、USD）。既定 0 = 有料の LLM を呼ばない。"
  type        = number
  default     = 0

  validation {
    condition     = var.budget_daily_usd >= 0
    error_message = "budget_daily_usd は 0 以上にしてください（0 は有料の LLM を呼ばない）。"
  }
}

variable "budget_daily_per_owner_usd" {
  description = "RP_BUDGET_DAILY_PER_OWNER_USD。1 日（UTC）の LLM 費用の上限（利用者ごと、USD）。既定 0 = 有料の LLM を呼ばない。"
  type        = number
  default     = 0

  validation {
    condition     = var.budget_daily_per_owner_usd >= 0
    error_message = "budget_daily_per_owner_usd は 0 以上にしてください（0 は有料の LLM を呼ばない）。"
  }
}

variable "bedrock_model_arns" {
  description = "ワーカーが呼んでよい Bedrock の ARN（推論プロファイルと、その先の基盤モデルの両方）。llm_provider = bedrock のとき必須。"
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for a in var.bedrock_model_arns : can(regex("^arn:aws[a-zA-Z-]*:bedrock:", a))])
    error_message = "bedrock_model_arns は arn:aws:bedrock: で始まる ARN の一覧にしてください。"
  }
}

# ---------------------------------------------------------------------------
# シークレット（値は Terraform の外で入れる）
# ---------------------------------------------------------------------------

variable "enable_anthropic_api_key_secret" {
  description = "ANTHROPIC_API_KEY 用のシークレットを作り、ワーカーに渡すか。llm_provider = anthropic のとき true にする。"
  type        = bool
  default     = false
}

variable "enable_slack_webhook_secret" {
  description = "RP_SLACK_WEBHOOK_URL 用のシークレットを作り、ワーカーに渡すか。true なら RP_NOTIFY = slack になる。"
  type        = bool
  default     = false
}

variable "secret_recovery_window_days" {
  description = "シークレットを消したあと、元に戻せる日数。0 ならすぐ消える（学習用）。"
  type        = number
  default     = 7

  validation {
    condition     = var.secret_recovery_window_days == 0 || (var.secret_recovery_window_days >= 7 && var.secret_recovery_window_days <= 30)
    error_message = "secret_recovery_window_days は 0 か 7〜30 にしてください。"
  }
}

# ---------------------------------------------------------------------------
# 認証（Cognito）
# ---------------------------------------------------------------------------

variable "cognito_mfa" {
  description = "Cognito の MFA。OFF / OPTIONAL / ON。OPTIONAL と ON は認証アプリ（TOTP）を使う。"
  type        = string
  default     = "OPTIONAL"

  validation {
    condition     = contains(["OFF", "OPTIONAL", "ON"], var.cognito_mfa)
    error_message = "cognito_mfa は OFF / OPTIONAL / ON のどれかにしてください。"
  }
}

variable "cognito_allow_user_password_auth" {
  description = "USER_PASSWORD_AUTH（CLI でパスワードを送ってトークンを得る流れ）を許すか。学習で CLI からトークンを取るときだけ true。"
  type        = bool
  default     = false
}

variable "cognito_domain_prefix" {
  description = "Cognito のログイン画面（認可コード + PKCE）を使うときのドメインの頭。リージョン内で一意。null なら作らない。"
  type        = string
  default     = null

  validation {
    condition     = var.cognito_domain_prefix == null || can(regex("^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", var.cognito_domain_prefix))
    error_message = "cognito_domain_prefix は英小文字・数字・ハイフンにしてください（先頭と末尾はハイフン不可）。"
  }
}

variable "cognito_callback_urls" {
  description = "認可コード + PKCE のコールバック URL。空なら OAuth の流れは有効にしない。"
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for u in var.cognito_callback_urls : can(regex("^(https://|http://localhost(:[0-9]+)?/|http://127\\.0\\.0\\.1(:[0-9]+)?/)", u))])
    error_message = "cognito_callback_urls は https:// か http://localhost / http://127.0.0.1 の URL にしてください。"
  }
}

variable "cognito_deletion_protection" {
  description = "ユーザープールの削除保護。true だと destroy できない。"
  type        = bool
  default     = false
}

# ---------------------------------------------------------------------------
# 監視・予算
# ---------------------------------------------------------------------------

variable "notification_email" {
  description = "アラームと予算の通知を送るメールアドレス。SNS の確認メールのリンクを開くまで届かない。"
  type        = string

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.notification_email))
    error_message = "notification_email はメールアドレスの形にしてください。"
  }
}

variable "alb_5xx_threshold" {
  description = "5 分間の 5xx の数がこれ以上ならアラーム。"
  type        = number
  default     = 5

  validation {
    condition     = var.alb_5xx_threshold >= 1
    error_message = "alb_5xx_threshold は 1 以上にしてください。"
  }
}

variable "monthly_budget_usd" {
  description = "月の予算（USD）。実績が 80% と 100%、予測が 100% を超えたらメールが届く。アカウント全体が対象。"
  type        = number
  default     = 300

  validation {
    condition     = var.monthly_budget_usd > 0
    error_message = "monthly_budget_usd は 0 より大きくしてください。"
  }
}
