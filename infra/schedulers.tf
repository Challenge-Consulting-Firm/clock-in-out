# EventBridge Scheduler: 日次取り込み / 週次レポート。
# Scheduler が Lambda を叩くための実行ロールを別途用意する。

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${var.name_prefix}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

data "aws_iam_policy_document" "scheduler_invoke" {
  statement {
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.ingest.arn, aws_lambda_function.weekly.arn]
  }
}

resource "aws_iam_role_policy" "scheduler_invoke" {
  name   = "${var.name_prefix}-scheduler-invoke"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler_invoke.json
}

resource "aws_scheduler_schedule" "daily_ingest" {
  name                         = "${var.name_prefix}-daily-ingest"
  schedule_expression          = var.daily_cron_jst
  schedule_expression_timezone = "Asia/Tokyo"
  flexible_time_window {
    mode = "OFF"
  }
  target {
    arn      = aws_lambda_function.ingest.arn
    role_arn = aws_iam_role.scheduler.arn
    retry_policy {
      maximum_retry_attempts = 2
    }
  }
}

resource "aws_scheduler_schedule" "weekly_report" {
  name                         = "${var.name_prefix}-weekly-report"
  schedule_expression          = var.weekly_cron_jst
  schedule_expression_timezone = "Asia/Tokyo"
  flexible_time_window {
    mode = "OFF"
  }
  target {
    arn      = aws_lambda_function.weekly.arn
    role_arn = aws_iam_role.scheduler.arn
    retry_policy {
      maximum_retry_attempts = 2
    }
  }
}
