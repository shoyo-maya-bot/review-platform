# 認証（Cognito ユーザープール）。
# 自分で登録はできない。管理者がユーザーを作る。
# API は JWT（RP_AUTH=jwt）で、このプールが発行したトークンだけを受け付ける。

resource "aws_cognito_user_pool" "this" {
  name = local.name

  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  deletion_protection      = var.cognito_deletion_protection ? "ACTIVE" : "INACTIVE"
  mfa_configuration        = var.cognito_mfa

  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  password_policy {
    minimum_length                   = 14
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = true
    temporary_password_validity_days = 3
  }

  # MFA を使うときは認証アプリ（TOTP）。SMS は使わない。
  dynamic "software_token_mfa_configuration" {
    for_each = var.cognito_mfa == "OFF" ? [] : [1]

    content {
      enabled = true
    }
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }
}

# アプリクライアント。シークレットなし（CLI・VS Code・SPA などの公開クライアント向け）。
resource "aws_cognito_user_pool_client" "this" {
  name         = "${local.name}-public"
  user_pool_id = aws_cognito_user_pool.this.id

  generate_secret               = false
  prevent_user_existence_errors = "ENABLED"
  enable_token_revocation       = true

  explicit_auth_flows = concat(
    ["ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"],
    var.cognito_allow_user_password_auth ? ["ALLOW_USER_PASSWORD_AUTH"] : [],
  )

  access_token_validity  = 60
  id_token_validity      = 60
  refresh_token_validity = 30

  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }

  # 認可コード + PKCE。コールバック URL を指定したときだけ有効にする。
  allowed_oauth_flows_user_pool_client = length(var.cognito_callback_urls) > 0
  allowed_oauth_flows                  = length(var.cognito_callback_urls) > 0 ? ["code"] : []
  allowed_oauth_scopes                 = length(var.cognito_callback_urls) > 0 ? ["openid", "email", "profile"] : []
  callback_urls                        = var.cognito_callback_urls
  supported_identity_providers         = ["COGNITO"]
}

# ログイン画面のドメイン（認可コード + PKCE を使うときだけ）。
resource "aws_cognito_user_pool_domain" "this" {
  count = var.cognito_domain_prefix == null ? 0 : 1

  domain       = var.cognito_domain_prefix
  user_pool_id = aws_cognito_user_pool.this.id
}
