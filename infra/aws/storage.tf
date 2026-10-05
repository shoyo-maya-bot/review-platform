# 保存。原本と結果は S3、ジョブの状態は DynamoDB。

# ---------------------------------------------------------------------------
# S3
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "data" {
  # バケット名は世界で一意。アカウント ID を付けてぶつからないようにする。
  bucket        = "${local.name}-data-${local.account_id}"
  force_destroy = var.force_destroy
}

resource "aws_s3_bucket_public_access_block" "data" {
  bucket = aws_s3_bucket.data.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ACL を使わず、持ち主はバケットの持ち主に固定する。
resource "aws_s3_bucket_ownership_controls" "data" {
  bucket = aws_s3_bucket.data.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_versioning" "data" {
  bucket = aws_s3_bucket.data.id

  versioning_configuration {
    status = "Enabled"
  }
}

# s3_kms_key_arn が null なら SSE-S3、あれば SSE-KMS。
resource "aws_s3_bucket_server_side_encryption_configuration" "data" {
  bucket = aws_s3_bucket.data.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = var.s3_kms_key_arn == null ? "AES256" : "aws:kms"
      kms_master_key_id = var.s3_kms_key_arn
    }
    bucket_key_enabled = var.s3_kms_key_arn != null
  }
}

# TLS（1.2 以上）でない通信を拒否する。
data "aws_iam_policy_document" "data_bucket" {
  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.data.arn,
      "${aws_s3_bucket.data.arn}/*",
    ]
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  statement {
    sid    = "DenyOldTls"
    effect = "Deny"
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.data.arn,
      "${aws_s3_bucket.data.arn}/*",
    ]
    condition {
      test     = "NumericLessThan"
      variable = "s3:TlsVersion"
      values   = ["1.2"]
    }
  }
}

resource "aws_s3_bucket_policy" "data" {
  bucket = aws_s3_bucket.data.id
  policy = data.aws_iam_policy_document.data_bucket.json

  # パブリックアクセスブロックを先に入れる。
  depends_on = [aws_s3_bucket_public_access_block.data]
}

# uploads/ と results/ は job_ttl_days で消す。古い版は 7 日で消す。
resource "aws_s3_bucket_lifecycle_configuration" "data" {
  bucket = aws_s3_bucket.data.id

  dynamic "rule" {
    for_each = toset(["uploads", "results"])

    content {
      id     = "expire-${rule.value}"
      status = "Enabled"

      filter {
        prefix = "${rule.value}/"
      }

      expiration {
        days = var.job_ttl_days
      }

      noncurrent_version_expiration {
        noncurrent_days = 7
      }

      abort_incomplete_multipart_upload {
        days_after_initiation = 1
      }
    }
  }

  # 版管理の設定が先に入っている必要がある。
  depends_on = [aws_s3_bucket_versioning.data]
}

# ---------------------------------------------------------------------------
# DynamoDB
# ---------------------------------------------------------------------------

resource "aws_dynamodb_table" "jobs" {
  name         = "${local.name}-jobs"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "job_id"

  attribute {
    name = "job_id"
    type = "S"
  }

  attribute {
    name = "owner"
    type = "S"
  }

  # 利用者ごとの一覧（新しい順）. job_id は日時で始まるので並べ替えのキーにそのまま使える.
  # 名前はアプリ（src/review_platform/adapters/aws.py の OWNER_INDEX）と合わせる.
  global_secondary_index {
    name            = "owner-job-index"
    hash_key        = "owner"
    range_key       = "job_id"
    projection_type = "ALL"
  }

  # expires_at（UNIX 秒）を過ぎた項目は DynamoDB が自動で消す。
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = true
  }

  # AWS 管理キー（aws/dynamodb）で暗号化する。
  server_side_encryption {
    enabled = true
  }

  deletion_protection_enabled = var.dynamodb_deletion_protection
}
