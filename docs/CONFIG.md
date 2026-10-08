# 設定（環境変数の契約）

アプリ（API・ワーカー・MCP）は **環境変数だけ** で動きを変えます。Terraform（AWS・Azure）と docker compose は、ここに書いた名前で値を渡します。
名前を変えるときは、アプリ・Terraform・compose・このページを同じ PR で直してください。

秘密情報（API キー・Webhook の URL・接続文字列）は、クラウドではシークレットの仕組み（AWS Secrets Manager / Azure Key Vault）から渡し、リポジトリやイメージには入れません。

## 共通

| 変数 | 既定 | 意味 |
|---|---|---|
| `RP_BACKEND` | `local` | 保存・キュー・ジョブ管理の実装。`local`（ファイルと SQLite）/ `aws` / `azure` |
| `RP_LOG_LEVEL` | `INFO` | ログの細かさ。ログは 1 行 1 JSON で標準出力へ |
| `RP_MAX_UPLOAD_MB` | `10` | 受け付けるファイルの上限（MB、1〜100） |
| `RP_ALLOWED_EXTENSIONS` | `.xlsx,.xlsm,.docx,.pptx,.pdf,.md,.diff,.patch` | 受け付ける拡張子 |
| `RP_JOB_TTL_DAYS` | `30` | ジョブと結果を消すまでの日数（AWS は DynamoDB の TTL、Azure はワーカーが表の行を消す。S3・Blob はライフサイクルでも消す） |
| `RP_PUBLIC_BASE_URL` | （空） | 結果の URL を通知に載せるときの基準 URL. MCP はこの Host 以外からの要求を断る（DNS リバインディング対策） |
| `RP_RATE_LIMIT_PER_MINUTE` | `30` | 1 人が 1 分間に送れる件数（API と MCP の POST）. 1 台ごとの数え方. 数えるための記録は、最後の POST から 60 秒過ぎた利用者のものを消し、1 万人を超えたら最も長く使われていない利用者から消す（メモリが増え続けない） |

## 認証（API と MCP）

| 変数 | 既定 | 意味 |
|---|---|---|
| `RP_AUTH` | `token` | `none`（ローカルの試験だけ）/ `token` / `jwt` |
| `RP_API_TOKENS_SHA256` | （空） | `token` のとき、許可するトークンの SHA-256（16 進）をカンマ区切りで。トークンそのものは置かない |
| `RP_JWT_ISSUER` | （空） | `jwt` のとき、発行者（Cognito のユーザープール URL / Entra ID のテナント URL） |
| `RP_JWT_AUDIENCE` | （空） | `jwt` のとき、対象（アプリクライアント ID / アプリケーション ID URI） |
| `RP_JWT_JWKS_URL` | （空） | `jwt` のとき、公開鍵の URL |

`RP_AUTH=none` は、`RP_BACKEND=local` かつ待ち受けが `127.0.0.1` のときしか起動しません（誤ってクラウドで認証なしにしないため）。

## 保存・キュー・ジョブ管理

### local

| 変数 | 既定 | 意味 |
|---|---|---|
| `RP_DATA_DIR` | `./.rp-data` | ファイル・SQLite の置き場（API とワーカーで共有する） |

### aws

| 変数 | 意味 |
|---|---|
| `AWS_REGION` | リージョン（なければ `AWS_DEFAULT_REGION`） |
| `RP_S3_BUCKET` | 原本と結果を置くバケット |
| `RP_SQS_QUEUE_URL` | ジョブのキュー（デッドレターキューは Terraform 側で設定） |
| `RP_DDB_TABLE` | ジョブの表（パーティションキー `job_id`（文字列）、TTL 属性 `expires_at`（UNIX 秒）、GSI `owner-job-index`（パーティションキー `owner`・ソートキー `job_id`、射影 ALL）。GSI は利用者ごとの一覧に使う。日次の費用の集計も同じ表に `job_id = spend#<日付>#<範囲>` の項目で置く（ワーカーに `UpdateItem` が要る）。名前はアプリと Terraform で合わせる） |
| `RP_AWS_ENDPOINT_URL` | （任意）moto サーバーなどのエミュレーターに向けるときだけ（docker compose の `--profile aws` で使う）。S3・SQS・DynamoDB に効く。Bedrock には効かない。クラウドでは使わない |

認証は実行ロール（ECS のタスクロール）。アクセスキーは使いません。

### azure

| 変数 | 意味 |
|---|---|
| `RP_AZURE_BLOB_URL` | `https://<account>.blob.core.windows.net` |
| `RP_AZURE_QUEUE_URL` | `https://<account>.queue.core.windows.net` |
| `RP_AZURE_TABLE_URL` | `https://<account>.table.core.windows.net` |
| `RP_AZURE_CONTAINER` | 原本と結果を置くコンテナ（既定 `review`） |
| `RP_AZURE_QUEUE_NAME` | ジョブのキュー（Storage Queue、既定 `jobs`）。何度も失敗したメッセージは `<キュー名>-poison` に移す |
| `RP_AZURE_TABLE_NAME` | ジョブの表（Table Storage、既定 `jobs`） |
| `AZURE_CLIENT_ID` | ユーザー割り当てマネージド ID のクライアント ID（アプリの設定ではなく、Azure SDK の `DefaultAzureCredential` が読む） |
| `RP_AZURE_CONNECTION_STRING` | （任意）Azurite に向けるときだけ。クラウドでは使わない |

認証はマネージド ID（`DefaultAzureCredential`）。

## LLM

| 変数 | 既定 | 意味 |
|---|---|---|
| `RP_LLM` | `none` | `none`（LLM を使わない）/ `fake`（試験用）/ `anthropic`（Messages API と同じ形の窓口）/ `bedrock` |
| `RP_LLM_BASE_URL` | `https://api.anthropic.com` | `anthropic` のときの窓口。社内の中継や Microsoft Foundry など、Messages API と同じ形の窓口に向けられる |
| `ANTHROPIC_API_KEY` | （空） | `anthropic` のときの API キー（シークレットから渡す） |
| `RP_LLM_MODEL_STANDARD` | `claude-sonnet-5-5` | ふだんのレビューのモデル。Bedrock では推論プロファイル ID |
| `RP_LLM_MODEL_DEEP` | `claude-opus-5-5` | 検証に使う上位モデル。空なら検証しない |
| `RP_BUDGET_PER_JOB_USD` | `0` | 1 ジョブの費用の上限. **`0`（既定）なら有料の LLM（`anthropic`・`bedrock`）は一切呼ばない**（下の注を参照）. やり直しをまたいで足し上げる（見積もりで超えそうなら上位モデルを省き、ふだんのレビューも超えるなら呼ばずに failed. 設計書・差分のどちらも、最初の呼び出しの前に見積もりで確かめる） |
| `RP_BUDGET_DAILY_USD` | `0` | 1 日（UTC）の LLM 費用の上限（全体）. **`0`（既定）なら有料の LLM は呼ばない**（上限なしではない）. ジョブ表と同じ共有の保存先（SQLite・DynamoDB・Table Storage）で集計するので、ワーカーが何台でも効く. LLM を 1 回呼ぶごとに、呼ぶ前に見積もり額（伏せ字の後の入力 + 出力は上限いっぱい）を集計に**予約**し（条件付き更新: SQLite は書き込みトランザクション、DynamoDB は `TransactWriteItems` の条件、Table Storage は同じパーティションの ETag 付きトランザクション. 複数のワーカーが同時に呼んでも上限を超えない）、呼んだ後に実額との差を精算する. 予約できなければ LLM を呼ばずに failed（検証段なら一次の結果を残す）. 1 ジョブで使える予算は、1 ジョブの上限の残りと日次の上限の残りの小さい方. 呼び出し中は予約額が集計に入っているので、同時に動くジョブが多いと上限の手前で断ることがある（安全側） |
| `RP_BUDGET_DAILY_PER_OWNER_USD` | `0` | 1 日（UTC）の LLM 費用の上限（利用者ごと）. **`0`（既定）なら有料の LLM は呼ばない**. 扱いは `RP_BUDGET_DAILY_USD` と同じ |
| `RP_MASK_PII` | `1` | `1`（既定）なら、LLM に送る前に設計書・差分・プロンプトの中のメールアドレスと電話番号（日本の形式。全角・区切りなし・`+81` を含む）を `＊＊＊` に置き換える（`devtools_common.llm.Masker`）。`anthropic`・`bedrock`・`fake` のすべてに効く。`0` で止める（メールアドレスの検査そのものをレビューしたいときなど）。詳しくは [SECURITY.md](SECURITY.md) |

費用の上限（`RP_BUDGET_*`）について: 既定はすべて `0` で、`0` は「上限なし」ではなく **「有料の LLM を一切呼ばない」** です（従量課金を勝手に使わないため）.
`RP_LLM=anthropic`・`bedrock` で AI レビューをするときは、3 つの上限を**すべて 0 より大きい値に明示的に設定**してください. 1 つでも `0` のままなら、ジョブは LLM を呼ばずに failed（理由に `0` の変数名を出す）になり、ワーカーの起動時にも警告をログに出します.
疑似 LLM（`RP_LLM=fake`. 費用 0）と `none` は上限 `0` のままでも動きます（`fake` で上限を明示すると、`fake` が返す見積もりの費用で上限の動きを試せます）.

## 通知

| 変数 | 既定 | 意味 |
|---|---|---|
| `RP_NOTIFY` | `none` | `none` / `slack`（Slack・Mattermost の Incoming Webhook と同じ形） |
| `RP_SLACK_WEBHOOK_URL` | （空） | Webhook の URL（シークレットから渡す） |

## 待ち受け

| 変数 | 既定 | 意味 |
|---|---|---|
| `RP_HOST` | `127.0.0.1` | API の待ち受け。コンテナでは `0.0.0.0` |
| `RP_PORT` | `8080` | API の待ち受けポート。MCP は同じポートの `/mcp` |
| `RP_FORWARDED_ALLOW_IPS` | `127.0.0.1` | `X-Forwarded-For` などを信用する送り元. ロードバランサーからしか届かない構成（Terraform の AWS・Azure）だけ `*` にする |

## コンテナの起動

1 つのイメージで、起動コマンドだけを変えます。

| 役割 | コマンド | 置き場（AWS / Azure） |
|---|---|---|
| API と MCP | `review-platform api` | ECS サービス（ALB の後ろ）/ Container App（外部 ingress） |
| ワーカー | `review-platform worker` | ECS サービス（SQS の滞留で台数を増減）/ Container App（キューの長さで KEDA が増減） |

ヘルスチェックは `GET /healthz`（認証なし。中身は `{"status":"ok"}` だけ）。

## 保存のキー

| もの | キー |
|---|---|
| 原本 | `uploads/<job_id>/<元のファイル名を安全な形にしたもの>` |
| 結果（JSON） | `results/<job_id>/report.json` |
| 結果（Markdown） | `results/<job_id>/report.md` |
| キューのメッセージ | `{"job_id": "<job_id>"}`（中身はジョブ表から読む） |
