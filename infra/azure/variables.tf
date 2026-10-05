# 入力変数。値は terraform.tfvars に書く（terraform.tfvars.example を写して使う）。
# 変数どうしをまたぐ確認は、各リソースの precondition で行う（Terraform 1.6 でも動くように）。

# ---------------------------------------------------------------
# 基本
# ---------------------------------------------------------------

variable "subscription_id" {
  description = "作る先のサブスクリプション ID。"
  type        = string

  validation {
    condition     = can(regex("^[0-9a-fA-F-]{36}$", var.subscription_id))
    error_message = "サブスクリプション ID は GUID の形で書いてください。"
  }
}

variable "location" {
  description = "リージョン。Container Apps のワークロードプロファイルが使えるリージョンを選ぶ。"
  type        = string
  default     = "japaneast"
}

variable "env" {
  description = "環境名。名前とタグ（Environment）に使う。"
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev", "stg", "prod"], var.env)
    error_message = "env は dev / stg / prod のどれかにしてください。"
  }
}

variable "name_suffix" {
  description = "世界で一意にしたい名前（ストレージ・ACR・Key Vault）の末尾に付ける文字。英小文字と数字で 3〜8 文字。"
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9]{3,8}$", var.name_suffix))
    error_message = "name_suffix は英小文字と数字で 3〜8 文字にしてください。"
  }
}

# ---------------------------------------------------------------
# ネットワーク
# ---------------------------------------------------------------

variable "vnet_address_space" {
  description = "VNet のアドレス範囲。"
  type        = string
  default     = "10.40.0.0/16"

  validation {
    condition     = can(cidrhost(var.vnet_address_space, 0))
    error_message = "CIDR の形で書いてください。"
  }
}

variable "aca_subnet_cidr" {
  description = "Container Apps 環境のサブネット。/23 以上の広さにする。"
  type        = string
  default     = "10.40.0.0/23"

  validation {
    condition     = can(cidrhost(var.aca_subnet_cidr, 0)) && tonumber(split("/", var.aca_subnet_cidr)[1]) <= 23
    error_message = "aca_subnet_cidr は /23 以上の広さの CIDR にしてください。"
  }
}

variable "pe_subnet_cidr" {
  description = "プライベートエンドポイントを置くサブネット。"
  type        = string
  default     = "10.40.2.0/24"

  validation {
    condition     = can(cidrhost(var.pe_subnet_cidr, 0))
    error_message = "CIDR の形で書いてください。"
  }
}

variable "internal_only" {
  description = "true にすると Container Apps 環境を内部ロードバランサーだけにする（VNet の中からしか API に届かない）。"
  type        = bool
  default     = false
}

variable "allowed_cidrs" {
  description = "API（ingress）に届いてよい送信元の CIDR。最低 1 つ。だれでもよいときは 0.0.0.0/0 を明示する。"
  type        = list(string)

  validation {
    condition     = length(var.allowed_cidrs) > 0 && alltrue([for c in var.allowed_cidrs : can(cidrhost(c, 0))])
    error_message = "allowed_cidrs には CIDR を 1 つ以上書いてください。"
  }
}

variable "admin_cidrs" {
  description = "管理者の作業端末の CIDR。Key Vault にシークレットを入れるとき、Premium の ACR に push するときに通す。"
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for c in var.admin_cidrs : can(cidrhost(c, 0))])
    error_message = "admin_cidrs は CIDR の形で書いてください。"
  }
}

# ---------------------------------------------------------------
# ストレージ
# ---------------------------------------------------------------

variable "storage_replication_type" {
  description = "ストレージの冗長化。学習用は LRS で十分。"
  type        = string
  default     = "LRS"

  validation {
    condition     = contains(["LRS", "ZRS", "GRS", "GZRS"], var.storage_replication_type)
    error_message = "LRS / ZRS / GRS / GZRS のどれかにしてください。"
  }
}

variable "job_ttl_days" {
  description = "ジョブと結果を消すまでの日数。アプリの RP_JOB_TTL_DAYS と Blob のライフサイクルの両方に使う。"
  type        = number
  default     = 30

  validation {
    condition     = var.job_ttl_days >= 1 && var.job_ttl_days <= 3650
    error_message = "job_ttl_days は 1〜3650 にしてください。"
  }
}

variable "blob_soft_delete_days" {
  description = "Blob とコンテナの論理削除の保持日数。"
  type        = number
  default     = 7

  validation {
    condition     = var.blob_soft_delete_days >= 1 && var.blob_soft_delete_days <= 365
    error_message = "blob_soft_delete_days は 1〜365 にしてください。"
  }
}

# ---------------------------------------------------------------
# コンテナレジストリ
# ---------------------------------------------------------------

variable "acr_sku" {
  description = "ACR の SKU。Basic が安い。プライベートエンドポイントを使うなら Premium。"
  type        = string
  default     = "Basic"

  validation {
    condition     = contains(["Basic", "Standard", "Premium"], var.acr_sku)
    error_message = "Basic / Standard / Premium のどれかにしてください。"
  }
}

variable "acr_private_endpoint_enabled" {
  description = "ACR にプライベートエンドポイントを付けるか。true のときは acr_sku = Premium が必要。"
  type        = bool
  default     = false
}

# ---------------------------------------------------------------
# Key Vault
# ---------------------------------------------------------------

variable "key_vault_public_network_access_enabled" {
  description = "Key Vault の公開エンドポイントを開けるか。開けても admin_cidrs 以外は拒否する。"
  type        = bool
  default     = true
}

variable "key_vault_private_endpoint_enabled" {
  description = "Key Vault にプライベートエンドポイントを付けるか。Container Apps はこちらを通って読む。"
  type        = bool
  default     = true
}

variable "grant_runner_key_vault_secrets_officer" {
  description = "terraform を実行した人に Key Vault Secrets Officer を付けるか（シークレットを入れるため）。"
  type        = bool
  default     = true
}

# ---------------------------------------------------------------
# アプリ（Container Apps）
# ---------------------------------------------------------------

variable "deploy_apps" {
  description = "Container App（api と worker）を作るか。1 回目は false で土台だけ作り、イメージの push とシークレット登録のあとに true にする。"
  type        = bool
  default     = false
}

variable "image_repository" {
  description = "ACR の中のリポジトリ名。"
  type        = string
  default     = "review-platform"
}

variable "image_tag" {
  description = "使うイメージのタグ。latest ではなく、ビルドごとの値（git のコミットなど）を勧める。"
  type        = string
  default     = "0.1.0"
}

variable "api_cpu" {
  description = "api の vCPU。Consumption では cpu と memory の組み合わせが決まっている（0.5 と 1Gi など）。"
  type        = number
  default     = 0.5
}

variable "api_memory" {
  description = "api のメモリ（例 1Gi）。"
  type        = string
  default     = "1Gi"
}

variable "worker_cpu" {
  description = "worker の vCPU。文書の変換があるので api より多め。"
  type        = number
  default     = 1
}

variable "worker_memory" {
  description = "worker のメモリ（例 2Gi）。"
  type        = string
  default     = "2Gi"
}

variable "api_min_replicas" {
  description = "api の最小台数。0 なら使わないときは費用がかからないが、最初の応答が遅くなる。"
  type        = number
  default     = 0

  validation {
    condition     = var.api_min_replicas >= 0 && var.api_min_replicas <= 10
    error_message = "api_min_replicas は 0〜10 にしてください。"
  }
}

variable "api_max_replicas" {
  description = "api の最大台数。"
  type        = number
  default     = 3

  validation {
    condition     = var.api_max_replicas >= 1 && var.api_max_replicas <= 30
    error_message = "api_max_replicas は 1〜30 にしてください。"
  }
}

variable "worker_min_replicas" {
  description = "worker の最小台数。0 ならキューが空のとき止まる。KEDA がキューを読めない環境では 1 にする（docs/AZURE.md の既知の妥協）。"
  type        = number
  default     = 0

  validation {
    condition     = var.worker_min_replicas >= 0 && var.worker_min_replicas <= 10
    error_message = "worker_min_replicas は 0〜10 にしてください。"
  }
}

variable "worker_max_replicas" {
  description = "worker の最大台数。LLM の呼び出し上限（レート制限）に合わせて決める。"
  type        = number
  default     = 5

  validation {
    condition     = var.worker_max_replicas >= 1 && var.worker_max_replicas <= 30
    error_message = "worker_max_replicas は 1〜30 にしてください。"
  }
}

variable "worker_queue_length" {
  description = "worker 1 台あたりのキューの長さの目安。KEDA はこの値で台数を決める。"
  type        = number
  default     = 5

  validation {
    condition     = var.worker_queue_length >= 1
    error_message = "worker_queue_length は 1 以上にしてください。"
  }
}

variable "log_level" {
  description = "RP_LOG_LEVEL。"
  type        = string
  default     = "INFO"

  validation {
    condition     = contains(["DEBUG", "INFO", "WARNING", "ERROR"], var.log_level)
    error_message = "DEBUG / INFO / WARNING / ERROR のどれかにしてください。"
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

variable "public_base_url" {
  description = "RP_PUBLIC_BASE_URL。空なら api の既定 URL（https://<アプリ名>.<環境の既定ドメイン>）を使う。"
  type        = string
  default     = ""
}

# ---------------------------------------------------------------
# 認証（Entra ID の JWT）
# ---------------------------------------------------------------

variable "jwt_issuer" {
  description = "RP_JWT_ISSUER。例 https://login.microsoftonline.com/<tenant-id>/v2.0"
  type        = string

  validation {
    condition     = startswith(var.jwt_issuer, "https://")
    error_message = "jwt_issuer は https:// で始めてください。"
  }
}

variable "jwt_audience" {
  description = "RP_JWT_AUDIENCE。v2 のアクセストークンではアプリ登録のアプリケーション（クライアント）ID。v1 なら api://... の URI。"
  type        = string

  validation {
    condition     = length(trimspace(var.jwt_audience)) > 0
    error_message = "jwt_audience を入れてください。"
  }
}

variable "jwt_jwks_url" {
  description = "RP_JWT_JWKS_URL。例 https://login.microsoftonline.com/<tenant-id>/discovery/v2.0/keys"
  type        = string

  validation {
    condition     = startswith(var.jwt_jwks_url, "https://")
    error_message = "jwt_jwks_url は https:// で始めてください。"
  }
}

# ---------------------------------------------------------------
# LLM
# ---------------------------------------------------------------

variable "llm_mode" {
  description = "RP_LLM。クラウドでは anthropic（Messages API と同じ形の窓口）を使う。"
  type        = string
  default     = "anthropic"

  validation {
    condition     = contains(["none", "fake", "anthropic"], var.llm_mode)
    error_message = "Azure では none / fake / anthropic のどれかにしてください。"
  }
}

variable "llm_base_url" {
  description = "RP_LLM_BASE_URL。Microsoft Foundry など、Messages API と同じ形の窓口。モデルの用意はこの Terraform の外で行う。"
  type        = string
  default     = "https://api.anthropic.com"

  validation {
    condition     = startswith(var.llm_base_url, "https://")
    error_message = "llm_base_url は https:// で始めてください。"
  }
}

variable "llm_model_standard" {
  description = "RP_LLM_MODEL_STANDARD。Foundry ではデプロイ名を入れる。"
  type        = string
  default     = "claude-sonnet-5-5"
}

variable "llm_model_deep" {
  description = "RP_LLM_MODEL_DEEP。空なら検証しない。"
  type        = string
  default     = "claude-opus-5-5"
}

variable "budget_per_job_usd" {
  description = "RP_BUDGET_PER_JOB_USD。1 ジョブの費用の上限（米ドル）。"
  type        = number
  default     = 1.0
}

# ---------------------------------------------------------------
# 通知
# ---------------------------------------------------------------

variable "notify" {
  description = "RP_NOTIFY。slack にすると Key Vault の slack-webhook-url を参照する。"
  type        = string
  default     = "none"

  validation {
    condition     = contains(["none", "slack"], var.notify)
    error_message = "none か slack にしてください。"
  }
}

# ---------------------------------------------------------------
# 監視・予算
# ---------------------------------------------------------------

variable "log_retention_days" {
  description = "Log Analytics の保持日数。30 日までは追加料金がかからない。"
  type        = number
  default     = 30

  validation {
    condition     = var.log_retention_days >= 30 && var.log_retention_days <= 730
    error_message = "log_retention_days は 30〜730 にしてください。"
  }
}

variable "log_daily_quota_gb" {
  description = "Log Analytics の 1 日の取り込み上限（GB）。ログが暴れたときの費用の歯止め。-1 で無制限。"
  type        = number
  default     = 1
}

variable "alert_emails" {
  description = "アラートと予算の通知を受けるメールアドレス。最低 1 つ。"
  type        = list(string)

  validation {
    condition     = length(var.alert_emails) > 0 && alltrue([for e in var.alert_emails : can(regex("^[^@\\s]+@[^@\\s]+$", e))])
    error_message = "alert_emails にメールアドレスを 1 つ以上書いてください。"
  }
}

variable "queue_backlog_threshold" {
  description = "キューに残るメッセージ数（アカウント全体）がこの値を超えたら知らせる。"
  type        = number
  default     = 50
}

variable "budget_amount" {
  description = "リソースグループの月の予算。単位は請求の通貨（日本の契約なら円）。"
  type        = number
  default     = 10000

  validation {
    condition     = var.budget_amount > 0
    error_message = "budget_amount は 0 より大きくしてください。"
  }
}

variable "budget_start_date" {
  description = "予算の開始日。月の 1 日にする（例 2026-10-01T00:00:00Z）。今月か、それより後の月にする。"
  type        = string

  validation {
    condition     = can(regex("^[0-9]{4}-[0-9]{2}-01T00:00:00Z$", var.budget_start_date))
    error_message = "budget_start_date は YYYY-MM-01T00:00:00Z の形にしてください。"
  }
}
