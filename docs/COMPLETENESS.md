# 「完成」の定義と確かめ方

このリポジトリの「完成」とは、**次の表のすべてが、CI か決まった手順で確かめられること** です。
自動で確かめられないもの（本物のクラウド・本物の LLM）は、確かめ方を書いた上で「未確認」と分けて載せます。確かめていないことを「動く」とは書きません。

## 自動で確かめていること（CI が毎回通す）

| 項目 | 何で確かめるか | CI のジョブ |
|---|---|---|
| コードの書き方 | ruff（lint・format） | test (Linux) |
| 受付・状態・結果・一覧の API | `tests/test_api.py` | test (Linux / Windows) |
| 認証（トークン・JWT・アルゴリズムのすり替え・レート制限） | `tests/test_auth.py` | 同上 |
| 設定の検査（認証なしはローカルだけ、など） | `tests/test_config_domain.py` | 同上 |
| ワーカー（条件付き更新・やり直し・失敗の分け方・読めない AI の出力・予算） | `tests/test_worker.py` |
| 期限切れのジョブの削除（SQLite・Table） | `tests/test_local.py`・`test_azure.py` | 同上 |
| MCP（本物の HTTP の上で公式 SDK から 5 つの道具。他人のジョブが見えない・認証なしで 401） | `tests/test_mcp.py` | 同上 |
| CLI・LLM の再試行とエラー・通知の無害化・ログ | `tests/test_cli_llm_notify.py` | 同上 |
| 3 つの実装の振る舞いがそろう | `tests/contract.py` を local・moto（AWS）・Azurite（Azure）に流す | test (Linux) |
| 試験の網羅率 85% 以上 | `pytest --cov-fail-under=85` | test (Linux, 3.11 / 3.12) |
| Windows で動く | 同じ試験（Azurite を除く） | test (Windows) |
| コンテナで通しに動く | `scripts/e2e.sh`: compose で起動 → 送る → 結果 → MCP の `tools/list` | e2e (local / aws / azure) |
| Terraform の文法と設定 | `terraform fmt -check`・`validate`・tflint | terraform (aws / azure) |
| IaC の危ない設定 | trivy config（例外は `.trivyignore` に理由つき） | trivy config |
| イメージの脆弱性 | trivy image（HIGH・CRITICAL で止める） | trivy image |
| 依存の脆弱性 | pip-audit | test (Linux) |
| コードの脆弱性 | CodeQL | analyze |
| 依存の更新 | Dependabot（pip・Actions・Docker・Terraform） | — |
| Actions の取り違え | すべてコミット SHA で固定 | — |

## 人が確かめること（費用・アカウントが要る）

| 項目 | 確かめ方 | 状態 |
|---|---|---|
| AWS に apply して動く | [AWS.md](AWS.md) の手順 1〜9. 予算アラートを先に設定 | 未確認 |
| Azure に apply して動く | [AZURE.md](AZURE.md) の手順 | 未確認 |
| 本物の LLM でレビューできる（Anthropic・Bedrock・Foundry） | `.env` を `RP_LLM=anthropic`（または `bedrock`）にして `samples/` を送る. 費用は結果の `cost_usd` | 未確認 |
| Cognito / Entra ID の JWT で API と MCP を呼べる | 各ドキュメントのトークンの取り方 → `curl` と VS Code | 未確認 |
| AWS の NAT なしで Cognito の公開鍵（JWKS）が取れる | ワーカーではなく API のログで JWKS の取得の失敗が無いこと. 取れなければ [SECURITY.md](SECURITY.md) の妥協のとおり | 未確認 |
| Azure の KEDA がマネージド ID でキューの長さを読み、0 台から増える | ジョブを送り、`az containerapp replica list` で台数を見る | 未確認 |
| VS Code の Copilot から MCP で使える | README の `.vscode/mcp.json` | 未確認（MCP の SDK のクライアントでは確認済み） |

確かめたら、この表の「状態」を日付つきで直してください（例: `確認済み（2026-10-10、ap-northeast-1）`）。

## わざとやっていないこと

| 項目 | 理由 |
|---|---|
| Terraform の lock ファイル（`.terraform.lock.hcl`）をコミットしていない | 作るときの環境（OS）で中身が変わるため. 最初に `terraform init` した人が `terraform providers lock -platform=linux_amd64 -platform=windows_amd64 -platform=darwin_arm64` で作ってコミットするのがおすすめ |
| 本番の負荷試験 | 学習と小さなチーム向けの作例のため. ローカルの実装（SQLite）は 1 台前提 |
| 画面（Web UI） | API・MCP・CLI で足りるため |

## 完成の状態を保つ決まり

- 環境変数を足す・変えるときは、アプリ・Terraform・compose・[CONFIG.md](CONFIG.md) を同じ PR で直す
- 試験を通さずに網羅率の基準を下げない
- `.trivyignore` に足すときは、理由と「本番で直すなら」を書く
- AI の出力は下書き. 結果をそのまま自動で反映する仕組みは足さない
