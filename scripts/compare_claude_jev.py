#!/usr/bin/env python3
"""過去 N 日の Teams 勤怠メッセージを Claude（Bedrock）と Jev（TypeSafe）で構造化し比較する。

使い方（リポジトリルート）:
  python3 scripts/compare_claude_jev.py
  python3 scripts/compare_claude_jev.py --days 21 --limit 0

必要環境変数（.env 可）:
  GRAPH_TENANT_ID / GRAPH_CLIENT_ID / GRAPH_CLIENT_SECRET
  CHANNELS_JSON または GRAPH_TEAM_ID + CLOCK_CHANNEL_ID + REPORT_CHANNEL_ID
  AWS 認証（Bedrock。AWS_PROFILE / AWS_REGION / BEDROCK_MODEL_ID）
  TYPESAFE_API_KEY
  TYPESAFE_MODEL=jev-latest （任意）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAMBDA_DIR = ROOT / "lambda"
CACHE_DIR = Path(__file__).resolve().parent / ".cache"
sys.path.insert(0, str(LAMBDA_DIR))

JST = timezone(timedelta(hours=9))
UTC = timezone.utc

TYPE_LABELS = {
    "clock_in": "出勤",
    "clock_out": "退勤",
    "late": "遅刻",
    "early_leave": "早退",
    "paid_leave": "休暇",
    "absence": "欠勤",
    "remote": "在宅",
    "out_of_office": "終日外出",
    "overtime": "残業",
    "other": "報告",
    "unknown": "不明",
}

CLOCK_TYPES = ["clock_in", "clock_out", "unknown"]
REPORT_TYPES = ["late", "early_leave", "paid_leave", "remote", "overtime", "unknown"]
CACHE_VER = "v4"

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


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def _cache_path(name: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / name


def _load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def _save_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _channels() -> list[dict]:
    team = os.environ.get("GRAPH_TEAM_ID", "")
    clock_id = os.environ.get("CLOCK_CHANNEL_ID", "")
    report_id = os.environ.get("REPORT_CHANNEL_ID", "")
    raw = os.environ.get("CHANNELS_JSON")
    if raw and "<" not in raw:
        return json.loads(raw)
    if not (team and clock_id and report_id):
        raise SystemExit("CHANNELS_JSON がプレースホルダのため GRAPH_TEAM_ID / CLOCK_CHANNEL_ID / REPORT_CHANNEL_ID が必要です")
    return [
        {"role": "clock", "team_id": team, "channel_id": clock_id, "label": "打刻"},
        {"role": "report", "team_id": team, "channel_id": report_id, "label": "報告"},
    ]


def _since_iso_utc(days: int) -> str:
    now = datetime.now(JST)
    start = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _sender(message: dict) -> tuple[str, str]:
    user = ((message.get("from") or {}).get("user")) or {}
    return user.get("id") or "", user.get("displayName") or "(unknown)"


def _to_jst_str(iso_utc: str) -> str:
    s = re.sub(r"\.\d+", "", iso_utc.replace("Z", "+00:00"))
    dt = datetime.fromisoformat(s).astimezone(JST)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _msg_key(channel_id: str, message_id: str) -> str:
    return f"{channel_id}:{message_id}"


def fetch_items(days: int) -> list[dict]:
    from graph_client import extract_plaintext, fetch_messages_and_replies_since, get_token

    token = get_token(
        os.environ["GRAPH_TENANT_ID"],
        os.environ["GRAPH_CLIENT_ID"],
        os.environ["GRAPH_CLIENT_SECRET"],
    )
    since = _since_iso_utc(days)
    items = []
    for ch in _channels():
        # 3 週間分の親スレッドを落とさないよう grace / ページを広げる
        messages = fetch_messages_and_replies_since(
            token,
            ch["team_id"],
            ch["channel_id"],
            since,
            parent_grace_days=21,
            max_pages=80,
        )
        for msg in messages:
            if msg.get("deletedDateTime") or msg.get("messageType") not in (None, "message"):
                continue
            employee_id, employee_name = _sender(msg)
            if not employee_id:
                continue
            text = extract_plaintext(msg)
            if not text:
                continue
            posted_at = _to_jst_str(msg["createdDateTime"])
            items.append(
                {
                    "message_id": msg.get("id"),
                    "channel_label": ch.get("label", ch["channel_id"]),
                    "channel_role": ch["role"],
                    "channel_id": ch["channel_id"],
                    "employee_id": employee_id,
                    "employee_name": employee_name,
                    "posted_at_jst": posted_at,
                    "raw_text": text,
                }
            )
    items.sort(key=lambda x: x["posted_at_jst"])
    return items


def parse_claude(item: dict) -> dict:
    from bedrock_parser import parse_message
    from date_extract import merge_target_dates

    parsed = parse_message(item["raw_text"], item["posted_at_jst"], item["employee_name"], item["channel_role"])
    return merge_target_dates(parsed, item["raw_text"], item["posted_at_jst"])


def _jev_post(state, questions: dict, api_key: str, model: str, retries: int = 6) -> dict:
    body = json.dumps({"state": state, "model": model, "questions": questions}, ensure_ascii=False).encode("utf-8")
    base = os.environ.get("TYPESAFE_BASE_URL", "https://api.typesafe.ai").rstrip("/")
    url = f"{base}/v1/systemone"
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
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


def _choice(instructions, criteria: dict) -> dict:
    return {"type": "choice", "instructions": instructions, "criteria": criteria}


def _jev_questions(channel_role: str) -> dict:
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


def _choice_of(answers: dict, key: str) -> tuple[str | None, float | None]:
    ans = answers.get(key) or {}
    return ans.get("choice"), ans.get("confidence")


def _assemble_jev(answers: dict, posted_at_jst: str) -> dict:
    posted = datetime.strptime(posted_at_jst, "%Y-%m-%d %H:%M:%S")
    today = posted.date()
    ptype, type_conf = _choice_of(answers, "type")
    timing, timing_conf = _choice_of(answers, "timing")
    mode, mode_conf = _choice_of(answers, "date_mode")
    confs = [c for c in (type_conf, timing_conf, mode_conf) if c is not None]

    target = today
    note = ""
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
                    note = "invalid-absolute-date"
                    target = today
            else:
                note = "year-out-of-range"
        else:
            note = "incomplete-absolute-date"
    elif mode == "relative":
        anchor, anchor_conf = _choice_of(answers, "day_anchor")
        confs.append(anchor_conf) if anchor_conf is not None else None
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
            else:
                note = "weekday-missing"
        else:
            note = "relative-missing"

    hour_s, hour_conf = _choice_of(answers, "hour")
    minute_s, minute_conf = _choice_of(answers, "minute")
    time_str = None
    if hour_s not in (None, "none") and str(hour_s).isdigit():
        minute_i = int(minute_s) if minute_s not in (None, "none") and str(minute_s).isdigit() else 0
        time_str = f"{int(hour_s):02d}:{minute_i:02d}"
        confs += [c for c in (hour_conf, minute_conf) if c is not None]

    punch = {"clock_in", "clock_out", "late", "early_leave"}
    if not time_str and ptype in punch and timing != "advance":
        time_str = posted_at_jst[11:16]

    confidence = min(confs) if confs else 0.0
    return {
        "type": ptype or "unknown",
        "time": time_str,
        "target_date": target.isoformat(),
        "timing": None if timing in (None, "unknown") else timing,
        "confidence": float(confidence),
        "note": note,
    }


def parse_jev(item: dict, api_key: str, model: str) -> dict:
    os.environ["TYPESAFE_API_KEY"] = api_key
    os.environ["TYPESAFE_MODEL"] = model
    from jev_parser import parse_message

    return parse_message(item["raw_text"], item["posted_at_jst"], item["employee_name"], item["channel_role"])


def _short(text: str, n: int = 40) -> str:
    t = re.sub(r"\s+", " ", text).strip()
    return t if len(t) <= n else t[: n - 1] + "…"


def _fmt_parsed(p: dict | None, err: str | None) -> str:
    if err:
        return f"ERROR:{_short(err, 24)}"
    if not p:
        return "-"
    label = TYPE_LABELS.get(p.get("type"), p.get("type"))
    bits = [label]
    dates = p.get("target_dates") or ([p["target_date"]] if p.get("target_date") else [])
    if dates:
        bits.append(",".join(d[5:] for d in dates))
    if p.get("time"):
        bits.append(p["time"])
    if p.get("timing"):
        bits.append(p["timing"])
    if p.get("confidence") is not None:
        bits.append(f"{float(p['confidence']):.2f}")
    return " ".join(str(b) for b in bits)


def _agree(a: dict | None, b: dict | None, field: str) -> bool | None:
    if not a or not b:
        return None
    return a.get(field) == b.get(field)


def print_report(items: list[dict], claude: dict, jev: dict) -> str:
    lines = []
    total = len(items)
    type_ag = date_ag = time_ag = timing_ag = 0
    compared = 0
    claude_types = Counter()
    jev_types = Counter()
    mismatches = []

    header = f"{'#':>3} {'役割':<4} {'投稿者':<10} {'投稿':<16} {'本文':<28} {'Claude':<28} {'Jev':<28} {'差'}"
    lines.append(header)
    lines.append("-" * len(header))

    for i, item in enumerate(items, 1):
        key = _msg_key(item["channel_id"], item["message_id"])
        c_entry = claude.get(key) or {}
        j_entry = jev.get(key) or {}
        c = c_entry.get("parsed")
        j = j_entry.get("parsed")
        c_err = c_entry.get("error")
        j_err = j_entry.get("error")
        if c:
            claude_types[c.get("type") or "unknown"] += 1
        if j:
            jev_types[j.get("type") or "unknown"] += 1
        diff = []
        if c and j:
            compared += 1
            if _agree(c, j, "type"):
                type_ag += 1
            else:
                diff.append("type")
            c_dates = c.get("target_dates") or ([c.get("target_date")] if c.get("target_date") else [])
            j_dates = j.get("target_dates") or ([j.get("target_date")] if j.get("target_date") else [])
            if c_dates == j_dates:
                date_ag += 1
            else:
                diff.append("date")
            # 空同士は一致とみなす
            if (c.get("time") or None) == (j.get("time") or None):
                time_ag += 1
            else:
                diff.append("time")
            if (c.get("timing") or None) == (j.get("timing") or None):
                timing_ag += 1
            else:
                diff.append("timing")
            if diff:
                mismatches.append((item, c, j, diff))
        mark = ",".join(diff) if diff else ("ok" if c and j else "err")
        role = "打刻" if item["channel_role"] == "clock" else "報告"
        lines.append(
            f"{i:>3} {role:<4} {_short(item['employee_name'], 10):<10} "
            f"{item['posted_at_jst'][:16]:<16} {_short(item['raw_text'], 28):<28} "
            f"{_fmt_parsed(c, c_err):<28} {_fmt_parsed(j, j_err):<28} {mark}"
        )

    def pct(n, d):
        return f"{(100.0 * n / d):.1f}%" if d else "-"

    lines.append("")
    lines.append(f"件数: {total}  両方成功: {compared}")
    lines.append(
        f"一致率  type={type_ag}/{compared} ({pct(type_ag, compared)})  "
        f"target_date={date_ag}/{compared} ({pct(date_ag, compared)})  "
        f"time={time_ag}/{compared} ({pct(time_ag, compared)})  "
        f"timing={timing_ag}/{compared} ({pct(timing_ag, compared)})"
    )
    lines.append("")
    lines.append("type 分布")
    all_types = sorted(set(claude_types) | set(jev_types), key=lambda t: (-(claude_types[t] + jev_types[t]), t))
    lines.append(f"  {'type':<16} {'Claude':>7} {'Jev':>7}")
    for t in all_types:
        lines.append(f"  {TYPE_LABELS.get(t, t):<16} {claude_types[t]:>7} {jev_types[t]:>7}")

    if mismatches:
        lines.append("")
        lines.append(f"不一致 {len(mismatches)} 件（最大 30 件を再掲）")
        for item, c, j, diff in mismatches[:30]:
            lines.append(
                f"  [{item['channel_role']}] {item['posted_at_jst'][:16]} {item['employee_name']} | "
                f"{_short(item['raw_text'], 50)}"
            )
            lines.append(f"    Claude: {_fmt_parsed(c, None)}")
            lines.append(f"    Jev   : {_fmt_parsed(j, None)}  ({','.join(diff)})")

    leave_hits = [
        (item, (claude.get(_msg_key(item["channel_id"], item["message_id"])) or {}).get("parsed"),
         (jev.get(_msg_key(item["channel_id"], item["message_id"])) or {}).get("parsed"))
        for item in items
    ]
    leave_rows = [
        (item, c, j)
        for item, c, j in leave_hits
        if (c and c.get("type") in {"paid_leave", "absence"}) or (j and j.get("type") in {"paid_leave", "absence"})
    ]
    if leave_rows:
        lines.append("")
        lines.append(f"休暇候補 {len(leave_rows)} 件")
        for item, c, j in leave_rows:
            posted = item["posted_at_jst"][:10]
            ctd = ",".join((c or {}).get("target_dates") or [((c or {}).get("target_date") or "-")])
            jtd = ",".join((j or {}).get("target_dates") or [((j or {}).get("target_date") or "-")])
            lines.append(
                f"  投稿 {posted}  {item['employee_name']}  Claude:{TYPE_LABELS.get((c or {}).get('type'), '-')} {ctd}  "
                f"Jev:{TYPE_LABELS.get((j or {}).get('type'), '-')} {jtd}  「{_short(item['raw_text'], 40)}」"
            )

    text = "\n".join(lines)
    print(text)
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description="Claude vs Jev 勤怠構造化比較")
    parser.add_argument("--days", type=int, default=21, help="遡る日数（既定 21）")
    parser.add_argument("--limit", type=int, default=0, help="先頭 N 件だけ（0=全件）")
    parser.add_argument("--refresh-messages", action="store_true", help="Teams 取得キャッシュを無視")
    parser.add_argument("--refresh-models", action="store_true", help="モデル結果キャッシュを無視")
    args = parser.parse_args()

    _load_dotenv(ROOT / ".env")

    missing = [k for k in ("GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET", "TYPESAFE_API_KEY") if not os.environ.get(k)]
    if missing:
        print("不足している環境変数: " + ", ".join(missing), file=sys.stderr)
        print(".env に TYPESAFE_API_KEY と Graph 資格情報を設定してください", file=sys.stderr)
        return 1

    os.environ.setdefault("AWS_REGION", "ap-northeast-1")
    api_key = os.environ["TYPESAFE_API_KEY"]
    model = os.environ.get("TYPESAFE_MODEL", "jev-latest")

    msg_cache = _cache_path(f"messages_{args.days}d.json")
    if args.refresh_messages or not msg_cache.exists():
        print(f"Teams から直近 {args.days} 日を取得中…", flush=True)
        items = fetch_items(args.days)
        _save_json(msg_cache, items)
    else:
        items = _load_json(msg_cache, [])
        print(f"メッセージキャッシュを使用: {msg_cache.name} ({len(items)} 件)", flush=True)

    if args.limit:
        items = items[: args.limit]
    print(f"比較対象 {len(items)} 件", flush=True)

    claude_cache = _cache_path("claude.json")
    jev_cache = _cache_path("jev.json")
    from date_extract import merge_target_dates

    claude = {} if args.refresh_models else _load_json(claude_cache, {})
    jev = {} if args.refresh_models else _load_json(jev_cache, {})
    dates_dirty = False

    for i, item in enumerate(items, 1):
        key = _msg_key(item["channel_id"], item["message_id"])
        fingerprint = hashlib.sha256(item["raw_text"].encode("utf-8")).hexdigest()[:16]

        def _needs_model(store):
            entry = store.get(key) or {}
            return entry.get("fp") != fingerprint or "parsed" not in entry

        def _apply_dates(store):
            nonlocal dates_dirty
            entry = store.get(key)
            if not entry or "parsed" not in entry:
                return
            if entry.get("ver") == CACHE_VER:
                return
            entry["parsed"] = merge_target_dates(entry["parsed"], item["raw_text"], item["posted_at_jst"])
            entry["ver"] = CACHE_VER
            store[key] = entry
            dates_dirty = True

        need_c = _needs_model(claude)
        need_j = _needs_model(jev)
        if not need_c:
            _apply_dates(claude)
        if not need_j:
            _apply_dates(jev)
        if not need_c and not need_j:
            continue
        print(f"  [{i}/{len(items)}] {item['posted_at_jst'][:16]} {item['employee_name']} {_short(item['raw_text'], 30)}", flush=True)
        if need_c:
            try:
                claude[key] = {"fp": fingerprint, "ver": CACHE_VER, "parsed": parse_claude(item)}
            except Exception as exc:  # noqa: BLE001
                claude[key] = {"fp": fingerprint, "ver": CACHE_VER, "error": str(exc)}
            _save_json(claude_cache, claude)
        if need_j:
            try:
                jev[key] = {"fp": fingerprint, "ver": CACHE_VER, "parsed": parse_jev(item, api_key, model)}
            except Exception as exc:  # noqa: BLE001
                jev[key] = {"fp": fingerprint, "ver": CACHE_VER, "error": str(exc)}
            _save_json(jev_cache, jev)

    if dates_dirty:
        _save_json(claude_cache, claude)
        _save_json(jev_cache, jev)

    report = print_report(items, claude, jev)
    report_path = _cache_path("compare_report.txt")
    report_path.write_text(report + "\n", encoding="utf-8")
    print(f"\nレポート: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
