# IAM。最小権限にする。
#   execution   : ECS がタスクを起こすときに使う（イメージ取得・ログ・シークレット読み取り）
#   api_task    : API のアプリが使う
#   worker_task : ワーカーのアプリが使う

# ECS タスクだけが引き受けられる。別アカウントからの悪用を防ぐ条件付き。
data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:${local.partition}:ecs:${var.region}:${local.account_id}:*"]
    }
  }
}

# ---------------------------------------------------------------------------
# 実行ロール
# ---------------------------------------------------------------------------

resource "aws_iam_role" "execution" {
  name               = "${local.name}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "execution" {
  # 認証トークンの取得は、リソースを絞れない（AWS の仕様）。
  statement {
    sid       = "EcrAuth"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid    = "EcrPull"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = [aws_ecr_repository.this.arn]
  }

  statement {
    sid    = "Logs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = [
      "${aws_cloudwatch_log_group.api.arn}:*",
      "${aws_cloudwatch_log_group.worker.arn}:*",
    ]
  }

  # 有効にしたシークレットだけを読める。
  dynamic "statement" {
    for_each = var.enable_anthropic_api_key_secret || var.enable_slack_webhook_secret ? [1] : []

    content {
      sid       = "ReadAppSecrets"
      effect    = "Allow"
      actions   = ["secretsmanager:GetSecretValue"]
      resources = local.secret_arns
    }
  }
}

resource "aws_iam_role_policy" "execution" {
  name   = "ecs-execution"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution.json
}

# ---------------------------------------------------------------------------
# API のタスクロール
# ---------------------------------------------------------------------------

resource "aws_iam_role" "api_task" {
  name               = "${local.name}-api-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "api_task" {
  # 原本を置く。
  statement {
    sid       = "PutUploads"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.data.arn}/uploads/*"]
  }

  # 原本と結果を読む。
  statement {
    sid     = "GetUploadsAndResults"
    effect  = "Allow"
    actions = ["s3:GetObject"]
    resources = [
      "${aws_s3_bucket.data.arn}/uploads/*",
      "${aws_s3_bucket.data.arn}/results/*",
    ]
  }

  statement {
    sid       = "EnqueueJobs"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.jobs.arn]
  }

  statement {
    sid    = "JobsTable"
    effect = "Allow"
    actions = [
      "dynamodb:PutItem",
      "dynamodb:GetItem",
      "dynamodb:Query",
    ]
    resources = [aws_dynamodb_table.jobs.arn, "${aws_dynamodb_table.jobs.arn}/index/owner-job-index"]
  }

  # S3 を自分の KMS キーで暗号化するときだけ。
  dynamic "statement" {
    for_each = var.s3_kms_key_arn == null ? [] : [1]

    content {
      sid       = "S3Kms"
      effect    = "Allow"
      actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
      resources = [var.s3_kms_key_arn]
    }
  }
}

resource "aws_iam_role_policy" "api_task" {
  name   = "api-task"
  role   = aws_iam_role.api_task.id
  policy = data.aws_iam_policy_document.api_task.json
}

# ---------------------------------------------------------------------------
# ワーカーのタスクロール
# ---------------------------------------------------------------------------

resource "aws_iam_role" "worker_task" {
  name               = "${local.name}-worker-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "worker_task" {
  statement {
    sid     = "GetUploadsAndResults"
    effect  = "Allow"
    actions = ["s3:GetObject"]
    resources = [
      "${aws_s3_bucket.data.arn}/uploads/*",
      "${aws_s3_bucket.data.arn}/results/*",
    ]
  }

  # 結果を書く。原本は書き換えない。
  statement {
    sid       = "PutResults"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.data.arn}/results/*"]
  }

  statement {
    sid    = "ConsumeJobs"
    effect = "Allow"
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:ChangeMessageVisibility",
    ]
    resources = [aws_sqs_queue.jobs.arn]
  }

  statement {
    sid    = "JobsTable"
    effect = "Allow"
    # 状態の更新は条件付きの PutItem（二重に届いたメッセージを 1 回分しか処理しないため）.
    # UpdateItem は日次の費用の集計（ADD で足す. 同時に足しても失われない）だけに使う
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
    ]
    resources = [aws_dynamodb_table.jobs.arn]
  }

  # 指定したモデル（推論プロファイルと基盤モデル）だけを呼べる。
  dynamic "statement" {
    for_each = var.llm_provider == "bedrock" && length(var.bedrock_model_arns) > 0 ? [1] : []

    content {
      sid    = "InvokeBedrock"
      effect = "Allow"
      actions = [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
      ]
      resources = var.bedrock_model_arns
    }
  }

  dynamic "statement" {
    for_each = var.s3_kms_key_arn == null ? [] : [1]

    content {
      sid       = "S3Kms"
      effect    = "Allow"
      actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
      resources = [var.s3_kms_key_arn]
    }
  }
}

resource "aws_iam_role_policy" "worker_task" {
  name   = "worker-task"
  role   = aws_iam_role.worker_task.id
  policy = data.aws_iam_policy_document.worker_task.json
}
