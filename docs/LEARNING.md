# このリポジトリで学べること

1 つの題材（設計書のレビュー基盤）を、ローカル・AWS・Azure で同じように動かすことで、クラウドの部品の役割と違いを比べながら身に付けます。
資格の試験範囲との対応は、公式の試験ガイドの分野名に合わせています（範囲は改訂されるので、受ける前に公式のガイドで確かめてください）。

## AWS と Azure の対応

| 役割 | このリポジトリのコード | AWS | Azure |
|---|---|---|---|
| コンテナを動かす | `Dockerfile` | ECS Fargate | Container Apps |
| HTTP の入口 | `api.py` | ALB（HTTPS） | Container Apps の ingress |
| ファイルを置く | `adapters/aws.py`・`azure.py` の BlobStore | S3 | Blob Storage |
| 非同期の受け渡し | 同 Queue | SQS（DLQ つき） | Storage Queue（`-poison` へ退避） |
| ジョブの状態 | 同 JobRepository | DynamoDB（GSI・TTL） | Table Storage |
| 台数の増減 | `worker.py` | Application Auto Scaling（SQS の滞留） | KEDA（キューの長さ） |
| 認証（利用者） | `auth.py` | Cognito（JWT） | Entra ID（JWT） |
| 認証（アプリ → クラウド） | SDK の既定の認証 | タスクロール（IAM） | マネージド ID（RBAC） |
| 秘密情報 | 環境変数 | Secrets Manager | Key Vault |
| 外に出さない | — | VPC エンドポイント（NAT なし） | プライベートエンドポイント・プライベート DNS |
| LLM | `adapters/llm.py` | Bedrock | Microsoft Foundry（Messages API と同じ形の窓口） |
| 監視・費用 | `logs.py`（1 行 1 JSON） | CloudWatch・Budgets | Monitor（Log Analytics）・Cost Management |
| IaC | `infra/` | Terraform（aws） | Terraform（azurerm） |

## 試験範囲との対応

| 試験 | 分野 | このリポジトリで触れるところ |
|---|---|---|
| AWS SAA | セキュアなアーキテクチャ | IAM の最小権限（API とワーカーでロールを分ける）、S3 の暗号化と公開ブロック、Cognito |
| AWS SAA | 弾力性 | SQS で API とワーカーを切り離す、DLQ、ワーカーの自動増減、冪等な処理 |
| AWS SAA | 高性能 | DynamoDB のキー設計と GSI、オンデマンド |
| AWS SAA | コスト最適化 | NAT を使わず VPC エンドポイント、ワーカーを 0 台まで減らす、ライフサイクル・TTL、Budgets |
| AZ-104 | ID とガバナンス | マネージド ID と RBAC の割り当て、タグ、予算 |
| AZ-104 | ストレージ | ストレージアカウント（共有キー無効・ライフサイクル・論理削除） |
| AZ-104 | コンピューティング | Container Apps、ACR |
| AZ-104 | ネットワーク | VNet・サブネットの委任・プライベートエンドポイント・プライベート DNS |
| AZ-104 | 監視 | Log Analytics、アラート |
| AZ-305 | ID・ガバナンス・監視の設計 | Entra ID の JWT、Key Vault の参照、診断設定 |
| AZ-305 | データの保存の設計 | Table と Cosmos DB、Storage Queue と Service Bus の選び方（[ARCHITECTURE.md](ARCHITECTURE.md) の判断表） |
| AZ-305 | インフラの設計 | 閉域、スケールの方式、コストの見積もり |

## MCP

| 学ぶこと | 見るところ |
|---|---|
| MCP サーバーを HTTP で公開する（Streamable HTTP、stateless） | `mcp_server.py`・`api.py` |
| 道具の説明文と引数の設計（エージェントが迷わない形） | `mcp_server.py` の docstring・`INSTRUCTIONS` |
| リモートの MCP の認証と、持ち主ごとの分離 | `auth.py`・`service.py`・`tests/test_mcp.py` |
| プロンプトインジェクションへの備え | `prompts/`・`pipeline/document.py` の `wrap_document` |

## おすすめの順番

1. ローカルで動かす（README の「すぐ試す」）. `docker compose --profile local`
2. `--profile aws`（moto）と `--profile azure`（Azurite）で、同じ操作がクラウドの実装で動くのを見る
3. `docs/ARCHITECTURE.md` で、状態の遷移とやり直しの決まりを読む
4. `infra/aws` を `terraform plan` まで（apply は費用がかかるので、予算アラートを設定してから）
5. `infra/azure` で同じことをし、上の対応表を自分の言葉で埋め直す
6. VS Code の Copilot から MCP でつなぐ
