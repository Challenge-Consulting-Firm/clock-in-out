"""本文から勤怠対象日を抽出する（複数日・月日混在に対応）。

モデルは種別判定に使い、カレンダー計算はコード側で行う。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

_WEEKDAY = {"月": 0, "火": 1, "水": 2, "木": 3, "金": 4, "土": 5, "日": 6}

_DATE_LIST_HINT = re.compile(
    r"有給|有休|休暇|午前休|午後休|半休|休みを|お休み|在宅|リモート|テレワーク|早退|遅刻"
)

_TOKEN_RE = re.compile(
    r"(?P<ymd>20\d{2}[-/.]\d{1,2}[-/.]\d{1,2})"
    r"|(?:(?P<year>20\d{2})\s*年\s*)?(?P<month>\d{1,2})\s*[月/．.]\s*(?P<mday>\d{1,2})\s*日?"
    r"|(?<![月/．.\d])(?P<dday>\d{1,2})\s*日"
    r"|(?P<rel>明後日|明日|本日|今日)"
    r"|来週(?P<next_w>[月火水木金土日])"
    r"|今週(?P<this_w>[月火水木金土日])"
)

_RANGE_PREFIX = re.compile(r"^[〜～\-–—から至]+")
_BARE_DAY = re.compile(r"^(?P<dday>\d{1,2})(?!\d)")


def _safe(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _add(out: list[date], d: date | None) -> None:
    if d and d not in out:
        out.append(d)


def _infer_md(posted: date, month: int, day: int, year: int | None) -> date | None:
    y = year or posted.year
    d = _safe(y, month, day)
    if not d:
        return None
    if year is None and d < posted - timedelta(days=31):
        d = _safe(y + 1, month, day)
    return d


def _infer_day(posted: date, day: int, last: date) -> date | None:
    d = _safe(last.year, last.month, day)
    if not d:
        month = last.month + 1
        year = last.year
        if month == 13:
            month, year = 1, year + 1
        d = _safe(year, month, day)
        return d
    if d < posted - timedelta(days=2):
        month = d.month + 1
        year = d.year
        if month == 13:
            month, year = 1, year + 1
        return _safe(year, month, d.day) or d
    return d


def _parse_match(m: re.Match, posted: date, last: date, text: str = "") -> date | None:
    if m.group("ymd"):
        y, mo, da = (int(x) for x in re.split(r"[-/.]", m.group("ymd")))
        return _safe(y, mo, da)
    if m.group("month") and m.group("mday"):
        year = int(m.group("year")) if m.group("year") else None
        return _infer_md(posted, int(m.group("month")), int(m.group("mday")), year)
    if m.group("dday"):
        return _infer_day(posted, int(m.group("dday")), last)
    if m.group("rel"):
        rel = m.group("rel")
        if rel in ("今日", "本日"):
            # 案件説明の「本日レビュー」等を対象日にしない。休暇・在宅の直後だけ採用。
            window = text[max(0, m.start() - 8) : m.end() + 24]
            if not _DATE_LIST_HINT.search(window):
                return None
            return posted
        return {
            "明日": posted + timedelta(days=1),
            "明後日": posted + timedelta(days=2),
        }[rel]
    if m.group("next_w"):
        w = _WEEKDAY[m.group("next_w")]
        monday = posted - timedelta(days=posted.weekday())
        return monday + timedelta(days=7 + w)
    if m.group("this_w"):
        w = _WEEKDAY[m.group("this_w")]
        monday = posted - timedelta(days=posted.weekday())
        return monday + timedelta(days=w)
    return None


def _expand_range(start: date, end: date, max_days: int = 14) -> list[date]:
    if end < start:
        start, end = end, start
    if (end - start).days > max_days:
        return [start, end]
    out = []
    cur = start
    while cur <= end:
        out.append(cur)
        cur += timedelta(days=1)
    return out


def extract_target_dates(text: str, posted_at_jst: str) -> list[str]:
    """本文から対象日 YYYY-MM-DD のリストを返す（出現順・重複なし）。見つからなければ空。"""
    if not text or not posted_at_jst:
        return []
    posted = datetime.strptime(posted_at_jst[:19], "%Y-%m-%d %H:%M:%S").date()
    found: list[date] = []
    last = posted
    idx = 0
    while idx < len(text):
        m = _TOKEN_RE.search(text, idx)
        if not m:
            break
        start_d = _parse_match(m, posted, last, text)
        # 「次回の出勤日は16日」など、復職日は休暇対象にしない
        prefix = text[max(0, m.start() - 8) : m.start()]
        if re.search(r"出勤日|出社日", prefix):
            idx = m.end()
            continue
        idx = m.end()
        if start_d:
            last = start_d
        rest = text[idx:]
        sep = _RANGE_PREFIX.match(rest.lstrip())
        skipped = len(rest) - len(rest.lstrip())
        if start_d and sep:
            after = rest[skipped + sep.end() :]
            m2 = _TOKEN_RE.match(after)
            end_d = _parse_match(m2, posted, start_d, text) if m2 else None
            consumed = m2.end() if m2 and end_d else 0
            if end_d is None:
                m3 = _BARE_DAY.match(after)
                if m3:
                    end_d = _infer_day(posted, int(m3.group("dday")), start_d)
                    consumed = m3.end()
            if end_d:
                for x in _expand_range(start_d, end_d):
                    _add(found, x)
                last = end_d
                idx += skipped + sep.end() + consumed
                continue
        _add(found, start_d)
    return [x.isoformat() for x in found]


def merge_target_dates(parsed: dict, text: str, posted_at_jst: str) -> dict:
    """モデル結果に target_dates を足す。複数日ヒントがあれば抽出日を優先。"""
    extracted = extract_target_dates(text, posted_at_jst)
    ptype = parsed.get("type") or "unknown"
    model_date = parsed.get("target_date") or posted_at_jst[:10]
    punch = {"clock_in", "clock_out", "overtime"}
    hinted = bool(_DATE_LIST_HINT.search(text or ""))
    use_extracted = bool(extracted) and (
        ptype in {"paid_leave", "absence", "early_leave", "remote", "late"}
        or (ptype not in punch and hinted)
        or (ptype in punch and hinted and any(d != posted_at_jst[:10] for d in extracted))
    )
    if use_extracted and ptype in punch:
        # 打刻＋休暇混在: 打刻の主日付を残しつつ休暇日も列挙する
        dates = []
        for d in [model_date, *extracted]:
            if d and d not in dates:
                dates.append(d)
    elif use_extracted:
        dates = extracted
    else:
        dates = [model_date]
    out = dict(parsed)
    out["target_dates"] = dates
    out["target_date"] = dates[0]
    return out
