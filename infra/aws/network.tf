# ネットワーク。
# パブリックサブネットには ALB だけを置く。タスクはプライベートサブネットに置く。
# NAT ゲートウェイは作らない。AWS のサービスへは VPC エンドポイントで届く。

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true # インターフェース型エンドポイントのプライベート DNS に必要。

  tags = { Name = local.name }
}

# 既定のセキュリティグループのルールをすべて外す（使わせない）。
resource "aws_default_security_group" "this" {
  vpc_id = aws_vpc.this.id

  tags = { Name = "${local.name}-default-unused" }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id

  tags = { Name = local.name }
}

# ---------------------------------------------------------------------------
# サブネット（2 AZ × パブリック / プライベート）
# ---------------------------------------------------------------------------

resource "aws_subnet" "public" {
  count = 2

  vpc_id                  = aws_vpc.this.id
  availability_zone       = local.azs[count.index]
  cidr_block              = cidrsubnet(var.vpc_cidr, 4, count.index)
  map_public_ip_on_launch = false

  tags = { Name = "${local.name}-public-${local.azs[count.index]}" }
}

resource "aws_subnet" "private" {
  count = 2

  vpc_id            = aws_vpc.this.id
  availability_zone = local.azs[count.index]
  cidr_block        = cidrsubnet(var.vpc_cidr, 4, count.index + 2)

  tags = { Name = "${local.name}-private-${local.azs[count.index]}" }
}

# ---------------------------------------------------------------------------
# ルートテーブル
# ---------------------------------------------------------------------------

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id

  tags = { Name = "${local.name}-public" }
}

resource "aws_route" "public_internet" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.this.id
}

resource "aws_route_table_association" "public" {
  count = 2

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# プライベートはインターネットへの経路を持たない。
# S3 と DynamoDB のゲートウェイ型エンドポイントの経路だけが入る。
resource "aws_route_table" "private" {
  vpc_id = aws_vpc.this.id

  tags = { Name = "${local.name}-private" }
}

resource "aws_route_table_association" "private" {
  count = 2

  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# ---------------------------------------------------------------------------
# セキュリティグループ
# ---------------------------------------------------------------------------

resource "aws_security_group" "alb" {
  name        = "${local.name}-alb"
  description = "ALB: 443 from allowed CIDRs, 80 for redirect only"
  vpc_id      = aws_vpc.this.id

  tags = { Name = "${local.name}-alb" }
}

resource "aws_security_group" "api" {
  name        = "${local.name}-api"
  description = "API tasks: 8080 from ALB only"
  vpc_id      = aws_vpc.this.id

  tags = { Name = "${local.name}-api" }
}

resource "aws_security_group" "worker" {
  name        = "${local.name}-worker"
  description = "Worker tasks: no ingress"
  vpc_id      = aws_vpc.this.id

  tags = { Name = "${local.name}-worker" }
}

resource "aws_security_group" "endpoints" {
  name        = "${local.name}-vpce"
  description = "Interface VPC endpoints: 443 from task security groups only"
  vpc_id      = aws_vpc.this.id

  tags = { Name = "${local.name}-vpce" }
}

# --- ALB ---

resource "aws_vpc_security_group_ingress_rule" "alb_https" {
  for_each = toset(var.allowed_cidrs)

  security_group_id = aws_security_group.alb.id
  description       = "HTTPS from allowed CIDR"
  cidr_ipv4         = each.value
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

# HTTP は HTTPS へ転送するだけ。
resource "aws_vpc_security_group_ingress_rule" "alb_http" {
  for_each = toset(var.allowed_cidrs)

  security_group_id = aws_security_group.alb.id
  description       = "HTTP from allowed CIDR (redirect to HTTPS only)"
  cidr_ipv4         = each.value
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
}

resource "aws_vpc_security_group_egress_rule" "alb_to_api" {
  security_group_id            = aws_security_group.alb.id
  description                  = "To API tasks"
  referenced_security_group_id = aws_security_group.api.id
  ip_protocol                  = "tcp"
  from_port                    = local.container_port
  to_port                      = local.container_port
}

# --- API タスク ---

resource "aws_vpc_security_group_ingress_rule" "api_from_alb" {
  security_group_id            = aws_security_group.api.id
  description                  = "From ALB"
  referenced_security_group_id = aws_security_group.alb.id
  ip_protocol                  = "tcp"
  from_port                    = local.container_port
  to_port                      = local.container_port
}

# --- タスク共通の出口（API とワーカー） ---

locals {
  task_security_groups = {
    api    = aws_security_group.api.id
    worker = aws_security_group.worker.id
  }

  # 出口の組み合わせ（タスク SG × 宛先）。
  task_egress_extra = {
    for pair in setproduct(keys(local.task_security_groups), var.extra_egress_cidrs) :
    "${pair[0]}-${pair[1]}" => { sg = local.task_security_groups[pair[0]], cidr = pair[1] }
  }
}

resource "aws_vpc_security_group_egress_rule" "task_to_endpoints" {
  for_each = local.task_security_groups

  security_group_id            = each.value
  description                  = "HTTPS to interface VPC endpoints"
  referenced_security_group_id = aws_security_group.endpoints.id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}

resource "aws_vpc_security_group_egress_rule" "task_to_s3" {
  for_each = local.task_security_groups

  security_group_id = each.value
  description       = "HTTPS to S3 gateway endpoint"
  prefix_list_id    = aws_vpc_endpoint.s3.prefix_list_id
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_egress_rule" "task_to_dynamodb" {
  for_each = local.task_security_groups

  security_group_id = each.value
  description       = "HTTPS to DynamoDB gateway endpoint"
  prefix_list_id    = aws_vpc_endpoint.dynamodb.prefix_list_id
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_egress_rule" "task_extra" {
  for_each = local.task_egress_extra

  security_group_id = each.value.sg
  description       = "HTTPS to extra egress CIDR (proxy or NAT)"
  cidr_ipv4         = each.value.cidr
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

# --- VPC エンドポイント ---

resource "aws_vpc_security_group_ingress_rule" "endpoints_from_tasks" {
  for_each = local.task_security_groups

  security_group_id            = aws_security_group.endpoints.id
  description                  = "HTTPS from ${each.key} tasks"
  referenced_security_group_id = each.value
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}

# ---------------------------------------------------------------------------
# VPC エンドポイント
# ---------------------------------------------------------------------------

# S3（ゲートウェイ型、無料）。このバケットと、ECR のイメージ層の置き場だけを通す。
data "aws_iam_policy_document" "s3_endpoint" {
  statement {
    sid    = "AppBucket"
    effect = "Allow"
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${aws_s3_bucket.data.arn}/*"]
  }

  statement {
    sid    = "EcrImageLayers"
    effect = "Allow"
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    actions   = ["s3:GetObject"]
    resources = ["arn:${local.partition}:s3:::prod-${var.region}-starport-layer-bucket/*"]
  }
}

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]
  policy            = data.aws_iam_policy_document.s3_endpoint.json

  tags = { Name = "${local.name}-s3" }
}

# DynamoDB（ゲートウェイ型、無料）。ジョブの表だけを通す。
data "aws_iam_policy_document" "dynamodb_endpoint" {
  statement {
    sid    = "JobsTable"
    effect = "Allow"
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:Query",
    ]
    resources = [aws_dynamodb_table.jobs.arn, "${aws_dynamodb_table.jobs.arn}/index/owner-job-index"]
  }
}

resource "aws_vpc_endpoint" "dynamodb" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${var.region}.dynamodb"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]
  policy            = data.aws_iam_policy_document.dynamodb_endpoint.json

  tags = { Name = "${local.name}-dynamodb" }
}

# インターフェース型（1 つあたり AZ ごとに時間課金）。使わないものは作らない。
locals {
  interface_endpoint_services = toset(concat(
    ["sqs", "ecr.api", "ecr.dkr", "logs", "sts"],
    var.llm_provider == "bedrock" ? ["bedrock-runtime"] : [],
    var.enable_anthropic_api_key_secret || var.enable_slack_webhook_secret ? ["secretsmanager"] : [],
    var.enable_cognito_idp_endpoint ? ["cognito-idp"] : [],
  ))
}

resource "aws_vpc_endpoint" "interface" {
  for_each = local.interface_endpoint_services

  vpc_id              = aws_vpc.this.id
  service_name        = "com.amazonaws.${var.region}.${each.key}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = slice(aws_subnet.private[*].id, 0, var.interface_endpoint_az_count)
  security_group_ids  = [aws_security_group.endpoints.id]
  private_dns_enabled = true

  tags = { Name = "${local.name}-${each.key}" }
}

# ---------------------------------------------------------------------------
# VPC フローログ
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "flow_logs" {
  count = var.enable_flow_logs ? 1 : 0

  name              = "/vpc/${local.name}/flow-logs"
  retention_in_days = var.log_retention_days
}

data "aws_iam_policy_document" "flow_logs_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["vpc-flow-logs.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "flow_logs" {
  count = var.enable_flow_logs ? 1 : 0

  name               = "${local.name}-flow-logs"
  assume_role_policy = data.aws_iam_policy_document.flow_logs_assume.json
}

data "aws_iam_policy_document" "flow_logs" {
  count = var.enable_flow_logs ? 1 : 0

  statement {
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
    ]
    resources = ["${aws_cloudwatch_log_group.flow_logs[0].arn}:*"]
  }
}

resource "aws_iam_role_policy" "flow_logs" {
  count = var.enable_flow_logs ? 1 : 0

  name   = "write-flow-logs"
  role   = aws_iam_role.flow_logs[0].id
  policy = data.aws_iam_policy_document.flow_logs[0].json
}

resource "aws_flow_log" "this" {
  count = var.enable_flow_logs ? 1 : 0

  vpc_id               = aws_vpc.this.id
  traffic_type         = "ALL"
  log_destination_type = "cloud-watch-logs"
  log_destination      = aws_cloudwatch_log_group.flow_logs[0].arn
  iam_role_arn         = aws_iam_role.flow_logs[0].arn

  tags = { Name = local.name }
}
