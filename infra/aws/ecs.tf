# ECS Fargate。1 つのイメージを、起動コマンドだけ変えて 2 つの役割で動かす。
#   api    : review-platform api（ALB の後ろ）
#   worker : review-platform worker（SQS の滞留で台数を増減）

resource "aws_ecs_cluster" "this" {
  name = local.name

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_ecs_cluster_capacity_providers" "this" {
  cluster_name       = aws_ecs_cluster.this.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]

  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
  }
}

# ---------------------------------------------------------------------------
# ログ
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "api" {
  name              = "/ecs/${local.name}/api"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/ecs/${local.name}/worker"
  retention_in_days = var.log_retention_days
}

# ---------------------------------------------------------------------------
# タスク定義
# ---------------------------------------------------------------------------

locals {
  # 両方の役割で同じ部分。
  container_base = {
    image                  = var.image
    essential              = true
    user                   = "10001:10001" # root では動かさない。
    readonlyRootFilesystem = true
    entryPoint             = ["review-platform"]
    environment            = local.environment_list

    # 書けるのは /tmp だけ（タスクが終わると消える）。
    mountPoints = [{
      sourceVolume  = "tmp"
      containerPath = "/tmp"
      readOnly      = false
    }]

    linuxParameters = {
      initProcessEnabled = true # SIGTERM を正しく受け取り、ゾンビプロセスを残さない。
      capabilities = {
        drop = ["ALL"]
      }
    }
  }
}

resource "aws_ecs_task_definition" "api" {
  family                   = "${local.name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.api_cpu
  memory                   = var.api_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.api_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.cpu_architecture
  }

  # ホストのパスを指定しない volume は、Fargate ではタスク内の一時領域になる。
  volume {
    name = "tmp"
  }

  container_definitions = jsonencode([
    merge(local.container_base, {
      name    = "api"
      command = ["api"]

      portMappings = [{
        containerPort = local.container_port
        protocol      = "tcp"
      }]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.api.name
          awslogs-region        = var.region
          awslogs-stream-prefix = "api"
          mode                  = "non-blocking"
          max-buffer-size       = "25m"
        }
      }
    })
  ])
}

resource "aws_ecs_task_definition" "worker" {
  family                   = "${local.name}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.worker_cpu
  memory                   = var.worker_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.worker_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.cpu_architecture
  }

  volume {
    name = "tmp"
  }

  container_definitions = jsonencode([
    merge(local.container_base, {
      name        = "worker"
      command     = ["worker"]
      secrets     = local.worker_secrets
      stopTimeout = 120 # 止める前に、処理中のジョブを片付ける時間（Fargate の最大）。

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.worker.name
          awslogs-region        = var.region
          awslogs-stream-prefix = "worker"
          mode                  = "non-blocking"
          max-buffer-size       = "25m"
        }
      }
    })
  ])

  # 組み合わせの誤りを plan の段階で止める。
  lifecycle {
    precondition {
      condition     = var.llm_provider != "bedrock" || (var.llm_model_standard != "" && length(var.bedrock_model_arns) > 0)
      error_message = "llm_provider = bedrock のときは llm_model_standard（推論プロファイル ID）と bedrock_model_arns を指定してください。"
    }
    precondition {
      condition     = var.llm_provider != "anthropic" || var.enable_anthropic_api_key_secret
      error_message = "llm_provider = anthropic のときは enable_anthropic_api_key_secret = true にしてください。"
    }
    precondition {
      condition     = var.worker_max_count >= var.worker_min_count
      error_message = "worker_max_count は worker_min_count 以上にしてください。"
    }
  }
}

# ---------------------------------------------------------------------------
# サービス
# ---------------------------------------------------------------------------

resource "aws_ecs_service" "api" {
  name             = "api"
  cluster          = aws_ecs_cluster.this.id
  task_definition  = aws_ecs_task_definition.api.arn
  desired_count    = var.api_desired_count
  launch_type      = "FARGATE"
  platform_version = "LATEST"

  enable_execute_command            = false
  propagate_tags                    = "SERVICE"
  health_check_grace_period_seconds = 60

  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.api.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = local.container_port
  }

  # リスナーができてから登録する。エンドポイントがないとイメージを取れない。
  depends_on = [
    aws_lb_listener.https,
    aws_vpc_endpoint.interface,
    aws_vpc_endpoint.s3,
  ]
}

resource "aws_ecs_service" "worker" {
  name             = "worker"
  cluster          = aws_ecs_cluster.this.id
  task_definition  = aws_ecs_task_definition.worker.arn
  desired_count    = var.worker_min_count
  launch_type      = var.worker_use_spot ? null : "FARGATE"
  platform_version = "LATEST"

  enable_execute_command = false
  propagate_tags         = "SERVICE"

  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  dynamic "capacity_provider_strategy" {
    for_each = var.worker_use_spot ? [1] : []

    content {
      capacity_provider = "FARGATE_SPOT"
      weight            = 1
    }
  }

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.worker.id]
    assign_public_ip = false
  }

  # 台数はオートスケーリングが決める。apply で元に戻さない。
  lifecycle {
    ignore_changes = [desired_count]
  }

  depends_on = [
    aws_ecs_cluster_capacity_providers.this,
    aws_vpc_endpoint.interface,
    aws_vpc_endpoint.s3,
  ]
}

# ---------------------------------------------------------------------------
# ワーカーのオートスケーリング（SQS の滞留で増減）
# ---------------------------------------------------------------------------

resource "aws_appautoscaling_target" "worker" {
  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.this.name}/${aws_ecs_service.worker.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = var.worker_min_count
  max_capacity       = var.worker_max_count
}

# 待ちのメッセージがあれば増やす。0 台からでも起きる。
resource "aws_appautoscaling_policy" "worker_scale_out" {
  name               = "${local.name}-worker-scale-out"
  policy_type        = "StepScaling"
  service_namespace  = aws_appautoscaling_target.worker.service_namespace
  resource_id        = aws_appautoscaling_target.worker.resource_id
  scalable_dimension = aws_appautoscaling_target.worker.scalable_dimension

  step_scaling_policy_configuration {
    adjustment_type         = "ChangeInCapacity"
    cooldown                = 60
    metric_aggregation_type = "Maximum"

    # 待ち 1〜9 件: 1 台増やす。
    step_adjustment {
      metric_interval_lower_bound = 0
      metric_interval_upper_bound = 10
      scaling_adjustment          = 1
    }

    # 待ち 10 件以上: 3 台増やす。
    step_adjustment {
      metric_interval_lower_bound = 10
      scaling_adjustment          = 3
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "worker_scale_out" {
  alarm_name          = "${local.name}-worker-scale-out"
  alarm_description   = "SQS にジョブが待っている。ワーカーを増やす。"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.jobs.name }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_appautoscaling_policy.worker_scale_out.arn]
}

# 待ちも処理中も 0 の状態が続いたら、最小台数に戻す。
# 処理中（NotVisible）を足しているので、ジョブの途中で止めない。
resource "aws_appautoscaling_policy" "worker_scale_in" {
  name               = "${local.name}-worker-scale-in"
  policy_type        = "StepScaling"
  service_namespace  = aws_appautoscaling_target.worker.service_namespace
  resource_id        = aws_appautoscaling_target.worker.resource_id
  scalable_dimension = aws_appautoscaling_target.worker.scalable_dimension

  step_scaling_policy_configuration {
    adjustment_type         = "ExactCapacity"
    cooldown                = 300
    metric_aggregation_type = "Maximum"

    step_adjustment {
      metric_interval_upper_bound = 0
      scaling_adjustment          = var.worker_min_count
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "worker_scale_in" {
  alarm_name          = "${local.name}-worker-scale-in"
  alarm_description   = "SQS が空。ワーカーを最小台数に戻す。"
  evaluation_periods  = var.worker_scale_in_minutes
  threshold           = 0
  comparison_operator = "LessThanOrEqualToThreshold"
  # キューが長く使われないとメトリクスが止まる。そのときは空とみなす。
  treat_missing_data = "breaching"
  alarm_actions      = [aws_appautoscaling_policy.worker_scale_in.arn]

  metric_query {
    id = "visible"
    metric {
      namespace   = "AWS/SQS"
      metric_name = "ApproximateNumberOfMessagesVisible"
      dimensions  = { QueueName = aws_sqs_queue.jobs.name }
      period      = 60
      stat        = "Maximum"
    }
  }

  metric_query {
    id = "inflight"
    metric {
      namespace   = "AWS/SQS"
      metric_name = "ApproximateNumberOfMessagesNotVisible"
      dimensions  = { QueueName = aws_sqs_queue.jobs.name }
      period      = 60
      stat        = "Maximum"
    }
  }

  metric_query {
    id          = "total"
    expression  = "visible + inflight"
    label       = "Messages visible + in flight"
    return_data = true
  }
}
