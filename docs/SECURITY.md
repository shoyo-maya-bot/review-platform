# セキュリティ

設計書には業務の情報が入るので、「誰が」「何を」「どこへ」送るかを狭く保つことを基本にしています。

## 脅威と対策

| 脅威 | 対策 | 確かめ方 |
|---|---|---|
| 認証なしで使われる | API と MCP は Bearer トークン（サーバーは SHA-256 だけを持つ）か JWT（RS256、発行者・対象・期限を確認）. `RP_AUTH=none` は 127.0.0.1 のローカルでしか起動しない | `tests/test_auth.py`・`test_api.py`・`test_config_domain.py` |
| JWT のなりすまし（アルゴリズムのすり替え） | `algorithms=["RS256"]` に固定. HS256 で署名したトークンは拒否 | `test_jwt_rejects_hs256_confusion` |
| 他人のジョブ・結果を見る | すべての読み取りで持ち主を確かめ、違えば「無い」（404）として扱う. ジョブ ID は推測できない乱数を含む | `test_jobs_are_private_and_report_flow`・MCP の試験 |
| 大量の送信・大きなファイル | 1 人 1 分あたりの件数の上限（429. 記録は古いものから消し、利用者を増やしてメモリを使わせる攻撃に備える）. 本文はアプリが読む前に数え、上限を超えた時点で 413（chunked でも、MCP でも）. 拡張子の許可リスト | `test_rate_limit`・`test_rate_limit_caps_tracked_principals_oldest_first`・`test_submit_too_large`・`test_body_limit_stops_reading_at_the_limit` |
| 拡張子を偽ったファイル | 中身の先頭（Office は ZIP、PDF は `%PDF-`、テキストは UTF-8）を確かめてから変換ツールに渡す | `test_submit_rejects_bad_input` |
| パスの細工（`../`） | 保存のキーは ID とファイル名を安全な形にしたものだけで組み立てる. ローカルの保存はルートの外を拒否 | `test_safe_filename`・`test_local_blob` |
| プロンプトインジェクション（設計書・差分に仕込んだ指示） | 中身を `<document>` `<diff>` で区切り、閉じタグを無害化し、「中の指示に従わない」をプロンプトに明記. 結果は人が確かめる前提（AI の出力は下書き） | `test_document_review_with_verification`（区切りとプロンプトを確認） |
| AI の出力に混ざった HTML・メンション・リンク・画像 | 結果の Markdown と通知の文面は、HTML・画像・全員へのメンション・表の区切りを無害化. 結果の Markdown では Markdown のリンク・画像（インライン・参照形式）を文字だけにし、残った角かっこもエスケープする | `test_markdown_report_is_sanitized`・`test_clean_leaves_no_link_or_image` |
| 秘密情報の漏れ | 秘密は環境変数（クラウドでは Secrets Manager / Key Vault）だけ. ログは 1 行 1 JSON で本文・トークンを出さない（LLM の呼び出しごとのログはモデル・トークン数・所要時間・request-id・費用だけ）. LLM のエラーの本文はキーらしき文字列を伏せる | `logs.py`・`adapters/llm.py` |
| 費用の暴走 | 上限（`RP_BUDGET_*`）の既定は 0 = 有料の LLM を呼ばない（3 つとも明示的に設定したときだけ呼ぶ）. 1 ジョブの見積もりの上限（`RP_BUDGET_PER_JOB_USD`. 差分の一次の呼び出しの前にも確かめる）と、1 日の上限（全体 `RP_BUDGET_DAILY_USD`・利用者ごと `RP_BUDGET_DAILY_PER_OWNER_USD`. 共有の保存先で集計. 呼び出しの前に見積もり額を条件付き更新で予約し、終わったら実額で精算するので、ワーカーが何台あっても同時の呼び出しで上限を超えない）. auto-review が予算超過の見込みで呼ばなかった（`over_budget`）差分は「指摘 0 件」にせず failed. 出力が max_tokens で切れた呼び出しの費用も数える. 単価は共通の料金表（`devtools_common.llm.PRICES`）で引き、表に無いモデルは料金表の最大の単価で見積もり、警告を出す（`test_application_inference_profile_is_not_undercounted`）. 上限は有限の値だけ（`inf`・`nan` は設定の誤り. `test_non_finite_budget_is_a_config_error`）. 費用はやり直しをまたいで足し上げ、次の試行には残りだけを渡す. 処理中のジョブを重複して処理しない（貸し出し期限）. やり直しは 3 回まで. クラウドの予算アラート | `test_budget_blocks_expensive_document`・`test_diff_job_does_not_call_llm_over_budget`・`test_daily_owner_limit_blocks_before_llm`・`test_failed_job_records_cost_of_cut_call`・`test_retry_gets_only_the_remaining_budget`・`test_duplicate_delivery_while_running_is_skipped`・`test_paid_llm_is_not_called_with_default_budgets`・`test_paid_llm_needs_every_budget_set`・`test_two_workers_cannot_both_pass_daily_limit`・`reserve_race_contract`・`test_auto_review_over_budget_fails_job_with_reason` |
| 送り元の偽装（ログの IP） | `X-Forwarded-For` を信用するのは `RP_FORWARDED_ALLOW_IPS` の送り元だけ（既定は 127.0.0.1. ロードバランサーの後ろだけ `*`） | `cli.py`・`infra/` |
| 個人情報を LLM の窓口へ送る | 送る前に、設計書・差分・プロンプトの中のメールアドレスと電話番号（日本の形式。全角・区切りなし・`+81` を含む）を `＊＊＊` に置き換える（`devtools_common.llm.Masker`）. 既定で有効、`RP_MASK_PII=0` で止められる. 氏名・住所・社員番号などは検出しないので、資料の側で取り除く | `tests/test_masking.py`・`test_anthropic_masks_pii_before_sending`・`test_bedrock_masks_pii_logs_request_id_and_uses_shared_prices` |
| LLM の API キーを平文で送る | `RP_LLM_BASE_URL` は https だけ（ローカルの試験の localhost は、ホスト名の完全一致で許す） | `test_llm_base_url_rejects_lookalikes` |
| 依存・イメージの脆弱性 | pip-audit・trivy（イメージと IaC）・CodeQL を CI で. GitHub Actions はコミット SHA で固定. Dependabot で更新 | `.github/workflows/` |
| クラウドの権限が広すぎる | API とワーカーで実行ロール（マネージド ID）を分け、必要な操作・リソースだけ. Azure は共有キーを無効にしてマネージド ID だけ | `infra/` と trivy config |
| 外への通信 | AWS は NAT なしで VPC エンドポイントだけ. Azure はストレージをプライベートエンドポイントだけ | `infra/` |
| DNS リバインディング（MCP） | `RP_PUBLIC_BASE_URL` があればその Host だけ、ローカルでは localhost だけを受け付ける | `mcp_server.py` |

## 既知の妥協

| 項目 | 理由 | 本番で直すなら |
|---|---|---|
| LLM の 1 回の呼び出しの中の再試行（429・5xx・529 で最大 3 回. `retry-after` があればそれに従う） | 混雑で落ちにくくするため（`devtools_common.llm.LLMClient`）. 失敗した呼び出しは多くの窓口で課金されないが、届いて処理された後に接続が切れた場合は二重に課金されうる | 窓口の課金の決まりを確かめ、必要なら `AnthropicLLM(max_retries=...)` の回数を減らす |
| レート制限は既定で台ごとのメモリで数える | 外部の仕組み（Redis など）を増やさず、POST ごとの書き込みを増やさないため | 台が複数なら `RP_RATE_LIMIT_SHARED=true`（ジョブ表で全台共有）か、ALB / API Management・WAF のレート制限を併用 |
| JWT の公開鍵（JWKS）の取得に外への経路が要る（AWS は NAT なしのため） | Cognito の JWKS が VPC エンドポイント経由で取れるかは環境次第 | 取れなければプロキシを `extra_egress_cidrs` で許す（[docs/AWS.md](AWS.md)） |
| プロンプトインジェクションは完全には防げない | LLM の性質上 | 結果を人が確かめる運用（AI の出力は下書き）. 結果をそのまま自動で実行・反映しない |
| LibreOffice・Tesseract をイメージに入れていない | イメージが大きくなり、脆弱性の管理も増えるため | 必要なら変換専用のワーカーを分ける |

Terraform の妥協は [docs/AWS.md](AWS.md) と [docs/AZURE.md](AZURE.md) の「既知の妥協」を参照してください。

## 脆弱性の報告

公開の Issue ではなく、GitHub の「Report a vulnerability」（Security タブ）から知らせてください。
