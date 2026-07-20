# コスト可視化: タグ付きアプリケーション推論プロファイル。
#
# Bedrock のオンデマンド推論はリソース非依存の課金のため、リソースタグだけでは
# 推論コストを配賦できない。システムの jp. プロファイルを複製した
# 「アプリケーション推論プロファイル」にタグを付け、この ARN 経由で呼ぶことで
# Cost Explorer でタグ別（Project 等）に推論コストを集計できる。
# プロファイル自体は無償。jp. の +10% プレミアムや推論先（東京+大阪）は元プロファイルを継承する。
#
# 出典: Challenge-Consulting-Firm/editor-claude-bedrock（実測 2026-07-14）。

locals {
  app_profile_models = {
    haiku-4-5  = "jp.anthropic.claude-haiku-4-5-20251001-v1:0"
    sonnet-4-6 = "jp.anthropic.claude-sonnet-4-6"
  }
}

resource "aws_bedrock_inference_profile" "attendance" {
  for_each = local.app_profile_models

  name = "${var.name_prefix}-${each.key}-jp"
  # description は ASCII のみ許可（日本語を入れると ValidationException — 実測）
  description = "Cost-allocation profile for clock-in-out use of ${each.value} with Japan-resident inference"

  model_source {
    copy_from = "arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:inference-profile/${each.value}"
  }

  # default_tags(Project=clock-in-out) に加え、他の Bedrock 利用と推論コストを明確に区別する専用タグ。
  # Cost Explorer で Workload=clock-in-out-attendance / Model=<key> でフィルタできる。
  tags = {
    Workload = "clock-in-out-attendance"
    Model    = each.key
  }
}

output "application_inference_profile_arns" {
  description = "BEDROCK_MODEL_ID に指定するとコスト配賦される ARN"
  value       = { for k, v in aws_bedrock_inference_profile.attendance : k => v.arn }
}
