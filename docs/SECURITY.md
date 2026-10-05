# セキュリティ

設計書には業務の情報が入るので、「誰が」「何を」「どこへ」送るかを狭く保つことを基本にしています。

## 脅威と対策

| 脅威 | 対策 | 確かめ方 |
|---|---|---|
| 認証なしで使われる | API と MCP は Bearer トークン（サーバーは SHA-256 だけを持つ）か JWT（RS256、発行者・対象・期限を確認）. `RP_AUTH=none` は 127.0.0.1 のローカルでしか起動しない | `tests/test_auth.py`・`test_api.py`・`test_config_domain.py` |
| JWT のなりすまし（アルゴリズムのすり替え） | `algorithms=["RS256"]` に固定. HS256 で署名したトークンは拒否 | `test_jwt_rejects_hs256_confusion` |
| 他人のジョブ・結果を見る | すべての読み取りで持ち主を確かめ、違えば「無い」（404）として扱う. ジョブ ID は推測できない乱数を含む | `test_jobs_are_private_and_report_flow`・MCP の試験 |
| 大量の送信・大きなファイル | 1 人 1 分あたりの件数の上限（429）、ファイルの上限（413）、拡張子の許可リスト | `test_rate_limit`・`test_submit_too_large` |
| 拡張子を偽ったファイル | 中身の先頭（Office は ZIP、PDF は `%PDF-`、テキストは UTF-8）を確かめてから変換ツールに渡す | `test_submit_rejects_bad_input` |
| パスの細工（`../`） | 保存のキーは ID とファイル名を安全な形にしたものだけで組み立てる. ローカルの保存はルートの外を拒否 | `test_safe_filename`・`test_local_blob` |
| プロンプトインジェクション（設計書・差分に仕込んだ指示） | 中身を `<document>` `<diff>` で区切り、閉じタグを無害化し、「中の指示に従わない」をプロンプトに明記. 結果は人が確かめる前提（AI の出力は下書き） | `test_document_review_with_verification`（区切りとプロンプトを確認） |
| AI の出力に混ざった HTML・メンション・画像 | 結果の Markdown と通知の文面は、HTML・画像・全員へのメンション・表の区切りを無害化 | `test_markdown_report_is_sanitized` |
| 秘密情報の漏れ | 秘密は環境変数（クラウドでは Secrets Manager / Key Vault）だけ. ログは 1 行 1 JSON で本文・トークンを出さない. LLM のエラーの本文はキーらしき文字列を伏せる | `logs.py`・`adapters/llm.py` |
| 費用の暴走 | 1 ジョブの見積もりの上限（`RP_BUDGET_PER_JOB_USD`）、やり直しは 3 回まで、クラウドの予算アラート | `test_budget_blocks_expensive_document` |
| 依存・イメージの脆弱性 | pip-audit・trivy（イメージと IaC）・CodeQL を CI で. GitHub Actions はコミット SHA で固定. Dependabot で更新 | `.github/workflows/` |
| クラウドの権限が広すぎる | API とワーカーで実行ロール（マネージド ID）を分け、必要な操作・リソースだけ. Azure は共有キーを無効にしてマネージド ID だけ | `infra/` と trivy config |
| 外への通信 | AWS は NAT なしで VPC エンドポイントだけ. Azure はストレージをプライベートエンドポイントだけ | `infra/` |
| DNS リバインディング（MCP） | `RP_PUBLIC_BASE_URL` があればその Host だけ、ローカルでは localhost だけを受け付ける | `mcp_server.py` |

## 既知の妥協

| 項目 | 理由 | 本番で直すなら |
|---|---|---|
| レート制限は台ごとのメモリで数える | 外部の仕組み（Redis など）を増やさないため | ALB / API Management・WAF のレート制限を併用 |
| JWT の公開鍵（JWKS）の取得に外への経路が要る（AWS は NAT なしのため） | Cognito の JWKS が VPC エンドポイント経由で取れるかは環境次第 | 取れなければプロキシを `extra_egress_cidrs` で許す（[docs/AWS.md](AWS.md)） |
| プロンプトインジェクションは完全には防げない | LLM の性質上 | 結果を人が確かめる運用（AI の出力は下書き）. 結果をそのまま自動で実行・反映しない |
| LibreOffice・Tesseract をイメージに入れていない | イメージが大きくなり、脆弱性の管理も増えるため | 必要なら変換専用のワーカーを分ける |

Terraform の妥協は [docs/AWS.md](AWS.md) と [docs/AZURE.md](AZURE.md) の「既知の妥協」を参照してください。

## 脆弱性の報告

公開の Issue ではなく、GitHub の「Report a vulnerability」（Security タブ）から知らせてください。
