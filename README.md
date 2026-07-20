# clock-in-out — Teams 勤怠打刻システム

Teams の 2 チャネル（打刻用・報告用）を毎日読み取り、**Bedrock Claude（国内完結・jp. プロファイル）**で
勤怠情報を構造化して S3 に蓄積し、週次で社員×日付の勤怠サマリを専用チャネルへ投稿する。

- 読み取り: Microsoft Graph API（`ChannelMessage.Read.All`, client credentials）
- 構造化: Bedrock Converse（`jp.anthropic.claude-*`・国内完結を IAM で強制）
- 蓄積: S3（正規化 JSONL / 生メッセージ / 週次 CSV）
- 通知: Teams Workflows webhook（`lambda/teams.py`）
- 起動: EventBridge Scheduler（日次 05:00 JST / 週次 月曜 09:30 JST）

設計の詳細は [docs/design.md](docs/design.md)。Bedrock 国内完結・IAM 統制の元ネタは
参考リポジトリ `Challenge-Consulting-Firm/editor-claude-bedrock`。

## 構成

```
lambda/
  graph_client.py       Graph トークン取得 + チャネルメッセージ取得（投稿日時フィルタ）
  bedrock_parser.py     Converse で勤怠メッセージを構造化（jp.）
  teams.py              Teams webhook 投稿（参考リポジトリから流用）
  ingest_attendance.py  日次ハンドラ（Graph→Bedrock→S3）
  weekly_report.py      週次ハンドラ（S3→集計→Teams）
infra/                  Terraform（S3 / IAM jp限定 / Lambda×2 / Scheduler×2 / SSM）
```

## デプロイ

```bash
cd infra
terraform init
terraform apply -var 'channels_json=[{"role":"clock","team_id":"...","channel_id":"...","label":"打刻"},{"role":"report","team_id":"...","channel_id":"...","label":"報告"}]'

# シークレットを投入（state に平文で残さないため apply 後に）
aws ssm put-parameter --name /clock-in-out/graph-credentials --type SecureString --overwrite \
  --value '{"tenant_id":"...","client_id":"...","client_secret":"..."}'
aws ssm put-parameter --name /clock-in-out/teams-webhook-url --type SecureString --overwrite \
  --value '<署名付き webhook URL>'
```

## 動作確認

```bash
# 日次取り込みを手動実行
aws lambda invoke --function-name clock-in-out-ingest /dev/stdout
# 週次レポートを手動実行
aws lambda invoke --function-name clock-in-out-weekly /dev/stdout
# 国内完結の確認: CloudTrail で InvokeModel/Converse の inferenceRegion が ap-northeast-1/3 か
```

## 事前準備（Azure 側）

1. Azure AD でアプリ登録し、**アプリケーション権限** `ChannelMessage.Read.All` を付与して管理者同意。
2. クライアントシークレットを発行し、対象チームの `team_id` / `channel_id` を控える。
3. 週次投稿先チャネルに Teams Workflows（Power Automate「Webhook 要求を受信したとき」）を作成し webhook URL を取得。
