"""日次バッチ: Teams 2チャネル → Bedrock 構造化 → S3 蓄積。

EventBridge Scheduler から毎日起動され:
  1. SSM から Graph 資格情報を取得しトークン発行
  2. 打刻用/報告用チャネルの「LOOKBACK_DAYS 日前 00:00 JST 以降」のメッセージを取得
     （delta は使わず投稿日時でフィルタ — 初回に全履歴を拾う問題を避ける）
  3. 各メッセージを Bedrock Claude(jp.) で構造化（送信者ID を社員キーに採用）
  4. 正規化済みレコードを S3 に日次 JSONL で蓄積（週次集計の元データ）

環境変数:
  ATTENDANCE_BUCKET      勤怠データ格納 S3 バケット
  GRAPH_SECRET_PARAM     SSM Parameter 名（JSON: {tenant_id, client_id, client_secret}）
  CHANNELS_JSON          [{"role":"clock"|"report","team_id":..,"channel_id":..,"label":..}]
  LOOKBACK_DAYS          取得対象の遡り日数（既定 1 = 前日 00:00 JST 以降）
  BEDROCK_MODEL_ID       (bedrock_parser 側で参照)
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

import boto3

from bedrock_parser import parse_message
from graph_client import extract_plaintext, fetch_messages_and_replies_since, get_token

logger = logging.getLogger()
logger.setLevel(logging.INFO)

JST = timezone(timedelta(hours=9))
UTC = timezone.utc

BUCKET = os.environ["ATTENDANCE_BUCKET"]
GRAPH_SECRET_PARAM = os.environ["GRAPH_SECRET_PARAM"]
CHANNELS = json.loads(os.environ["CHANNELS_JSON"])
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "1"))

s3 = boto3.client("s3")
ssm = boto3.client("ssm")

_RAW_PREFIX = "attendance/raw"
_NORMALIZED_PREFIX = "attendance/normalized"


def _load_graph_secret() -> dict:
    val = ssm.get_parameter(Name=GRAPH_SECRET_PARAM, WithDecryption=True)["Parameter"]["Value"]
    return json.loads(val)


def _since_iso_utc(now_jst: datetime) -> str:
    """LOOKBACK_DAYS 日前の JST 0:00 を UTC ISO8601 で返す（Graph の createdDateTime と比較用）。"""
    start_jst = (now_jst - timedelta(days=LOOKBACK_DAYS)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start_jst.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _sender(message: dict) -> tuple[str, str]:
    """(employee_id, employee_name) を送信者から取り出す。ボット/システム投稿は空 ID。"""
    user = ((message.get("from") or {}).get("user")) or {}
    return user.get("id") or "", user.get("displayName") or "(unknown)"


def _to_jst_str(iso_utc: str) -> str:
    # Graph の createdDateTime は小数秒の桁数がまちまち（".123Z" / ".75Z" 等）。
    # 秒精度で十分なので小数秒を除去してからパースする
    # （古い Python の fromisoformat は小数秒 3/6 桁以外を弾くため）。
    s = re.sub(r"\.\d+", "", iso_utc.replace("Z", "+00:00"))
    dt = datetime.fromisoformat(s).astimezone(JST)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _process_channel(token: str, channel: dict, run_date: str, since_iso_utc: str) -> list[dict]:
    role = channel["role"]
    channel_id = channel["channel_id"]
    # 打刻はスレッド返信で行われることが多いため、親＋返信をまとめて取得する
    messages = fetch_messages_and_replies_since(token, channel["team_id"], channel_id, since_iso_utc)
    logger.info("channel=%s role=%s 取得 %d 件（親＋返信）", channel.get("label", channel_id), role, len(messages))

    records = []
    for msg in messages:
        # 削除/システムメッセージ・本文空はスキップ
        if msg.get("deletedDateTime") or msg.get("messageType") not in (None, "message"):
            continue
        employee_id, employee_name = _sender(msg)
        if not employee_id:
            continue
        text = extract_plaintext(msg)
        if not text:
            continue
        posted_at = _to_jst_str(msg["createdDateTime"])
        parsed = parse_message(text, posted_at, employee_name, role)
        records.append(
            {
                "message_id": msg.get("id"),
                "channel_label": channel.get("label", channel_id),
                "channel_role": role,
                "employee_id": employee_id,
                "employee_name": employee_name,
                "posted_at_jst": posted_at,
                "raw_text": text,
                **parsed,
            }
        )

    # 生メッセージも監査用に保存（チャネル×実行日）
    s3.put_object(
        Bucket=BUCKET,
        Key=f"{_RAW_PREFIX}/{run_date[:4]}/{run_date[5:7]}/{run_date[8:10]}/{channel_id}.json",
        Body=json.dumps(messages, ensure_ascii=False).encode("utf-8"),
    )
    return records


def lambda_handler(event, context):
    now_jst = datetime.now(JST)
    run_date = now_jst.strftime("%Y-%m-%d")
    since_iso_utc = _since_iso_utc(now_jst)
    secret = _load_graph_secret()
    token = get_token(secret["tenant_id"], secret["client_id"], secret["client_secret"])

    all_records = []
    for channel in CHANNELS:
        all_records.extend(_process_channel(token, channel, run_date, since_iso_utc))

    # 正規化レコードを JSONL で日次保存（週次集計はこれを読む）
    if all_records:
        body = "\n".join(json.dumps(r, ensure_ascii=False) for r in all_records).encode("utf-8")
        key = f"{_NORMALIZED_PREFIX}/{run_date[:4]}/{run_date[5:7]}/{run_date[8:10]}.jsonl"
        s3.put_object(Bucket=BUCKET, Key=key, Body=body)

    logger.info("正規化レコード %d 件を保存", len(all_records))
    return {"date": run_date, "records": len(all_records)}
