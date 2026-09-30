# clock-in-out — Teams 勤怠打刻システム

Teams の 2 チャネル（打刻用・報告用）を毎日読み取り、**TypeSafe Jev** で
勤怠情報を構造化して S3 に蓄積し、週次で社員×日付の勤怠サマリを専用チャネルへ投稿する。

- 読み取り: Microsoft Graph API（`ChannelMessage.Read.All`, client credentials）
- 構造化: TypeSafe Jev（Choice で種別判定。日付列挙は `lambda/date_extract.py`）
- 蓄積: S3（正規化 JSONL / 生メッセージ / 週次 CSV）
- 通知: Teams Workflows webhook（`lambda/teams.py`）
- 起動: EventBridge Scheduler（日次 05:00 JST / 週次 月曜 09:30 JST）

設計の詳細は [docs/design.md](docs/design.md)。

## 構成

```
lambda/
  graph_client.py       Graph トークン取得 + チャネルメッセージ取得（投稿日時フィルタ）
  jev_parser.py         TypeSafe Jev で勤怠メッセージを構造化
  date_extract.py       本文から対象日リストを抽出（複数日休暇）
  bedrock_parser.py     切戻し用（Bedrock Claude）
  teams.py              Teams webhook 投稿
  ingest_attendance.py  日次ハンドラ（Graph→Jev→S3）
  weekly_report.py      週次ハンドラ（S3→集計→Teams）
infra/                  Terraform（S3 / IAM / Lambda×2 / Scheduler×2 / SSM）
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
aws ssm put-parameter --name /clock-in-out/typesafe-api-key --type SecureString --overwrite \
  --value '<TypeSafe API key>'
```

## 動作確認

```bash
# 日次取り込みを手動実行
aws lambda invoke --function-name clock-in-out-ingest /dev/stdout
# 週次レポートを手動実行
aws lambda invoke --function-name clock-in-out-weekly /dev/stdout
```

Claude との比較はリポジトリルートで:

```bash
python3 scripts/compare_claude_jev.py --days 21
```

## 事前準備（Azure 側）

1. Azure AD でアプリ登録し、**アプリケーション権限** `ChannelMessage.Read.All` を付与して管理者同意。
2. クライアントシークレットを発行し、対象チームの `team_id` / `channel_id` を控える。
3. 週次投稿先チャネルに Teams Workflows（Power Automate「Webhook 要求を受信したとき」）を作成し webhook URL を取得。
