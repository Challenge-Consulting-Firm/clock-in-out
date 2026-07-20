"""勤怠メッセージを Bedrock Claude（jp. プロファイル・国内完結）で構造化する。

boto3 の bedrock-runtime.converse を使用。モデルは環境変数で指定し、
デフォルトは jp. の軽量モデル（Haiku）。コスト配賦したい場合はアプリケーション
推論プロファイル ARN を指定する（infra/inference-profiles.tf）。

国内完結の担保:
  - モデル ID は jp. プロファイル or その複製アプリケーション推論プロファイルのみ
  - IAM 側（infra/main.tf）で jp. 以外の推論を Deny 済み
  - 実処理リージョンは CloudTrail の inferenceRegion で事後監査
"""

import json
import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "jp.anthropic.claude-haiku-4-5-20251001-v1:0")

_bedrock = boto3.client("bedrock-runtime")

# チャネルの役割ごとに期待する type を絞ると精度が上がる。
CLOCK_TYPES = ["clock_in", "clock_out"]
REPORT_TYPES = ["late", "early_leave", "paid_leave", "absence", "remote", "out_of_office", "overtime", "other"]

_SYSTEM_PROMPT = """あなたは勤怠管理アシスタントです。社員が Teams に投稿した1件のメッセージから勤怠情報を抽出し、指定された JSON スキーマだけを返します。説明文やコードブロックは一切付けず、JSON オブジェクトのみを出力してください。

判定ルール:
- type は次のいずれか:
  - clock_in(出勤打刻)
  - clock_out(退勤打刻)
  - late(遅刻)
  - early_leave(早退)
  - paid_leave(有給休暇)
  - absence(欠勤)
  - remote(在宅勤務・リモートワーク・テレワーク)
  - out_of_office(終日外出・直行直帰・研修や人間ドック等で終日オフィス外だが休みではない)
  - overtime(残業)
  - other(上記に当てはまらない勤怠に関する報告)
  - unknown(勤怠と無関係。営業電話の共有・雑談・業務連絡など)
- 判定のヒント（重要な運用ルール）:
  - 「おはようございます」「出勤します」等の朝の挨拶・出勤連絡 → clock_in（出勤打刻）。本文に時刻が無くても投稿時刻が出勤時刻。
  - 「お疲れ様でした」「退勤します」等の終業の挨拶・退勤連絡 → clock_out（退勤打刻）。本文に時刻が無くても投稿時刻が退勤時刻。
  - 「本日は〇〇様に直行します」「直行します」→ clock_in（客先直行はその日の勤務開始＝出勤打刻。投稿時刻が出勤時刻）。
  - 「直帰します」→ clock_out（客先から直帰はその日の勤務終了＝退勤打刻）。
  - 「在宅」「リモート」「テレワーク」→ remote
  - 「終日外出」「研修で終日〇〇にいます」「人間ドック」など、その日オフィスにも自席にもおらず通常勤務でない → out_of_office
    （注意: 単なる「直行/直帰」は通常勤務の一部なので out_of_office ではなく clock_in/clock_out）
  - 「営業電話の共有」「〜の件です」等の業務連絡で勤怠に関係しないもの → unknown
- time は "HH:MM"（24時間）。本文に明示時刻（「10時出社」等）があればそれを使う。無ければ null でよい（当日打刻の time は後処理で投稿時刻から補完する）。
- target_date はその勤怠が対象とする日付 "YYYY-MM-DD"。明示が無ければ投稿日を使う。翌日/来週など相対表現は投稿日から解決する。「7/16」のような月日表記は投稿年で補完する。
- timing は事前報告なら "advance"、事後報告や当日の打刻・連絡なら "after"、判断できなければ null。
  「明日休みます」「来週有給です」は advance。「おはようございます」「お疲れ様でした」「本日在宅にします」は当日なので after または null。
- confidence は 0.0〜1.0。勤怠と無関係な雑談・業務連絡は type="unknown" で confidence を低くする。"""


def _build_user_prompt(text: str, posted_at_jst: str, sender_name: str, channel_role: str) -> str:
    allowed = CLOCK_TYPES if channel_role == "clock" else REPORT_TYPES
    return f"""チャネル種別: {channel_role}（このチャネルで想定される主な type: {", ".join(allowed)}）
投稿者: {sender_name}
投稿日時(JST): {posted_at_jst}
メッセージ本文:
\"\"\"{text}\"\"\"

次のスキーマの JSON だけを返してください:
{{"type": "...", "time": "HH:MM"|null, "target_date": "YYYY-MM-DD", "timing": "advance"|"after"|null, "confidence": 0.0}}"""


def parse_message(text: str, posted_at_jst: str, sender_name: str, channel_role: str) -> dict:
    """1件のメッセージを構造化 dict にして返す。失敗時は type=unknown で退避。"""
    if not text or not text.strip():
        return {"type": "unknown", "time": None, "target_date": posted_at_jst[:10], "timing": None, "confidence": 0.0}

    try:
        resp = _bedrock.converse(
            modelId=MODEL_ID,
            system=[{"text": _SYSTEM_PROMPT}],
            messages=[{"role": "user", "content": [{"text": _build_user_prompt(text, posted_at_jst, sender_name, channel_role)}]}],
            inferenceConfig={"maxTokens": 512, "temperature": 0.0},
        )
        raw = resp["output"]["message"]["content"][0]["text"].strip()
        return _coerce(_extract_json(raw), posted_at_jst)
    except Exception as exc:  # noqa: BLE001 - 1件の失敗で日次全体を止めない
        logger.warning("Bedrock 構造化に失敗（type=unknown で退避）: %s", exc)
        return {"type": "unknown", "time": None, "target_date": posted_at_jst[:10], "timing": None, "confidence": 0.0}


def _extract_json(raw: str) -> dict:
    """コードフェンス付き等で返ってきても JSON 本体を取り出す。"""
    s = raw.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
    start, end = s.find("{"), s.rfind("}")
    if start != -1 and end != -1:
        s = s[start : end + 1]
    return json.loads(s)


# 打刻時刻を「投稿時刻」で補完する type（当日の実打刻を表すもの）。
# 事前報告（timing=advance）は将来日の予定なので投稿時刻を打刻時刻にしない。
_PUNCH_TYPES = {"clock_in", "clock_out", "late", "early_leave"}


def _coerce(obj: dict, posted_at_jst: str) -> dict:
    """欠損キーを埋め、型を整える。当日打刻で time が空なら投稿時刻(HH:MM)で補完。"""
    ptype = obj.get("type") or "unknown"
    time = obj.get("time")
    timing = obj.get("timing")
    # posted_at_jst は "YYYY-MM-DD HH:MM:SS"。当日打刻で time 未取得なら投稿時刻を採用。
    if not time and ptype in _PUNCH_TYPES and timing != "advance":
        time = posted_at_jst[11:16] or None
    return {
        "type": ptype,
        "time": time,
        "target_date": obj.get("target_date") or posted_at_jst[:10],
        "timing": timing,
        "confidence": float(obj.get("confidence", 0.0) or 0.0),
    }
