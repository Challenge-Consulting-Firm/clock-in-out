"""勤怠メッセージを TypeSafe Jev で構造化する。

Choice で type / timing / 日付部品 / 時刻部品を並列評価し、カレンダー計算はコード側
（date_extract）で行う。API キーは環境変数 TYPESAFE_API_KEY、無ければ
TYPESAFE_API_KEY_PARAM の SSM SecureString。
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

from date_extract import merge_target_dates

logger = logging.getLogger()
logger.setLevel(logging.INFO)

CLOCK_TYPES = ["clock_in", "clock_out", "unknown"]
REPORT_TYPES = ["late", "early_leave", "paid_leave", "remote", "overtime", "unknown"]

MONTHS = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

_PUNCH_TYPES = {"clock_in", "clock_out", "late", "early_leave"}

_api_key_cache: str | None = None


def _api_key() -> str:
    global _api_key_cache
    if _api_key_cache:
        return _api_key_cache
    key = os.environ.get("TYPESAFE_API_KEY") or ""
    if not key:
        param = os.environ.get("TYPESAFE_API_KEY_PARAM")
        if param:
            import boto3

            key = boto3.client("ssm").get_parameter(Name=param, WithDecryption=True)["Parameter"]["Value"]
    if not key:
        raise RuntimeError("TYPESAFE_API_KEY も TYPESAFE_API_KEY_PARAM も未設定")
    _api_key_cache = key
    return key


def _choice(instructions, criteria: dict) -> dict:
    return {"type": "choice", "instructions": instructions, "criteria": criteria}


def questions_for(channel_role: str) -> dict:
    types = CLOCK_TYPES if channel_role == "clock" else REPORT_TYPES
    type_criteria = {
        "clock_in": "朝の出勤連絡・おはようございます・出勤します・客先直行（通常勤務の開始）",
        "clock_out": "退勤連絡・お疲れ様でした・退勤します・直帰",
        "late": "遅刻の報告",
        "early_leave": "早退の報告",
        "paid_leave": "休暇・有給・休みの申請や報告（半休含む）。在宅勤務ではない",
        "remote": "在宅勤務・リモートワーク・テレワーク。休暇や休みではない",
        "overtime": "残業の報告",
        "unknown": "勤怠と無関係、または上記に当てはまらない",
    }
    absent = "この日付の種類ではない、または本文に書かれていない"
    return {
        "type": _choice(
            {
                "question": "この Teams 投稿の勤怠種別はどれか",
                "focus": "本文の主目的だけを分類する。在宅・リモートは remote。休暇・半休は paid_leave。雑談・業務連絡は unknown。",
            },
            {t: type_criteria[t] for t in types},
        ),
        "timing": _choice(
            "この勤怠は事前申請か、当日・事後の連絡か",
            {
                "advance": "明日・来週など、投稿日より後の予定の申請",
                "after": "当日の打刻・当日の連絡・事後報告",
                "unknown": "判断できない",
            },
        ),
        "date_mode": _choice(
            "勤怠の対象日は本文でどう書かれているか",
            {
                "posted": "日付の指定がなく、投稿日そのものが対象",
                "absolute": "カレンダー日付（7/16、9月30日、2026-09-30 など）",
                "relative": "相対日付（明日、明後日、来週月曜、今週金曜 など）",
                "none": "対象日が読み取れない",
            },
        ),
        "month": _choice(
            "絶対日付なら何月か。相対日付や日付なしなら none",
            {m: None for m in MONTHS} | {"none": absent},
        ),
        "day": _choice(
            "絶対日付なら何日か（1-31）。相対日付や日付なしなら none",
            {str(d): None for d in range(1, 32)} | {"none": absent},
        ),
        "year": _choice(
            "絶対日付に西暦年が書いてあればその年。書いていなければ none",
            {str(y): None for y in range(2024, 2028)} | {"none": "年は書かれていない", "out_of_range": "リスト外の年"},
        ),
        "day_anchor": _choice(
            "相対日付ならどれか。絶対日付や日付なしなら none",
            {
                "today": "本日・今日",
                "tomorrow": "明日",
                "day_after": "明後日",
                "weekday": "曜日指定（月曜、来週金曜 など）",
                "none": absent,
            },
        ),
        "weekday": _choice(
            "曜日指定なら何曜日か。そうでなければ none",
            {w: None for w in WEEKDAYS} | {"none": absent},
        ),
        "week_offset": _choice(
            "曜日指定の週はどれか",
            {
                "current": "今週・今週の金曜 など",
                "next": "来週・来週月曜 など",
                "none": "今週来週の指定なし（次に来るその曜日）",
            },
        ),
        "hour": _choice(
            "本文に明示された時刻の時（0-23）。無ければ none。投稿時刻そのものは使わない",
            {str(h): None for h in range(24)} | {"none": "明示時刻なし"},
        ),
        "minute": _choice(
            "本文に明示された時刻の分（0-59）。無ければ none",
            {str(m): None for m in range(60)} | {"none": "明示時刻なし"},
        ),
    }


def _post(state, questions: dict, api_key: str, model: str, retries: int = 6) -> dict:
    body = json.dumps({"state": state, "model": model, "questions": questions}, ensure_ascii=False).encode("utf-8")
    base = os.environ.get("TYPESAFE_BASE_URL", "https://api.typesafe.ai").rstrip("/")
    req = urllib.request.Request(
        f"{base}/v1/systemone",
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    delay = 1.0
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                return json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            if exc.code in (429, 529) and attempt < retries - 1:
                time.sleep(delay)
                delay = min(delay * 2, 20)
                continue
            raise RuntimeError(f"Jev HTTP {exc.code}: {detail}") from exc
    raise RuntimeError("Jev retries exhausted")


def _choice_of(answers: dict, key: str) -> tuple[str | None, float | None]:
    ans = answers.get(key) or {}
    return ans.get("choice"), ans.get("confidence")


def assemble(answers: dict, posted_at_jst: str) -> dict:
    posted = datetime.strptime(posted_at_jst[:19], "%Y-%m-%d %H:%M:%S")
    today = posted.date()
    ptype, type_conf = _choice_of(answers, "type")
    timing, timing_conf = _choice_of(answers, "timing")
    mode, mode_conf = _choice_of(answers, "date_mode")
    confs = [c for c in (type_conf, timing_conf, mode_conf) if c is not None]

    target = today
    if mode == "absolute":
        month_name, month_conf = _choice_of(answers, "month")
        day_s, day_conf = _choice_of(answers, "day")
        year_s, year_conf = _choice_of(answers, "year")
        confs += [c for c in (month_conf, day_conf, year_conf) if c is not None]
        month_i = MONTHS.index(month_name) + 1 if month_name in MONTHS else None
        day_i = int(day_s) if day_s and str(day_s).isdigit() else None
        if month_i and day_i:
            year_i = today.year if year_s in (None, "none") else (int(year_s) if str(year_s).isdigit() else None)
            if year_i:
                try:
                    target = date(year_i, month_i, day_i)
                    if year_s in (None, "none") and target < today - timedelta(days=31):
                        target = date(year_i + 1, month_i, day_i)
                except ValueError:
                    target = today
    elif mode == "relative":
        anchor, anchor_conf = _choice_of(answers, "day_anchor")
        if anchor_conf is not None:
            confs.append(anchor_conf)
        if anchor == "today":
            target = today
        elif anchor == "tomorrow":
            target = today + timedelta(days=1)
        elif anchor == "day_after":
            target = today + timedelta(days=2)
        elif anchor == "weekday":
            weekday, w_conf = _choice_of(answers, "weekday")
            offset, o_conf = _choice_of(answers, "week_offset")
            confs += [c for c in (w_conf, o_conf) if c is not None]
            if weekday in WEEKDAYS:
                w = WEEKDAYS.index(weekday)
                this_monday = today - timedelta(days=today.weekday())
                if offset == "next":
                    target = this_monday + timedelta(days=7 + w)
                elif offset == "current":
                    target = this_monday + timedelta(days=w)
                else:
                    target = today + timedelta(days=(w - today.weekday()) % 7)

    hour_s, hour_conf = _choice_of(answers, "hour")
    minute_s, minute_conf = _choice_of(answers, "minute")
    time_str = None
    if hour_s not in (None, "none") and str(hour_s).isdigit():
        minute_i = int(minute_s) if minute_s not in (None, "none") and str(minute_s).isdigit() else 0
        time_str = f"{int(hour_s):02d}:{minute_i:02d}"
        confs += [c for c in (hour_conf, minute_conf) if c is not None]

    if not time_str and ptype in _PUNCH_TYPES and timing != "advance":
        time_str = posted_at_jst[11:16]

    return {
        "type": ptype or "unknown",
        "time": time_str,
        "target_date": target.isoformat(),
        "timing": None if timing in (None, "unknown") else timing,
        "confidence": float(min(confs) if confs else 0.0),
    }


def parse_message(text: str, posted_at_jst: str, sender_name: str, channel_role: str) -> dict:
    """1件のメッセージを構造化 dict にして返す。失敗時は type=unknown で退避。"""
    empty = {"type": "unknown", "time": None, "target_date": posted_at_jst[:10], "timing": None, "confidence": 0.0}
    if not text or not text.strip():
        return merge_target_dates(empty, text, posted_at_jst)
    try:
        model = os.environ.get("TYPESAFE_MODEL", "jev-latest")
        state = {
            "channel_role": channel_role,
            "sender": sender_name,
            "posted_at_jst": posted_at_jst,
            "message": text,
        }
        resp = _post(state, questions_for(channel_role), _api_key(), model)
        parsed = assemble(resp.get("answers") or {}, posted_at_jst)
        return merge_target_dates(parsed, text, posted_at_jst)
    except Exception as exc:  # noqa: BLE001 - 1件の失敗で日次全体を止めない
        logger.warning("Jev 構造化に失敗（type=unknown で退避）: %s", exc)
        return merge_target_dates(empty, text, posted_at_jst)
