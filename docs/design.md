# 設計書: 勤怠打刻システム（Teams 読み取り × Bedrock 構造化 × 週次レポート）

## 1. 目的

Teams の 2 チャネル（打刻用・報告用）の投稿を毎日読み取り、社員ごとの勤怠を構造化して蓄積し、
週次で社員×日付の勤怠サマリを専用チャネルへ投稿する。自然文からの構造化は
**Bedrock Claude（国内完結・jp. プロファイル）** で行い、コード・勤怠情報を国外 LLM SaaS へ出さない。

参考実装 Challenge-Consulting-Firm/editor-claude-bedrock（Bedrock 国内完結 PoC）から、
Teams 投稿ユーティリティ（`teams.py`）・週次 EventBridge→Lambda→Teams の型・jp. 限定 IAM 設計を流用。

## 2. 全体構成

```
[日次] EventBridge Scheduler(毎日 05:00 JST)
  └─▶ Lambda: clock-in-out-ingest (ingest_attendance.py)
        1. SSM から Graph 資格情報取得 → トークン発行（client credentials）
        2. 打刻用/報告用チャネルの「LOOKBACK_DAYS 日前 00:00 JST 以降」を取得
           （delta は使わず投稿日時でフィルタ。メッセージは新しい順に返るので古い所で打ち切り）
        3. 各メッセージを Bedrock Claude(jp.) で構造化
        4. S3 に日次 JSONL で正規化保存（raw も監査用に保存）

[週次] EventBridge Scheduler(毎週月曜 09:30 JST)
  └─▶ Lambda: clock-in-out-weekly (weekly_report.py)
        1. S3 から前週(月〜日)の正規化データを社員ごとに集約
        2. 社員×日付マトリクスを Markdown/CSV 生成
        3. teams.py の post_teams で専用チャネルへ投稿（CSV は S3 保存）

S3 レイアウト:
  attendance/raw/YYYY/MM/DD/{channel_id}.json … 生メッセージ（監査）
  attendance/normalized/YYYY/MM/DD.jsonl      … 正規化レコード（集計の元）
  attendance/reports/{start}_{end}.csv        … 週次 CSV 成果物
```

## 3. データモデル（正規化レコード）

| フィールド | 内容 |
|---|---|
| message_id | Teams メッセージ ID（冪等キー） |
| channel_role | clock / report |
| employee_id | 送信者 AAD user id（**社員特定キー**・マスタ不要） |
| employee_name | 送信者表示名 |
| posted_at_jst | 投稿日時(JST) |
| type | clock_in/clock_out/late/early_leave/paid_leave/absence/overtime/other/unknown |
| time | "HH:MM" or null |
| target_date | 勤怠対象日 "YYYY-MM-DD"（相対表現を投稿日から解決） |
| timing | advance(事前)/after(事後)/null |
| confidence | 0.0〜1.0 |
| raw_text | 元メッセージ本文 |

## 4. 国内完結の担保（Bedrock jp.）

- モデルは `jp.` プレフィックスのシステムプロファイル、またはその複製アプリケーション推論プロファイルのみ。
- Lambda 実行ロールの IAM で jp. 以外の推論を **物理的に Deny**（`infra/main.tf`。参考リポジトリの設計を踏襲）。
- 実処理リージョンは CloudTrail の `additionalEventData.inferenceRegion`（ap-northeast-1/3）で事後監査。

## 4.5 コスト配賦（他の Bedrock 利用との区別）

- Bedrock のオンデマンド推論は**リソース非依存の課金**のため、リソースタグだけでは推論コストを配賦できない。
  そこで jp. システムプロファイルを複製した**タグ付きアプリケーション推論プロファイル**（`infra/inference-profiles.tf`）を作り、
  Lambda はこの ARN 経由で Bedrock を呼ぶ（`var.bedrock_model_id` 未指定時は自動でこの ARN を使用）。
- タグ: provider の `default_tags`（`Project=clock-in-out`）に加え、プロファイルに `Workload=clock-in-out-attendance` / `Model=<key>`。
  → Cost Explorer で他アカウント・他用途の Bedrock 利用と推論コストを明確に区別できる。
- 前提: 課金データにタグが現れた後（利用開始から最大24h）、コスト配分タグを有効化する:
  `aws ce update-cost-allocation-tags-status --cost-allocation-tags-status TagKey=Project,Status=Active TagKey=Workload,Status=Active`
- AWS プロファイル/アカウントは用途ごとに分離することを推奨（他の Bedrock 利用とアカウントを分ければ、タグ配賦と併せて区別がより明確になる）。

## 5. 認証・シークレット

- Graph: Azure AD アプリ登録（client credentials / アプリケーション権限 **ChannelMessage.Read.All**）。
- シークレット（Graph 資格情報・Teams webhook URL）は SSM SecureString。Terraform は空枠のみ作成し、
  実値は `aws ssm put-parameter` で投入（state に平文を残さない）。

## 6. 運用メモ・既知の制約

- Teams webhook は投稿専用（読み取り不可）。読み取りは Graph API が必須。
- webhook URL は署名(sig)付きの完全な URL を使う（不完全だと 401。参考リポジトリの実測）。
- Markdown 整形: Teams は単独 `\n` がスペースに潰れ、コードブロック非対応 → 桁揃えは Markdown テーブルで。
- スレッド返信（replies）での打刻運用がある場合は `graph_client.get_message_replies` で拾う拡張が必要。
- 構造化精度は既定 Haiku。不足時は `BEDROCK_MODEL_ID` を jp. Sonnet に切替。

## 7. 今後（フェーズ2）

- 週次 CSV を Teams チャネルへファイル添付（Graph の driveItem アップロード）。
- 社員マスタ連携（AAD id → 社員番号）。
- 打刻の欠落検知（出勤のみで退勤なし等）のアラート。
- Budget ソフト通知（参考リポジトリ budget.tf を流用）。
