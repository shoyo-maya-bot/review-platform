# Terraform とプロバイダーの版を固定する。
terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source = "hashicorp/aws"
      # 5.40 以上、6.0 未満。
      version = "~> 5.40"
    }
  }

  # 状態ファイル（tfstate）をチームで共有するときは S3 に置く。
  # バケットは先に手で作り、バージョニングと暗号化を有効にしておく。
  # 使うときはコメントを外し、値を自分の環境に合わせる。
  #
  # backend "s3" {
  #   bucket       = "<your-tfstate-bucket>"
  #   key          = "review-platform/dev/terraform.tfstate"
  #   region       = "ap-northeast-1"
  #   encrypt      = true
  #   use_lockfile = true # Terraform 1.10 以上。古い版は dynamodb_table を使う。
  # }
}

provider "aws" {
  region = var.region

  # すべてのリソースに共通のタグを付ける。
  default_tags {
    tags = {
      Project     = "review-platform"
      Environment = var.env
    }
  }
}
