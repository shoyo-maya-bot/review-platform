# ジョブのキュー（SQS）と、デッドレターキュー（DLQ）。
# 3 回受け取って終わらなかったメッセージは DLQ へ移る。

resource "aws_sqs_queue" "jobs_dlq" {
  name                      = "${local.name}-jobs-dlq"
  message_retention_seconds = 1209600 # 14 日（最大）。調べる時間を確保する。
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "jobs" {
  name                       = "${local.name}-jobs"
  visibility_timeout_seconds = 900    # 1 ジョブの最長処理時間より長くする。
  message_retention_seconds  = 345600 # 4 日
  receive_wait_time_seconds  = 20     # ロングポーリングで空振りの呼び出しを減らす。
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.jobs_dlq.arn
    maxReceiveCount     = 3
  })
}

# DLQ に入れてよいのは本キューだけ。
resource "aws_sqs_queue_redrive_allow_policy" "jobs_dlq" {
  queue_url = aws_sqs_queue.jobs_dlq.id

  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.jobs.arn]
  })
}

# TLS でない通信を拒否する。
data "aws_iam_policy_document" "queue_tls_only" {
  for_each = {
    jobs     = aws_sqs_queue.jobs.arn
    jobs_dlq = aws_sqs_queue.jobs_dlq.arn
  }

  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    actions   = ["sqs:*"]
    resources = [each.value]
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_sqs_queue_policy" "jobs" {
  queue_url = aws_sqs_queue.jobs.id
  policy    = data.aws_iam_policy_document.queue_tls_only["jobs"].json
}

resource "aws_sqs_queue_policy" "jobs_dlq" {
  queue_url = aws_sqs_queue.jobs_dlq.id
  policy    = data.aws_iam_policy_document.queue_tls_only["jobs_dlq"].json
}
