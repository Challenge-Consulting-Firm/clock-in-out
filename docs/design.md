# 設計書: 勤怠打刻システム（Teams 読み取り × Jev 構造化 × 週次レポート）

## 1. 目的

Teams の 2 チャネル（打刻用・報告用）の投稿を毎日読み取り、社員ごとの勤怠を構造化して蓄積し、
週次で社員×日付の勤怠サマリを専用チャネルへ投稿する。自然文からの構造化は
**TypeSafe Jev** で行い、対象日の列挙・カレンダー計算はコード側（`date_extract.py`）で行う。

打刻チャネル（General）: 出退勤。報告チャネル: 残業・遅刻・早退・休暇・在宅。

## 2. 全体構成

```
[日次] EventBridge Scheduler(毎日 05:00 JST)
  └─▶ Lambda: clock-in-out-ingest (ingest_attendance.py)
        1. SSM から Graph 資格情報取得 → トークン発行（client credentials）
        2. 打刻用/報告用チャネルの「LOOKBACK_DAYS 日前 00:00 JST 以降」を取得
           （delta は使わず投稿日時でフィルタ。メッセージは新しい順に返るので古い所で打ち切り）
        3. 各メッセージを TypeSafe Jev で構造化
        4. S3 に日次 JSONL で正規化保存（raw も監査用に保存）

[週次] EventBridge Scheduler(毎週月曜 09:30 JST)
  └─▶ Lambda: clock-in-out-weekly (weekly_report.py)
        1. S3 から前週(月〜日)の正規化データ＋ LEAVE_LOOKBACK_DAYS（既定21日）前までの JSONL を読む
           （休暇は投稿日のファイルに保存され、target_dates は申請対象日。事前申請を落とさないため）
        2. target_dates が前週に入るレコードを社員ごとに集約
        3. 社員×日付マトリクスを Markdown/CSV 生成
        4. teams.py の post_teams で専用チャネルへ投稿（CSV は S3 保存）

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
| type | clock_in/clock_out/late/early_leave/paid_leave/remote/overtime/unknown |
| time | "HH:MM" or null |
| target_date | 主たる勤怠対象日 "YYYY-MM-DD" |
| target_dates | 対象日の配列。複数日申請（「10日午後休 15日午前休」等）はここに列挙 |
| timing | advance(事前)/after(事後)/null |
| confidence | 0.0〜1.0（Jev の校正済み confidence の最小値） |
| raw_text | 元メッセージ本文 |

午後休は Jev では `paid_leave`（休暇）。早退との差は給与計算上同等として扱う。

## 4. 構造化（Jev）

- API: `POST https://api.typesafe.ai/v1/systemone`（モデル既定 `jev-latest`）
- Choice で type / timing / 日付部品 / 時刻部品を 1 リクエストで並列評価
- 複数日・相対日の確定は `lambda/date_extract.py`（モデルにカレンダー計算を任せない）
- API キーは SSM `/clock-in-out/typesafe-api-key`
- 切戻し用に `lambda/bedrock_parser.py` と Bedrock 推論プロファイルを残置

## 5. 認証・シークレット

- Graph: Azure AD アプリ登録（client credentials / アプリケーション権限 **ChannelMessage.Read.All**）。
- シークレット（Graph 資格情報・Teams webhook URL・TypeSafe API キー）は SSM SecureString。
  Terraform は空枠のみ作成し、実値は `aws ssm put-parameter` で投入（state に平文を残さない）。

## 6. 運用メモ・既知の制約

- Teams webhook は投稿専用（読み取り不可）。読み取りは Graph API が必須。
- webhook URL は署名(sig)付きの完全な URL を使う（不完全だと 401）。
- Markdown 整形: Teams は単独 `\n` がスペースに潰れ、コードブロック非対応 → 桁揃えは Markdown テーブルで。
- Jev の英語偏重により、定型出勤でも confidence が低め（0.3〜0.5）になることがある。閾値ゲートは必須にしない。
- 複合メッセージ（退勤＋休暇）は type が退勤側に寄ることがあり、週次は `target_dates` の非主日付を休暇として展開する。

## 7. 今後（フェーズ2）

- 週次 CSV を Teams チャネルへファイル添付（Graph の driveItem アップロード）。
- 社員マスタ連携（AAD id → 社員番号）。
- 打刻の欠落検知（出勤のみで退勤なし等）のアラート。
