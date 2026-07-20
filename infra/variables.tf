variable "aws_region" {
  description = "呼び出し元リージョン（データ所在＝東京固定）"
  type        = string
  default     = "ap-northeast-1"
}

variable "name_prefix" {
  description = "リソース名の接頭辞"
  type        = string
  default     = "clock-in-out"
}

variable "bedrock_model_id" {
  description = "勤怠構造化に使うモデル ID の明示上書き。空なら inference-profiles.tf のタグ付き Haiku アプリケーション推論プロファイル ARN を自動使用（コスト配賦のため推奨）。上書きする場合も国内完結のため jp. プレフィックス or application-inference-profile ARN のみ"
  type        = string
  default     = ""
}

variable "cost_profile_model_key" {
  description = "コスト配賦に使う app_profile_models のキー（inference-profiles.tf）。既定は Haiku"
  type        = string
  default     = "haiku-4-5"
}

variable "channels_json" {
  description = "読み取り対象チャネル定義。[{role:clock|report, team_id, channel_id, label}]"
  type        = string
}

variable "lookback_days" {
  description = "日次取り込みで遡る日数（1 = 前日 00:00 JST 以降。delta を使わず投稿日時でフィルタ）"
  type        = number
  default     = 1
}

variable "daily_cron_jst" {
  description = "日次取り込みの起動時刻（EventBridge Scheduler の cron。JST）"
  type        = string
  default     = "cron(0 5 * * ? *)" # 毎日 05:00 JST（前日分を確定）
}

variable "weekly_cron_jst" {
  description = "週次レポートの起動時刻（月曜 JST）"
  type        = string
  default     = "cron(30 9 ? * MON *)" # 毎週月曜 09:30 JST
}

# 各シークレットの実体は Terraform では投入せず、apply 後に aws ssm put-parameter で設定する
# （state に平文で残さないため）。ここでは空の SecureString 枠だけ作る。
