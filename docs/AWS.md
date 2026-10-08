# AWS で動かす

`infra/aws/` の Terraform で、review-platform を AWS に作ります。
閉じたネットワーク（NAT なし）、最小権限、費用を抑える、の 3 つを方針にしています。

環境変数の名前は [CONFIG.md](CONFIG.md) が正です。Terraform はその名前のまま値を渡します。

> `terraform apply` と `terraform destroy` は、人（利用者）が内容を確かめてから自分で実行します。
> このドキュメントやツールが自動で実行することはありません。

## 全体の形

```
利用者（allowed_cidrs）
   │ HTTPS 443
   ▼
ALB（パブリックサブネット × 2 AZ）
   │ 8080
   ▼
ECS Fargate: api（プライベートサブネット）──→ S3 / DynamoDB / SQS
                                              │
ECS Fargate: worker（プライベートサブネット）←─┘ SQS の滞留で 0〜N 台
   │
   └─→ Bedrock（VPC エンドポイント経由）

プライベートサブネットからインターネットへの経路はない。
AWS のサービスへは VPC エンドポイントで届く。
```

## 作られるもの

| ファイル | 作られるもの | ひとこと |
|---|---|---|
| `network.tf` | VPC、パブリック / プライベートサブネット（各 2 AZ）、IGW、ルートテーブル、セキュリティグループ、VPC エンドポイント、フローログ | NAT ゲートウェイは作らない |
| `network.tf` | ゲートウェイ型エンドポイント: S3、DynamoDB | 無料。エンドポイントポリシーで、このバケット（と ECR のイメージ層の置き場）、ジョブの表とその GSI だけを通す |
| `network.tf` | インターフェース型エンドポイント: SQS、ECR（api / dkr）、CloudWatch Logs、STS、Bedrock Runtime、Secrets Manager、Cognito | Bedrock は `llm_provider = bedrock` のとき、Secrets Manager はシークレットを使うときだけ |
| `storage.tf` | S3 バケット | 公開ブロック、暗号化（SSE-S3 か SSE-KMS）、版管理、TLS 必須、`uploads/` と `results/` を `job_ttl_days` で削除 |
| `storage.tf` | DynamoDB テーブル | オンデマンド、パーティションキー `job_id`、GSI `owner-job-index`（`owner` + `job_id`）、TTL `expires_at`、PITR 有効 |
| `queue.tf` | SQS キューと DLQ | 5 回受信で DLQ へ（ワーカーは 3 回で failed にするので、その後の保険）。可視性タイムアウト 900 秒。SSE-SQS |
| `ecr.tf` | ECR リポジトリ | タグ上書き不可、push 時スキャン、最新 20 個を残す |
| `ecs.tf` | ECS クラスター、タスク定義 × 2、サービス × 2、オートスケーリング | Container Insights 有効。root 以外のユーザー、読み取り専用ルート、`/tmp` だけ書ける |
| `alb.tf` | ALB、ターゲットグループ、リスナー（443 / 80） | 80 は 443 へ転送するだけ |
| `iam.tf` | 実行ロール、API 用タスクロール、ワーカー用タスクロール | 必要な操作とリソースだけ |
| `auth.tf` | Cognito ユーザープール、アプリクライアント、（任意）ドメイン | 自己登録なし、強いパスワード、MFA は変数 |
| `secrets.tf` | （任意）Secrets Manager のシークレット | 入れ物だけ。値は Terraform の外で入れる |
| `monitoring.tf` | SNS トピック（メール）、アラーム、ログのメトリクスフィルター | DLQ、ALB / API の 5xx、健全なタスク数、ワーカーの ERROR |
| `budget.tf` | AWS Budgets | 月の予算。実績 80%・実績 100%・予測 100% でメール |

### コンテナに渡す環境変数

| 変数 | 値 |
|---|---|
| `RP_BACKEND` | `aws` |
| `RP_AUTH` | `jwt` |
| `RP_JWT_ISSUER` / `RP_JWT_JWKS_URL` | Cognito のプールから自動で作る |
| `RP_JWT_AUDIENCE` | アプリクライアント ID。Cognito のアクセストークンには `aud` がないので、アプリは `token_use = access` と `client_id` で確かめる |
| `AWS_REGION` / `RP_S3_BUCKET` / `RP_SQS_QUEUE_URL` / `RP_DDB_TABLE` | 作ったリソースから自動で入れる |
| `RP_LLM` / `RP_LLM_MODEL_DEEP` | 変数から（`llm_provider` / `llm_model_deep`）。`llm_model_deep` が空なら検証しない |
| `RP_BUDGET_PER_JOB_USD` / `RP_BUDGET_DAILY_USD` / `RP_BUDGET_DAILY_PER_OWNER_USD` | 変数から（`budget_per_job_usd` / `budget_daily_usd` / `budget_daily_per_owner_usd`）。既定はすべて 0 = 有料の LLM（Bedrock）を呼ばない |
| `RP_LLM_MODEL_STANDARD` | `llm_model_standard` を入れたときだけ（空ならアプリの既定値） |
| `RP_LLM_BASE_URL` | `llm_provider = anthropic` のときだけ（`llm_base_url`） |
| `RP_NOTIFY` | Slack のシークレットを有効にしたら `slack`、それ以外は `none` |
| `RP_LOG_LEVEL` / `RP_MAX_UPLOAD_MB` / `RP_ALLOWED_EXTENSIONS` / `RP_JOB_TTL_DAYS` / `RP_PUBLIC_BASE_URL` | 変数から |
| `RP_HOST` / `RP_PORT` | `0.0.0.0` / `8080` |
| `TMPDIR` / `HOME` | `/tmp`（ルートを読み取り専用にしているため。アプリの設定ではなく OS の決まり） |
| `ANTHROPIC_API_KEY` / `RP_SLACK_WEBHOOK_URL` | 有効にしたときだけ、Secrets Manager からワーカーに渡す |

同じ設定を API とワーカーの両方に渡します。役割の違いは起動コマンド（`review-platform api` / `review-platform worker`）だけです。

## 費用の目安（試算）

東京リージョン（ap-northeast-1）、x86、既定の変数で見積もった **試算** です。
単価は執筆時点の想定です。使う前に必ず AWS の料金ページで確かめてください。為替・税は含みません。

### 何もしていないとき（月額）

| 項目 | 前提 | 月額（USD） |
|---|---|---|
| インターフェース型 VPC エンドポイント | 7 個 × 2 AZ × 約 0.014/時 | 約 143 |
| ALB | 約 0.0243/時 + わずかな LCU | 約 20 |
| ALB のパブリック IPv4 | 2 個 × 約 0.005/時 | 約 7 |
| Fargate（api） | 0.5 vCPU / 1 GB × 1 台 × 常時 | 約 23 |
| Fargate（worker） | 最小 0 台 | 0 |
| CloudWatch（ログ・Container Insights・アラーム） | 少量 | 約 5 |
| S3 / DynamoDB / SQS / フローログ | ほぼ空 | 約 1 |
| Cognito | 小人数（無料の範囲を想定） | 0 |
| Secrets Manager | 使うときだけ、1 個 約 0.40 + エンドポイント 2 AZ で約 20 | 0（既定） |
| **合計** | | **約 200** |

いちばん大きいのは VPC エンドポイントです。安くしたいときは次を検討します。

- `interface_endpoint_az_count = 1` にする（エンドポイントが約半分の約 72 に。合計は約 130）。
- 使わない日は `terraform destroy` する。学習ならこれがいちばん安い。

### 100 ジョブあたり（追加分）

| 項目 | 前提 | 100 ジョブで（USD） |
|---|---|---|
| Bedrock（ふだんのモデル） | 1 ジョブ 入力 4 万トークン・出力 5 千トークン。想定単価 入力 3 / 出力 15（100 万トークンあたり） | 約 20 |
| Bedrock（上位モデルで検証。`llm_model_deep` を入れたときだけ） | 1 ジョブ 入力 2 万・出力 2 千。想定単価 入力 5 / 出力 25 | 約 15 |
| Fargate（worker） | 1 vCPU / 2 GB、1 ジョブ 5 分 + 台数を戻すまでの待ち | 約 1 |
| S3 / SQS / DynamoDB / ログ | 1 ジョブ 数 MB | 1 未満 |
| **合計** | | **約 36** |

- LLM の費用がほとんどです。モデルと文書の大きさで大きく変わります。
- **既定では LLM の費用は 0 です**。費用の上限 `budget_per_job_usd`・`budget_daily_usd`・`budget_daily_per_owner_usd`（`RP_BUDGET_*`）の既定は 0 で、0 のままだと Bedrock を一切呼びません（ジョブは変換だけして LLM を呼ばずに failed）。上の表の Bedrock の行は、上限を設定して AI レビューを使うときの試算です。
- AI レビューを使うときは 3 つとも 0 より大きい値を明示的に入れます（例: 1 ジョブ 1.0・1 日 10・利用者ごと 2）。LLM の費用は 1 日あたり最大で `budget_daily_usd` に収まります。
- 予算（`monthly_budget_usd`）を超えそうになったらメールが届きます。

## 前提

- AWS アカウントと、Terraform を実行できる権限（学習用アカウントを推奨）。
- Terraform 1.6 以上（または OpenTofu）、AWS CLI v2、Docker。
- 使うドメインと、そのドメインの **ACM 証明書**（同じリージョン、DNS 検証で発行済み）。
- Bedrock で使うモデルが、そのリージョンで使える状態になっていること。コンソールの「モデルアクセス」で確認します（Anthropic のモデルは、初回に利用目的の入力を求められることがあります）。
- 使う推論プロファイル ID と、IAM で許す ARN（推論プロファイルと、そのルーティング先の基盤モデル）。
- 自分の出口 IP（`allowed_cidrs` に入れる）。

## 手順

以下、`<...>` は自分の値に置き換えます。アカウント ID などの実際の値をリポジトリに入れないでください。

### 1. 変数を用意する

```bash
cd infra/aws
cp terraform.tfvars.example terraform.tfvars
# terraform.tfvars を編集する（.gitignore に入れておく）
```

`image` は手順 3 の `plan` の時点で入力チェックを通る値にしておきます（`<...>` のままだと止まります）。
ECR の URL は `<account-id>.dkr.ecr.<region>.amazonaws.com/<project_name>-<env>` になるので、push する予定のタグで先に書いておきます。

### 2. 初期化

```bash
terraform init
```

状態ファイルをチームで共有するときは、`versions.tf` の S3 バックエンドの例を使います。

### 3. 先に ECR だけ作る

イメージがないとサービスが起動できないので、ECR を先に作ります。

```bash
terraform plan  -target=aws_ecr_repository.this -target=aws_ecr_lifecycle_policy.this -out=ecr.tfplan
terraform apply ecr.tfplan   # 人が内容を確かめて実行する
terraform output -raw ecr_repository_url
```

### 4. イメージを作って push する

```bash
REPO=<ecr_repository_url の値>
TAG=<バージョンやコミット ID。latest は使わない>

aws ecr get-login-password --region ap-northeast-1 \
  | docker login --username AWS --password-stdin "${REPO%%/*}"

# Dockerfile はリポジトリの直下にある（いまは infra/aws にいる）
docker build --platform linux/amd64 -t "$REPO:$TAG" ../..
docker push "$REPO:$TAG"
```

タグは上書きできません（IMMUTABLE）。作り直すときは新しいタグにします。
`terraform.tfvars` の `image` を `"$REPO:$TAG"` に合わせます。ARM64 で作るときは `--platform linux/arm64` でビルドし、`cpu_architecture = "ARM64"` にします。

### 5. 全体を作る

```bash
terraform plan -out=tfplan
terraform apply tfplan   # 人が内容を確かめて実行する
```

終わったら次をします。

- 届いた **SNS の確認メール** のリンクを開く（開くまでアラームは届きません）。
- DNS で、証明書のドメインを `terraform output -raw alb_dns_name` に向ける（CNAME、または Route 53 のエイリアス）。

### 6.（使うときだけ）シークレットに値を入れる

```bash
aws secretsmanager put-secret-value \
  --secret-id "$(terraform output -raw slack_webhook_url_secret_arn)" \
  --secret-string '<webhook-url>'

# 値を入れたあと、ワーカーを入れ替える
aws ecs update-service --cluster "$(terraform output -raw ecs_cluster_name)" \
  --service worker --force-new-deployment
```

値が入るまで、ワーカーのタスクは起動に失敗します。
Slack や Anthropic API は外への通信が必要です。閉域のままでは届きません（既知の妥協を参照）。

### 7. Cognito にユーザーを作る

自己登録はできません。管理者が作ります。

```bash
POOL_ID=$(terraform output -raw cognito_user_pool_id)

aws cognito-idp admin-create-user --user-pool-id "$POOL_ID" \
  --username <user@example.com> \
  --user-attributes Name=email,Value=<user@example.com> Name=email_verified,Value=true \
  --message-action SUPPRESS

# 14 文字以上、大文字・小文字・数字・記号を含む
aws cognito-idp admin-set-user-password --user-pool-id "$POOL_ID" \
  --username <user@example.com> --password '<strong-password>' --permanent
```

### 8. トークンを取る

`cognito_allow_user_password_auth = true` のときの、CLI での取り方です。

```bash
CLIENT_ID=$(terraform output -raw cognito_app_client_id)

TOKEN=$(aws cognito-idp initiate-auth \
  --auth-flow USER_PASSWORD_AUTH \
  --client-id "$CLIENT_ID" \
  --auth-parameters USERNAME=<user@example.com>,PASSWORD='<strong-password>' \
  --query 'AuthenticationResult.AccessToken' --output text)
```

- アクセストークンの有効期限は 60 分です。
- `--auth-parameters` はカンマ区切りです。パスワードにカンマが入るときは JSON 形式（`--auth-parameters '{"USERNAME":"<user@example.com>","PASSWORD":"<strong-password>"}'`）で渡します。
- MFA を設定したユーザーは、追加の手順（`respond-to-auth-challenge`）が要ります。
- 本番では `cognito_allow_user_password_auth = false` にし、ログイン画面（認可コード + PKCE、`cognito_domain_prefix` と `cognito_callback_urls`）を使います。

### 9. API を呼ぶ

```bash
BASE=$(terraform output -raw api_base_url)

# ヘルスチェック（認証なし）
curl -sS "$BASE/healthz"
# => {"status":"ok"}

# MCP に認証付きでつながるか（initialize を送る）
curl -sS -X POST "$BASE/mcp" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'

# トークンなしなら 401 になることも確かめる
curl -sS -o /dev/null -w '%{http_code}\n' -X POST "$BASE/mcp"
```

- `public_base_url` を入れたときは、MCP はその Host 以外からの要求を断ります（DNS リバインディング対策）。ALB の DNS 名ではなく、証明書のドメインで呼びます。

ジョブの投入や結果の取得の API は、アプリの API の説明を見てください。どれも `Authorization: Bearer $TOKEN` を付けて呼びます。

ログは次で見られます。ロググループ名は `/ecs/<project_name>-<env>/api` と `/ecs/<project_name>-<env>/worker` です（`terraform output log_group_names` で確かめられます）。

```bash
aws logs tail "/ecs/review-platform-<env>/api" --follow
aws logs tail "/ecs/review-platform-<env>/worker" --follow
```

### 10. VS Code から MCP につなぐ

ワークスペースの `.vscode/mcp.json` に書きます。トークンは起動時に入力させ、ファイルには書きません。

```json
{
  "inputs": [
    {
      "type": "promptString",
      "id": "rp-token",
      "description": "review-platform のアクセストークン",
      "password": true
    }
  ],
  "servers": {
    "review-platform": {
      "type": "http",
      "url": "https://<your-domain>/mcp",
      "headers": {
        "Authorization": "Bearer ${input:rp-token}"
      }
    }
  }
}
```

トークンは 60 分で切れます。切れたら手順 8 で取り直し、MCP サーバーを再起動します。
接続元の IP が `allowed_cidrs` に入っている必要があります。

### 11. 片付ける

```bash
terraform destroy   # 人が内容を確かめて実行する
```

- バケットや ECR に中身が残っていると消せません。学習用は `force_destroy = true` にしてから `apply`、そのあと `destroy` します。
- `dynamodb_deletion_protection`・`alb_deletion_protection`・`cognito_deletion_protection` が `true` なら、先に `false` にして `apply` します。
- シークレットは `secret_recovery_window_days` の日数だけ残ります（0 ならすぐ消える）。
- ACM 証明書と DNS のレコードは Terraform の外で作ったものなので、手で消します。

## AWS SAA の出題範囲との対応

| SAA の分野 | この構成のどこか |
|---|---|
| セキュアなアーキテクチャ: ネットワーク | パブリック / プライベートサブネットの分離、NAT なしの閉域、VPC エンドポイント（ゲートウェイ型とインターフェース型の違い）、セキュリティグループの参照による許可 |
| セキュアなアーキテクチャ: IAM | 実行ロールとタスクロールの分離、リソースを絞ったポリシー、`aws:SourceAccount` / `aws:SourceArn` による混乱した代理の防止、エンドポイントポリシー |
| セキュアなアーキテクチャ: データ保護 | S3 の公開ブロック・暗号化（SSE-S3 / SSE-KMS）・TLS 必須のバケットポリシー、Secrets Manager、ACM と ALB の TLS 終端 |
| セキュアなアーキテクチャ: 認証 | Cognito ユーザープール、JWT（発行者・対象・JWKS）、MFA |
| 弾力性のあるアーキテクチャ | 2 AZ、ALB のヘルスチェック、SQS による疎結合、DLQ、可視性タイムアウト、ECS のデプロイのサーキットブレーカー |
| 高性能なアーキテクチャ | SQS の滞留によるステップスケーリング、Fargate、DynamoDB オンデマンドと GSI（`owner-job-index` で利用者ごとの一覧を新しい順に取る）、ロングポーリング |
| コスト最適化 | NAT を使わない、ワーカー 0 台まで縮小、Fargate Spot（任意）、S3 ライフサイクル、DynamoDB TTL、ECR ライフサイクル、AWS Budgets |
| 運用 | CloudWatch Logs・メトリクスフィルター・アラーム・SNS、Container Insights、VPC フローログ、PITR |

## 既知の妥協

セキュリティ検査ツール（tfsec / trivy config など）で指摘されうる点と、そうした理由です。

| 項目 | 内容 | 理由 | 本番で強めるなら |
|---|---|---|---|
| ALB がインターネット向け | `internal = false` | 利用者がインターネット経由で使うため | `allowed_cidrs` を最小にする。社内からだけなら内部 ALB と VPN / Direct Connect |
| HTTP（80）のリスナー | 80 を開けている | HTTPS へ転送するためだけ。中身は返さない | 不要なら `aws_vpc_security_group_ingress_rule.alb_http` と `aws_lb_listener.http_redirect` を消す |
| WAF なし | AWS WAF を付けていない | 費用と、送信元 IP を絞っているため | AWS WAF のマネージドルールを ALB に付ける |
| ALB のアクセスログなし | 記録していない | ログ用バケットの設定が増えるため | ログ用の S3 バケットを作り `access_logs` を有効にする |
| S3 のアクセスログなし | サーバーアクセスログを取っていない | 費用と構成を簡単にするため | ログ用バケットか CloudTrail のデータイベント |
| 暗号化キーが AWS 管理 | CloudWatch Logs、ECR、DynamoDB、SQS、Secrets Manager、S3（既定）は AWS 管理のキー | KMS の CMK は 1 個 月 1 USD 程度かかり、キーポリシーの管理も増えるため | CMK を作り各リソースに設定する（S3 は `s3_kms_key_arn` で切り替え可能） |
| SNS トピックを暗号化していない | `aws_sns_topic.alarms` に KMS なし | AWS 管理キー（aws/sns）では CloudWatch アラームから送れないため。中身はアラームの文面だけ | CMK を作り、キーポリシーで `cloudwatch.amazonaws.com` に許可する |
| ポリシーに `*` がある | `ecr:GetAuthorizationToken` のリソース、エンドポイントポリシーのプリンシパル | AWS の仕様で絞れないため。エンドポイントポリシーはリソースで絞っている | なし |
| JWKS への経路 | API は Cognito の JWKS（`https://cognito-idp.<region>.amazonaws.com/<pool-id>/.well-known/jwks.json`）を取りに行く。NAT がないので Cognito のインターフェース型エンドポイント経由にしている | 閉域のまま認証するため | **最初の apply 後に必ず確認する**。取れないときは `enable_cognito_idp_endpoint = false` にし、`extra_egress_cidrs` で社内プロキシか、自分で用意した NAT を通す |
| Slack・Anthropic API に届かない | 閉域なので外へ出られない | NAT を作らない方針のため | 社内プロキシや NAT を用意し `extra_egress_cidrs` で許す。または VPC 内から届く通知先を使う |
| エンドポイントの費用 | インターフェース型を 2 AZ に置く | 片方の AZ の障害に備えるため | 学習用は `interface_endpoint_az_count = 1` |
| 予算がアカウント全体 | タグで絞っていない | コスト配分タグの有効化が別に要るため | `Project` タグをコスト配分タグにして、予算にフィルターを付ける |
| USER_PASSWORD_AUTH | 例の tfvars で有効にしている | CLI でトークンを取る学習のため | `false` にして、ログイン画面（認可コード + PKCE）を使う |
| Cognito の脅威保護なし | 高度なセキュリティ機能を使っていない | 上位の料金プランが要るため | 必要なら有効にする |
| コンテナのヘルスチェックなし | タスク定義に `healthCheck` がない | ECS は Dockerfile の `HEALTHCHECK` を使わない。API は ALB のヘルスチェック（`/healthz`）で見ている | Dockerfile と同じ python のコマンドで、タスク定義に `healthCheck` を足す |
| ワーカーの Spot | `worker_use_spot = true` だと途中で止まることがある | 安くするための選択肢。既定は false | 止まったジョブは可視性タイムアウト後に別のタスクが引き継ぐ。3 回で failed |
| ログの保存期間 | 既定 30 日 | 費用を抑えるため | 監査の要件に合わせて `log_retention_days` を延ばす |

## 変数の一覧

主な変数です。全部は `infra/aws/variables.tf` を見てください（どれにも説明と入力チェックがあります）。

| 変数 | 既定 | 意味 |
|---|---|---|
| `env` | （必須） | 環境名 |
| `image` | （必須） | イメージの URI（タグかダイジェスト。latest は不可） |
| `allowed_cidrs` | （必須） | ALB に接続してよい CIDR |
| `acm_certificate_arn` | （必須） | ACM 証明書 |
| `notification_email` | （必須） | アラームと予算の通知先 |
| `public_base_url` | 空 | `RP_PUBLIC_BASE_URL`。証明書のドメイン（`https://<your-domain>`）。MCP はこの Host だけを受け付ける |
| `llm_provider` | `bedrock` | `RP_LLM` |
| `llm_model_standard` / `llm_model_deep` | 空 | 推論プロファイル ID |
| `bedrock_model_arns` | `[]` | ワーカーが呼んでよい Bedrock の ARN |
| `job_ttl_days` | 30 | S3 のライフサイクルと `RP_JOB_TTL_DAYS` |
| `worker_min_count` / `worker_max_count` | 0 / 3 | ワーカーの台数 |
| `interface_endpoint_az_count` | 2 | エンドポイントを置く AZ の数 |
| `monthly_budget_usd` | 300 | 月の予算 |
