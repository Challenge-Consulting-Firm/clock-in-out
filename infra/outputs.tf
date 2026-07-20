output "attendance_bucket" {
  description = "勤怠データ格納 S3 バケット名"
  value       = aws_s3_bucket.attendance.id
}

output "ingest_function" {
  description = "日次取り込み Lambda 関数名（手動実行: aws lambda invoke）"
  value       = aws_lambda_function.ingest.function_name
}

output "weekly_function" {
  description = "週次レポート Lambda 関数名"
  value       = aws_lambda_function.weekly.function_name
}

output "graph_secret_param" {
  description = "Graph 資格情報を投入する SSM Parameter 名"
  value       = aws_ssm_parameter.graph_secret.name
}

output "teams_webhook_param" {
  description = "Teams webhook URL を投入する SSM Parameter 名"
  value       = aws_ssm_parameter.teams_webhook.name
}
