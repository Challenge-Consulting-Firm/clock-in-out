"""週次バッチ: S3 正規化データ → 社員×日付の勤怠表 → Teams 投稿。

EventBridge Scheduler から毎週月曜に起動され、前週（月〜日）の正規化レコードを
社員ごとに集約し、Markdown テーブルで専用チャネルへ投稿する。CSV も S3 に保存する。

Markdown 整形の前提（report_usage.py の実測知見を踏襲）:
  - Teams（Power Automate 経由）は単独 \n がスペースに潰れる（ソフト改行）
  - 空行(\n\n) は段落区切り。コードブロック(```)は非対応
  → 桁揃えは空白でなく Markdown テーブルで行う

環境変数:
  ATTENDANCE_BUCKET   勤怠データ S3 バケット
  WEBHOOK_PARAM       Teams Workflows webhook URL を格納した SSM Parameter 名
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import boto3

from teams import post_teams

logger = logging.getLogger()
logger.setLevel(logging.INFO)

JST = timezone(timedelta(hours=9))

BUCKET = os.environ["ATTENDANCE_BUCKET"]
WEBHOOK_PARAM = os.environ["WEBHOOK_PARAM"]

s3 = boto3.client("s3")
ssm = boto3.client("ssm")

_NORMALIZED_PREFIX = "attendance/normalized"
_REPORT_PREFIX = "attendance/reports"

# 表示用ラベル（type → 日本語）
TYPE_LABELS = {
    "clock_in": "出勤",
    "clock_out": "退勤",
    "late": "遅刻",
    "early_leave": "早退",
    "paid_leave": "有給",
    "absence": "欠勤",
    "remote": "在宅",
    "out_of_office": "終日外出",
    "overtime": "残業",
    "other": "報告",
    "unknown": "不明",
}


def _prev_week_range(today_jst: datetime) -> tuple[datetime, datetime]:
    """今日(月曜想定)から見た前週の月曜〜日曜を返す。"""
    this_monday = (today_jst - timedelta(days=today_jst.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    start = this_monday - timedelta(days=7)
    end = this_monday - timedelta(days=1)
    return start, end


def _load_records(start: datetime, end: datetime) -> list[dict]:
    records = []
    day = start
    while day <= end:
        key = f"{_NORMALIZED_PREFIX}/{day:%Y/%m/%d}.jsonl"
        try:
            body = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read().decode("utf-8")
        except s3.exceptions.NoSuchKey:
            day += timedelta(days=1)
            continue
        for line in body.splitlines():
            line = line.strip()
            if line:
                records.append(json.loads(line))
        day += timedelta(days=1)
    return records


# 独立して1行表示する在席状態（出退勤とは別に、その日の勤務形態を表す）。
_STANDALONE_LABELS = {
    "remote": "在宅",
    "out_of_office": "終日外出",
    "paid_leave": "有給",
    "absence": "欠勤",
}


def _format_cell_lines(day_records: list[dict]) -> list[str]:
    """同一社員・同一日のレコード群を「状態／出勤／退勤」の表示行に整形する。

    形式:
        在宅              （remote 等の在席状態があれば）
        出勤：08:58（遅刻） （clock_in。late があれば注記）
        退勤：19:40（残業） （clock_out。overtime/early_leave があれば注記）
    """
    types = [r.get("type") for r in day_records]

    def first_time(*wanted):
        for r in day_records:
            if r.get("type") in wanted and r.get("time"):
                return r["time"]
        return None

    lines: list[str] = []

    # 1) 在席状態（独立行）。重複は除きつつ出現順を保つ
    seen = set()
    for r in day_records:
        lbl = _STANDALONE_LABELS.get(r.get("type"))
        if lbl and lbl not in seen:
            seen.add(lbl)
            lines.append(lbl)

    # 2) 出勤行（遅刻注記）
    in_time = first_time("clock_in") or first_time("late")
    in_notes = ["遅刻"] if "late" in types else []
    if in_time or "clock_in" in types:
        note = f"（{'／'.join(in_notes)}）" if in_notes else ""
        lines.append(f"出勤：{in_time or '—'}{note}")
    elif in_notes:  # 遅刻連絡のみで出勤打刻なし
        lines.append(f"出勤：{in_time or '—'}（{'／'.join(in_notes)}）")

    # 3) 退勤行（残業／早退注記）
    out_time = first_time("clock_out") or first_time("early_leave")
    out_notes = []
    if "overtime" in types:
        out_notes.append("残業")
    if "early_leave" in types:
        out_notes.append("早退")
    if out_time or "clock_out" in types or out_notes:
        note = f"（{'／'.join(out_notes)}）" if out_notes else ""
        lines.append(f"退勤：{out_time or '—'}{note}")

    return lines


def _aggregate(records: list[dict], start: datetime):
    """employee_name → {date_str → [表示行,...]} に集約（状態／出勤／退勤の3系統に整形）。"""
    dates = [(start + timedelta(days=i)) for i in range(7)]
    date_strs = [f"{d:%m/%d}" for d in dates]
    iso_dates = [f"{d:%Y-%m-%d}" for d in dates]

    # まず (emp, col) ごとに、投稿時刻順でレコードを溜める
    grouped: dict[str, dict[str, list[tuple[str, dict]]]] = {}
    for r in records:
        emp = r.get("employee_name") or r.get("employee_id") or "(unknown)"
        td = r.get("target_date") or ""
        if td not in iso_dates:
            continue
        col = date_strs[iso_dates.index(td)]
        sort_key = r.get("posted_at_jst") or "9999"
        grouped.setdefault(emp, {}).setdefault(col, []).append((sort_key, r))

    by_emp: dict[str, dict[str, list[str]]] = {}
    for emp, cols in grouped.items():
        by_emp[emp] = {}
        for col, entries in cols.items():
            day_records = [r for _, r in sorted(entries, key=lambda x: x[0])]
            by_emp[emp][col] = _format_cell_lines(day_records)
    return by_emp, date_strs


def _build_markdown(period_label: str, by_emp: dict, date_strs: list[str]) -> str:
    header = "| 社員 | " + " | ".join(date_strs) + " |"
    sep = "|:--|" + "".join(["--:|"] * len(date_strs))
    lines = [f"**【勤怠週次サマリ】{period_label}**", "", header, sep]
    for emp in sorted(by_emp):
        cells = []
        for col in date_strs:
            entries = by_emp[emp].get(col, [])
            cells.append("<br>".join(entries) if entries else "-")
        lines.append(f"| {emp} | " + " | ".join(cells) + " |")
    if not by_emp:
        lines.append("| (該当データなし) |" + " |" * len(date_strs))
    return "\n".join(lines)


def _build_csv(by_emp: dict, date_strs: list[str]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["社員"] + date_strs)
    for emp in sorted(by_emp):
        writer.writerow([emp] + [" / ".join(by_emp[emp].get(c, [])) for c in date_strs])
    return buf.getvalue()


def lambda_handler(event, context):
    today = datetime.now(JST)
    start, end = _prev_week_range(today)
    period_label = f"{start:%Y-%m-%d}〜{end:%Y-%m-%d}"

    records = _load_records(start, end)
    by_emp, date_strs = _aggregate(records, start)

    # CSV を S3 に保存（Graph 添付はフェーズ2。まずはリンク可能な成果物として残す）
    csv_body = _build_csv(by_emp, date_strs)
    csv_key = f"{_REPORT_PREFIX}/{start:%Y-%m-%d}_{end:%Y-%m-%d}.csv"
    s3.put_object(Bucket=BUCKET, Key=csv_key, Body=csv_body.encode("utf-8-sig"))

    message = _build_markdown(period_label, by_emp, date_strs)
    webhook_url = ssm.get_parameter(Name=WEBHOOK_PARAM, WithDecryption=True)["Parameter"]["Value"]
    post_teams(webhook_url, message)

    logger.info("週次レポート投稿完了 period=%s 社員数=%d", period_label, len(by_emp))
    return {"period": period_label, "employees": len(by_emp), "csv_key": csv_key}
