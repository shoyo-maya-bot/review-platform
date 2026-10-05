# Azure に置く

review-platform を Azure で動かすための手順と設計です。Terraform は `infra/azure/` にあります。
アプリに渡す環境変数は [CONFIG.md](CONFIG.md) の契約どおりです。

方針は 4 つです。

- **閉じたネットワーク**：ストレージは公開アクセスを止め、プライベートエンドポイントだけで使う。
- **最小の権限**：アプリはマネージド ID で動く。共有キーや接続文字列は使わない。権限はコンテナ・キュー・テーブル単位で付ける。
- **費用を抑える**：使わないときは 0 台まで減らす。高い SKU は変数で選べるようにする。
- **学びやすい**：1 ファイル 1 テーマ。コメントに「なぜそうするか」を書く。

> `terraform apply` と `terraform destroy` は、人（利用者）が内容を確かめてから自分で実行します。
> このドキュメントやツールが自動で実行することはありません。

## 全体の形

```text
             利用者（curl / VS Code の MCP）
                     │ HTTPS + Entra ID の JWT
                     ▼
┌──────────── VNet ─────────────────────────────────────────────┐
│ snet-aca（/23, Microsoft.App/environments に委任）               │
│   Container Apps 環境（ワークロードプロファイル: Consumption）    │
│     ├─ api    : review-platform api（外部 ingress, IP 許可リスト）│
│     └─ worker : review-platform worker（ingress なし, KEDA）      │
│                    │ マネージド ID                               │
│ snet-pe            ▼                                             │
│   PE: blob / queue / table ──► Storage（公開アクセスなし）       │
│   PE: vault ─────────────────► Key Vault（RBAC）                 │
│   PE: registry（任意, Premium）► ACR                              │
│ プライベート DNS ゾーン（privatelink.*）を VNet にリンク          │
└───────────────────────────────────────────────────────────────┘
        │ 送信（インターネット）
        ▼
  LLM の窓口（Microsoft Foundry など、Messages API と同じ形）
```

## 作るもの

| ファイル | リソース | 役目 |
|---|---|---|
| `main.tf` | リソースグループ | 全部をまとめる。タグ `Project` と `Environment` を全リソースに付ける |
| `network.tf` | VNet、サブネット 2 つ、プライベート DNS ゾーン | `snet-aca`（/23、委任あり）と `snet-pe`。`privatelink.blob/queue/table.core.windows.net` ほか |
| `storage.tf` | ストレージアカウント、コンテナ `review`、キュー `jobs` と `jobs-poison`、テーブル `jobs`、ライフサイクル、PE 3 つ | 原本・結果・ジョブ。TLS 1.2 以上、共有キー無効、公開アクセス無効、バージョニングと論理削除 |
| `identity.tf` | ユーザー割り当てマネージド ID、ロールの割り当て | api と worker が共用。Blob/Queue/Table のデータ共同作成者、AcrPull、Key Vault Secrets User |
| `registry.tf` | ACR（既定 Basic）、PE（任意） | イメージ置き場。管理者ユーザーは無効 |
| `keyvault.tf` | Key Vault、PE（既定あり） | API キーなど。RBAC、削除保護、論理削除。シークレットの値は Terraform で作らない |
| `monitoring.tf` | Log Analytics、アクショングループ、アラート 3 つ、診断設定 | ERROR ログ、`jobs-poison` への投入、キューの滞留を知らせる |
| `containerapps.tf` | Container Apps 環境、api、worker、内部 DNS（任意） | `deploy_apps = true` のときだけアプリを作る |
| `budget.tf` | 予算（リソースグループ） | 月の費用が 80%・100%（と予測 100%）でメール |
| `outputs.tf` | 出力 | ACR のログインサーバー、API と MCP の URL など |

Container Apps 環境は、基盤用のリソースグループ `rg-<prefix>-aca-infra` を自動で作ります（ロードバランサーや公開 IP が入ります）。`<prefix>` は `rp-<env>` です。

キューは **Storage Queue**、ジョブの表は **Table Storage** です（Service Bus・Cosmos DB は使いません。理由は [ARCHITECTURE.md](ARCHITECTURE.md)）。

### コンテナに渡す環境変数

| 変数 | 値 |
|---|---|
| `RP_BACKEND` | `azure` |
| `RP_AUTH` | `jwt` |
| `RP_JWT_ISSUER` / `RP_JWT_AUDIENCE` / `RP_JWT_JWKS_URL` | 変数 `jwt_issuer` / `jwt_audience` / `jwt_jwks_url` から |
| `RP_AZURE_BLOB_URL` / `RP_AZURE_QUEUE_URL` / `RP_AZURE_TABLE_URL` | ストレージアカウントの各エンドポイント（末尾の `/` なし） |
| `RP_AZURE_CONTAINER` / `RP_AZURE_QUEUE_NAME` / `RP_AZURE_TABLE_NAME` | `review` / `jobs` / `jobs` |
| `AZURE_CLIENT_ID` | マネージド ID のクライアント ID（Azure SDK の `DefaultAzureCredential` が読む） |
| `RP_LLM` / `RP_LLM_BASE_URL` / `RP_LLM_MODEL_STANDARD` / `RP_LLM_MODEL_DEEP` / `RP_BUDGET_PER_JOB_USD` | 変数 `llm_mode` / `llm_base_url` / `llm_model_standard` / `llm_model_deep` / `budget_per_job_usd` から |
| `RP_NOTIFY` | 変数 `notify` から |
| `RP_LOG_LEVEL` / `RP_MAX_UPLOAD_MB` / `RP_JOB_TTL_DAYS` | 変数 `log_level` / `max_upload_mb` / `job_ttl_days` から |
| `RP_PUBLIC_BASE_URL` | `public_base_url`。空なら api の URL（`https://<api の FQDN>`） |
| `RP_HOST` / `RP_PORT` | `0.0.0.0` / `8080` |
| `ANTHROPIC_API_KEY` | `llm_mode = "anthropic"` のときだけ、Key Vault の `anthropic-api-key` から |
| `RP_SLACK_WEBHOOK_URL` | `notify = "slack"` のときだけ、Key Vault の `slack-webhook-url` から |

`RP_ALLOWED_EXTENSIONS` は渡していません（アプリの既定値を使う）。api と worker に同じ値を渡します。

## 費用の試算（月額の目安）

東日本リージョン、従量課金、2026 年時点の公開価格をもとにした **目安** です。為替は 1 ドル 150 円で計算しています。実際の金額は Azure の料金計算ツールで確かめてください。

### 使っていないとき（既定の設定）

| もの | 前提 | 月額の目安 |
|---|---|---|
| プライベートエンドポイント | 4 つ（blob / queue / table / vault）× 約 $7.3 | 約 $29 |
| プライベート DNS ゾーン | 4 つ × $0.5 | 約 $2 |
| ACR Basic | 1 日 約 $0.17 | 約 $5 |
| Container Apps 環境の公開 IP | 基盤のリソースグループに 1 つ | 約 $4 |
| Container Apps（api・worker） | 最小 0 台。動いていない時間は課金なし | $0 |
| Log Analytics | 取り込みが少ない。保持 30 日は無料 | $0〜1 |
| アラート | ログ検索アラート 2 つ（15 分ごと）、メトリックアラート 1 つ | 約 $1〜2 |
| Storage・Key Vault | 容量も操作も少ない | $1 未満 |
| **合計** | | **約 $40〜45（6,000〜7,000 円）** |

いちばん大きいのはプライベートエンドポイントです。学習で短期間だけ使うなら、使い終わったらすぐ `destroy` してください。
Key Vault の PE をやめる（`key_vault_private_endpoint_enabled = false`）と約 $7 減ります（その代わり Key Vault の公開エンドポイントを使います）。
閉じたネットワークを強める設定（ACR Premium と PE）では、ACR が約 $50 になり、PE と DNS ゾーンが 1 つずつ増えます。

### 100 ジョブあたり

| もの | 前提 | 目安 |
|---|---|---|
| worker | 1 vCPU・2 GiB。1 ジョブで 3 分動く（ほとんどは LLM の応答待ち）。100 ジョブで 18,000 vCPU 秒・36,000 GiB 秒 | 月の無料枠（180,000 vCPU 秒・360,000 GiB 秒）の中なら $0。枠を使い切っていれば約 $0.5 |
| api | 0.5 vCPU・1 GiB。受付と結果の取得だけ | ほぼ $0 |
| Storage | 1 ジョブ 5 MB（原本と結果）。操作は数十回 | $0.01 未満 |
| Log Analytics | 1 ジョブ 1 MB のログ | 約 $0.3 |
| **LLM** | 1 ジョブの入力・出力のトークン数 × 単価。単価は Foundry（または使う窓口）の価格表で確認 | **ここがほとんど。上限は `RP_BUDGET_PER_JOB_USD`（既定 $1）× 100 = $100** |

LLM 以外は 100 ジョブで $1 前後です。費用の管理は、アプリの 1 ジョブ上限（`budget_per_job_usd`）と、Azure の予算アラート（`budget_amount`）の 2 段で行います。

## 前提

- Azure のサブスクリプション。権限は「所有者」、または「共同作成者」と「ユーザーアクセス管理者」（ロールの割り当てを作るため）。
- 手元の道具：Azure CLI（`az`）、Terraform 1.6 以上（OpenTofu でも可）、Docker。
- リソースプロバイダーの登録（初めてのサブスクリプションだけ）。

```bash
for ns in Microsoft.App Microsoft.ContainerRegistry Microsoft.KeyVault Microsoft.Storage \
          Microsoft.Network Microsoft.OperationalInsights Microsoft.Insights \
          Microsoft.ManagedIdentity Microsoft.Consumption; do
  az provider register --namespace "$ns"
done
```

### Entra ID のアプリ登録（JWT 用）

この Terraform はアプリ登録を作りません（azuread プロバイダとテナントの管理者権限が要るため）。手で 1 回だけ作ります。

```bash
TENANT_ID=$(az account show --query tenantId -o tsv)

# 1. API 側のアプリ登録を作る
APP_ID=$(az ad app create --display-name "review-platform-api" \
  --sign-in-audience AzureADMyOrg --query appId -o tsv)

# 2. アプリケーション ID URI を付け、v2 のアクセストークンを出すようにする
az ad app update --id "$APP_ID" --identifier-uris "api://$APP_ID"
OBJECT_ID=$(az ad app show --id "$APP_ID" --query id -o tsv)
az rest --method PATCH \
  --uri "https://graph.microsoft.com/v1.0/applications/$OBJECT_ID" \
  --body '{"api":{"requestedAccessTokenVersion":2}}'

# 3. サービスプリンシパルを作る
az ad sp create --id "$APP_ID"
```

続きはポータルで行います（JSON を手で書くより間違いが少ないため）。

1. 「Microsoft Entra ID」→「アプリの登録」→ `review-platform-api` →「API の公開」。
2. 「スコープの追加」で `access_as_user` を作る（同意できるのは「管理者とユーザー」）。
3. 「承認済みのクライアント アプリケーション」に Azure CLI（クライアント ID `04b07795-8ddb-461a-bbee-02f9e1bf7b46`）を追加し、上のスコープにチェックを入れる。これで `az account get-access-token` で試験用のトークンが取れます。

`terraform.tfvars` には次の値を入れます。

| 変数 | 値 |
|---|---|
| `jwt_issuer` | `https://login.microsoftonline.com/<TENANT_ID>/v2.0` |
| `jwt_audience` | `<APP_ID>`（v2 のトークンでは `aud` がクライアント ID になる） |
| `jwt_jwks_url` | `https://login.microsoftonline.com/<TENANT_ID>/discovery/v2.0/keys` |

## デプロイ

アプリ（Container App）は、イメージとシークレットがそろっていないと作成に失敗します。そこで **2 回に分けて** `apply` します。

### 1. 土台を作る（`deploy_apps = false`）

```bash
cd infra/azure
cp terraform.tfvars.example terraform.tfvars   # <your-value> を書き換える
az login
az account set --subscription "<サブスクリプション ID>"

terraform init
terraform plan -out tfplan
terraform apply tfplan      # 人が中身を確かめてから実行する
```

### 2. イメージを ACR に push する

```bash
ACR=$(terraform output -raw acr_name)
LOGIN=$(terraform output -raw acr_login_server)
TAG=<image_tag と同じ値>

az acr login --name "$ACR"
# Dockerfile はリポジトリの直下にある（いまは infra/azure にいる）
docker build --platform linux/amd64 -t "$LOGIN/review-platform:$TAG" ../..
docker push "$LOGIN/review-platform:$TAG"
```

- リポジトリ名 `review-platform` は変数 `image_repository` の既定値です。変えたときは合わせます。
- `terraform output -raw image` で、Container Apps が使うイメージの名前を確かめられます。
- ACR Premium と PE を使う設定では、`admin_cidrs` に入れた端末からだけ push できます。

### 3. Key Vault にシークレットを入れる

値をシェルの履歴に残さないように、ファイルから読みます。

```bash
KV=$(terraform output -raw key_vault_name)
terraform output key_vault_secret_names   # 入れる必要がある名前

read -rs KEY && printf '%s' "$KEY" > /tmp/key.txt && unset KEY
az keyvault secret set --vault-name "$KV" --name anthropic-api-key --file /tmp/key.txt
rm -f /tmp/key.txt
# notify = "slack" のときは slack-webhook-url も同じように入れる
```

- 実行した人には `Key Vault Secrets Officer` が付きます（`grant_runner_key_vault_secrets_officer = true` のとき）。
- Key Vault は `admin_cidrs` 以外からの公開アクセスを拒否します。自分の IP を入れておいてください。
- ロールの割り当てが効くまで数分かかることがあります。`Forbidden` が出たら少し待ちます。

### 4. アプリを作る（`deploy_apps = true`）

```bash
# terraform.tfvars の deploy_apps を true に変える
terraform plan -out tfplan
terraform apply tfplan
terraform output api_url
```

イメージを新しくするときは、`image_tag` を変えて `plan` と `apply` をします。

### Container Apps から LLM の窓口（Foundry）へ届く道

- worker は `RP_LLM_BASE_URL`（例 `https://<リソース名>.services.ai.azure.com/anthropic`）へ、HTTPS で直接つなぎます。
- 通り道は Container Apps 環境の送信（インターネット経由）です。認証は Key Vault から渡す `ANTHROPIC_API_KEY` です。
- モデル（デプロイ）は Foundry 側で別に用意し、デプロイ名を `llm_model_standard` と `llm_model_deep` に入れます。この Terraform はモデルを作りません。
- 窓口を閉じたネットワークに寄せたいときは、Foundry にプライベートエンドポイントを付け、DNS ゾーンを VNet にリンクします（既知の妥協を参照）。

### シークレットを入れ替えたとき

Container App はバージョンなしの ID で参照しています。新しい値は自動で取り直されますが、すぐ反映したいときはリビジョンを再起動します。

アプリ名は `ca-rp-<env>-api` と `ca-rp-<env>-worker` です（`env = "dev"` なら `ca-rp-dev-worker`）。

```bash
RG=$(terraform output -raw resource_group_name)
APP=ca-rp-<env>-worker
az containerapp revision restart -g "$RG" -n "$APP" \
  --revision "$(az containerapp revision list -g "$RG" -n "$APP" --query '[0].name' -o tsv)"
```

## 動作の確認

### curl

```bash
API=$(terraform output -raw api_url)

# ヘルスチェック（認証なし）
curl -sS "$API/healthz"          # {"status":"ok"}

# トークンを取る（Azure CLI を承認済みクライアントにしてあること）
TOKEN=$(az account get-access-token \
  --scope "api://<APP_ID>/access_as_user" --query accessToken -o tsv)

# MCP の初期化を試す（Streamable HTTP）
curl -sS -X POST "$API/mcp" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
```

- `<APP_ID>` はアプリ登録のアプリケーション（クライアント）ID です。v2 のトークンでは `aud` がこの ID になり、`RP_JWT_AUDIENCE` と比べられます。
- `api_min_replicas = 0` のときは、最初の 1 回だけ起動を待つので遅くなります。
- `public_base_url` を入れたときは、MCP はその Host 以外からの要求を断ります（DNS リバインディング対策）。そのドメインで呼びます。

### VS Code の MCP

`.vscode/mcp.json` に次のように書きます。トークンは 1 時間ほどで切れるので、切れたら入れ直します。

```json
{
  "inputs": [
    {
      "type": "promptString",
      "id": "rp-token",
      "description": "az account get-access-token で取ったトークン",
      "password": true
    }
  ],
  "servers": {
    "review-platform": {
      "type": "http",
      "url": "https://<api の FQDN>/mcp",
      "headers": { "Authorization": "Bearer ${input:rp-token}" }
    }
  }
}
```

### ログと台数

```bash
RG=$(terraform output -raw resource_group_name)
az containerapp logs show -g "$RG" -n ca-rp-<env>-api --follow
az containerapp replica list -g "$RG" -n ca-rp-<env>-worker -o table   # キューが積まれると増える
```

Log Analytics では次のように探せます。

```kusto
ContainerAppConsoleLogs_CL
| where ContainerAppName_s startswith "ca-rp-"
| project TimeGenerated, ContainerAppName_s, Log_s
| order by TimeGenerated desc
```

## 片付け

```bash
terraform destroy    # 人が中身を確かめてから実行する
```

- Key Vault は削除保護つきです。消しても 7 日間は論理削除の状態で残り、完全には消せません（purge できない）。同じ名前で `apply` すると、消した Key Vault が復元されます（`recover_soft_deleted_key_vaults = true`）。まっさらで作り直すなら `name_suffix` を変えます。
- `prevent_deletion_if_contains_resources = true` なので、手で足したリソースが残っているとリソースグループは消えません。
- Container Apps 環境の基盤用リソースグループは、環境と一緒に消えます。
- Entra ID のアプリ登録は手で消します（`az ad app delete --id <APP_ID>`）。

## AWS と Azure の対応

| 役目 | AWS | Azure（この構成） |
|---|---|---|
| コンテナの実行 | ECS on Fargate | Container Apps（Consumption） |
| 入口・負荷分散 | ALB | Container Apps の ingress（Envoy） |
| 送信元の制限 | セキュリティグループ / WAF | ingress の IP 許可リスト |
| ファイルの保存 | S3 | Blob Storage |
| ジョブのキュー | SQS | Storage Queue |
| デッドレターキュー | SQS の DLQ（設定だけ） | `jobs-poison`（アプリが移す） |
| ジョブの表 | DynamoDB（TTL あり。GSI `owner-job-index` で利用者ごとの一覧） | Table Storage（TTL なし。一覧は `owner` で絞り込む） |
| 期限切れの削除 | S3 ライフサイクル・DynamoDB TTL | Blob ライフサイクル・ワーカーが表の行を消す |
| 実行時の権限 | IAM タスクロール | ユーザー割り当てマネージド ID ＋ RBAC |
| 秘密情報 | Secrets Manager | Key Vault |
| イメージ置き場 | ECR | ACR |
| 閉じた接続 | VPC エンドポイント（PrivateLink） | プライベートエンドポイント ＋ プライベート DNS ゾーン |
| キューで台数を増減 | Application Auto Scaling（SQS の滞留） | KEDA（azure-queue） |
| ログ | CloudWatch Logs | Log Analytics |
| アラート | CloudWatch アラーム ＋ SNS | Azure Monitor アラート ＋ アクショングループ |
| 予算 | AWS Budgets | Cost Management の予算 |
| 利用者の認証 | Cognito | Entra ID |
| LLM | Bedrock | Microsoft Foundry（Messages API と同じ形の窓口） |

## AZ-104 / AZ-305 との対応

| 試験 | 分野 | この構成で触れるところ |
|---|---|---|
| AZ-104 | ID とガバナンス | マネージド ID、RBAC の範囲（コンテナ・キュー単位）、タグ、予算 |
| AZ-104 | ストレージ | 冗長化（LRS）、共有キーの無効化、バージョニング、論理削除、ライフサイクル管理 |
| AZ-104 | コンピューティング | Container Apps、ACR、イメージの pull をマネージド ID で行う |
| AZ-104 | 仮想ネットワーク | サブネットの委任、プライベートエンドポイント、プライベート DNS ゾーンと VNet リンク |
| AZ-104 | 監視 | Log Analytics、診断設定、ログ検索アラート、メトリックアラート、アクショングループ |
| AZ-305 | ID・ガバナンス・監視の設計 | Entra ID で API を守る設計、ログの保持と取り込み上限、予算の通知 |
| AZ-305 | データストレージの設計 | Blob・Storage Queue・Table Storage の使い分け（Service Bus・Cosmos DB との比較）、TTL の代わりの手段 |
| AZ-305 | 事業継続の設計 | LRS を選んだ理由（学習用）、論理削除で戻せる範囲 |
| AZ-305 | インフラの設計 | サーバーレスのコンテナ、キュー駆動の台数増減、閉じたネットワーク、費用と安全のつり合い |

## 既知の妥協

| 項目 | いまの形 | 理由 | 強めるなら |
|---|---|---|---|
| ACR の公開エンドポイント | Basic のため開いている（管理者ユーザーは無効、pull は AcrPull だけ） | Premium は月 $50 ほどかかる | `acr_sku = "Premium"`、`acr_private_endpoint_enabled = true` |
| Key Vault の公開エンドポイント | 開いているが `admin_cidrs` 以外は拒否 | 手元からシークレットを入れるため | `key_vault_public_network_access_enabled = false`。入れるときは VNet の中の端末から |
| ストレージを手元から見られない | 公開アクセスを止めている | 閉じたネットワークを優先 | 見るときは VNet の中の踏み台（Bastion など）を使う。急ぎなら一時的に `az storage account update --public-network-access Enabled` と IP 規則を足し、終わったら戻す |
| コンテナ・キュー・テーブルの作成 | `storage_account_id` を使い、管理プレーン（ARM）で作る。`features.storage.data_plane_available = false` | 公開アクセスを止めても Terraform から作れるように | ー（azurerm 4.77 以上が必要。`queue_properties` や `static_website` は使えない） |
| Storage Queue に DLQ がない | アプリが `dequeue_count > 5` のメッセージを `jobs-poison` へ移す | サービスにその機能がない | ー |
| Table Storage に TTL がない | Blob はライフサイクルで消える。テーブルの行は、ワーカーが手すきのとき（1 時間に 1 回まで）に `expires_at` を過ぎたものを消す | サービスにその機能がない | ワーカーが 0 台の間は消えない。確実に消すなら、定期実行のジョブ（Container Apps Jobs など）を足す |
| KEDA の認証 | `custom_scale_rule` の `identity_id`（マネージド ID）で読む | 共有キーが無効なので接続文字列が使えない | KEDA がキューを読めない（台数が 0 のまま）ときは `worker_min_replicas = 1` にする |
| Key Vault の参照 | Container Apps が PE 経由でマネージド ID を使って読む | 値を Terraform の状態に残さない | 読めないときは PE と DNS リンク、ロールの割り当て（Secrets User）を確かめる |
| 送信の通り道 | 環境の既定の送信（固定 IP ではない） | NAT Gateway や Firewall は費用がかかる | NAT Gateway で固定 IP、Azure Firewall で宛先を絞る |
| LLM の窓口 | インターネット経由、API キー | モデルの用意はこの Terraform の外 | Foundry に PE を付け、Entra ID の認証にする |
| サブネットの NSG | 付けていない | ingress の許可リストと PE で足りると判断 | 要件に合わせて NSG を付ける（Container Apps の必要な通信は公式の一覧を見る） |
| マネージド ID が 1 つ | api と worker で共用 | 構成を簡単にするため | 役割ごとに ID を分け、api から `jobs-poison` の権限を外す |
| 2 回に分けた apply | `deploy_apps` で切り替え | イメージとシークレットがないとアプリの作成に失敗する | CI で push → シークレット確認 → apply の順に流す |
| 最小 0 台 | 使わないと費用 0、最初の応答が遅い | 費用を優先 | `api_min_replicas = 1`（待機中も少し課金される） |
| アラートの粒度 | ERROR は文字列一致。キューの滞留はアカウント全体で 1 時間ごと | Storage のメトリックはキューごとに分かれない | `jobs-poison` への投入はリソースログ（`StorageQueueLogs`）で個別に検知している |
| Log Analytics の上限 | 1 日 1 GB を超えた分は捨てる | ログが暴れたときの費用の歯止め | `log_daily_quota_gb` を上げる、または -1 |
| 予算の範囲 | リソースグループだけ。基盤用のリソースグループ（公開 IP など）は入らない | 予算はリソースグループ単位 | サブスクリプション単位の予算を足す |
| internal_only のとき | 環境の既定ドメインをプライベート DNS ゾーンで引く | VNet の中から呼ぶため | `allowed_cidrs` に VNet の中の送信元の範囲を入れる |
| 削除したバージョン | 本体が消えたあと、古いバージョンは `blob_soft_delete_days` だけ残る | 誤って消したときに戻せるように | 日数を短くする |
| Entra ID のアプリ登録 | 手で作る | azuread プロバイダとテナント管理者の権限が要る | 別の Terraform（azuread）に分けて管理する |
