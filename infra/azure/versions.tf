# Terraform と azurerm プロバイダの版を固定する。
# azurerm 4.77 以上が必要（storage_table の storage_account_id、
# custom_scale_rule の identity_id を使うため）。5.x は未検証なので上限を切る。

terraform {
  required_version = ">= 1.6"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = ">= 4.77, < 5.9"
    }
  }

  # 状態ファイルは既定でローカルに置く。
  # チームで使うときは、Blob に置く例を参考にする（先に別途ストレージを作る）。
  # use_azuread_auth = true にすると、共有キーなしで読み書きできる。
  #
  # backend "azurerm" {
  #   resource_group_name  = "<your-value>"
  #   storage_account_name = "<your-value>"
  #   container_name       = "tfstate"
  #   key                  = "review-platform/azure.tfstate"
  #   use_azuread_auth     = true
  # }
}

provider "azurerm" {
  subscription_id = var.subscription_id

  # ストレージは共有キーを無効にしているので、Entra ID で認証する。
  storage_use_azuread = true

  features {
    storage {
      # ストレージは公開アクセスを止めている。
      # データプレーン（*.blob/queue/table.core.windows.net）を呼ばないようにする。
      # コンテナ・キュー・テーブルは storage_account_id を使い、管理プレーン経由で作る。
      data_plane_available = false
    }

    key_vault {
      # 削除保護を有効にしているので、削除時に完全消去はしない。
      purge_soft_delete_on_destroy    = false
      recover_soft_deleted_key_vaults = true
    }

    resource_group {
      # 手で足したリソースが残っていたら、リソースグループごと消さない。
      prevent_deletion_if_contains_resources = true
    }
  }
}

# 実行している人（または CI）の情報。テナント ID などに使う。
data "azurerm_client_config" "current" {}
