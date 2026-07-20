# 勤怠打刻システム本体: S3 / SSM / IAM(Bedrock jp.限定) / Lambda 2本。
#
# 迂回防止（Bedrock 国内完結）の IAM 設計は
# Challenge-Consulting-Firm/editor-claude-bedrock infra/main.tf を踏襲:
#   - Allow は (a) jp.* 推論プロファイル と (b) 東京/大阪の foundation-model
#     （jp.* プロファイル経由の条件付き）のみ
#   - 明示 Deny で東京/大阪以外のリージョンへの推論呼び出しを拒否
# これにより Lambda 実行ロールでも jp. 以外の推論は物理的に不可能になる。

data "aws_caller_identity" "current" {}

locals {
  jp_inference_regions = ["ap-northeast-1", "ap-northeast-3"]
  jp_profile_arn_patterns = [
    "arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:inference-profile/jp.*",
  ]

  # BEDROCK_MODEL_ID: 明示指定が無ければタグ付きアプリケーション推論プロファイル ARN を使う。
  # これにより Bedrock 推論コストが Project=clock-in-out タグで Cost Explorer に配賦され、
  # 他アカウント/他用途の Bedrock 利用と区別できる（オンデマンド課金はリソース非依存のため
  # この「タグ付きプロファイル経由」が AWS 公式のコスト配賦手段）。
  bedrock_model_id = var.bedrock_model_id != "" ? var.bedrock_model_id : aws_bedrock_inference_profile.attendance[var.cost_profile_model_key].arn
}

# ---- S3: 勤怠データ格納 ------------------------------------------------------
resource "aws_s3_bucket" "attendance" {
  bucket = "${var.name_prefix}-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_public_access_block" "attendance" {
  bucket                  = aws_s3_bucket.attendance.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "attendance" {
  bucket = aws_s3_bucket.attendance.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "attendance" {
  bucket = aws_s3_bucket.attendance.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# ---- SSM: シークレット枠（値は apply 後に put-parameter で投入）--------------
resource "aws_ssm_parameter" "graph_secret" {
  name        = "/${var.name_prefix}/graph-credentials"
  description = "Graph client credentials JSON: {tenant_id, client_id, client_secret}"
  type        = "SecureString"
  value       = "{}" # プレースホルダ。実値は CLI で上書き
  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "teams_webhook" {
  name        = "/${var.name_prefix}/teams-webhook-url"
  description = "週次レポート投稿先の Teams Workflows webhook URL（署名付き完全 URL）"
  type        = "SecureString"
  value       = "placeholder"
  lifecycle {
    ignore_changes = [value]
  }
}

# ---- IAM: Lambda 実行ロール --------------------------------------------------
data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "${var.name_prefix}-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "lambda_logs" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "lambda_perms" {
  # Bedrock: jp. プロファイル経由の推論のみ許可（国内完結の技術強制）
  statement {
    sid     = "AllowInvokeJpInferenceProfile"
    actions = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock:Converse", "bedrock:ConverseStream"]
    resources = concat(
      local.jp_profile_arn_patterns,
      ["arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:application-inference-profile/*"],
    )
  }

  statement {
    sid       = "AllowFoundationModelOnlyViaJpProfile"
    actions   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock:Converse", "bedrock:ConverseStream"]
    resources = [for r in local.jp_inference_regions : "arn:aws:bedrock:${r}::foundation-model/*"]
    condition {
      test     = "ArnLike"
      variable = "bedrock:InferenceProfileArn"
      values = concat(
        local.jp_profile_arn_patterns,
        ["arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:application-inference-profile/*"],
      )
    }
  }

  # 東京・大阪以外のリージョンへの推論呼び出しを明示 Deny（迂回防止の 2 重目）
  statement {
    sid       = "DenyInvokeOutsideJpRegions"
    effect    = "Deny"
    actions   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock:Converse", "bedrock:ConverseStream"]
    resources = ["*"]
    condition {
      test     = "StringNotEquals"
      variable = "aws:RequestedRegion"
      values   = local.jp_inference_regions
    }
  }

  # S3: 勤怠バケットの読み書き
  statement {
    sid       = "AllowS3Attendance"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
    resources = [aws_s3_bucket.attendance.arn, "${aws_s3_bucket.attendance.arn}/*"]
  }

  # SSM: シークレット読み取り
  statement {
    sid       = "AllowSsmRead"
    actions   = ["ssm:GetParameter"]
    resources = [aws_ssm_parameter.graph_secret.arn, aws_ssm_parameter.teams_webhook.arn]
  }
}

resource "aws_iam_role_policy" "lambda_perms" {
  name   = "${var.name_prefix}-lambda-perms"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.lambda_perms.json
}

# ---- Lambda パッケージング（lambda/ ディレクトリを zip）----------------------
data "archive_file" "lambda_zip" {
  type        = "zip"
  source_dir  = "${path.module}/../lambda"
  output_path = "${path.module}/.build/lambda.zip"
}

resource "aws_lambda_function" "ingest" {
  function_name    = "${var.name_prefix}-ingest"
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  handler          = "ingest_attendance.lambda_handler"
  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  timeout          = 300
  memory_size      = 256

  environment {
    variables = {
      ATTENDANCE_BUCKET  = aws_s3_bucket.attendance.id
      GRAPH_SECRET_PARAM = aws_ssm_parameter.graph_secret.name
      CHANNELS_JSON      = var.channels_json
      LOOKBACK_DAYS      = tostring(var.lookback_days)
      BEDROCK_MODEL_ID   = local.bedrock_model_id
    }
  }
}

resource "aws_lambda_function" "weekly" {
  function_name    = "${var.name_prefix}-weekly"
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  handler          = "weekly_report.lambda_handler"
  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  timeout          = 120
  memory_size      = 256

  environment {
    variables = {
      ATTENDANCE_BUCKET = aws_s3_bucket.attendance.id
      WEBHOOK_PARAM     = aws_ssm_parameter.teams_webhook.name
    }
  }
}
