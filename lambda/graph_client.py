"""Microsoft Graph API クライアント（チャネルメッセージ取得）。

依存を Lambda 標準ランタイムだけに抑えるため urllib ベース（teams.py と同方針）。
認証は client credentials フロー（アプリケーション権限）。必要権限:
  - ChannelMessage.Read.All（チャネルメッセージ読み取り・アプリ権限）

delta query でチャネルメッセージの差分を取得する。deltaLink は呼び出し側
（ingest_attendance）が S3 / SSM に保管し、次回実行時に渡す。
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

logger = logging.getLogger()
logger.setLevel(logging.INFO)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_LOGIN_BASE = "https://login.microsoftonline.com"


class GraphError(Exception):
    """Graph API 呼び出しが失敗した。"""


def get_token(tenant_id: str, client_id: str, client_secret: str) -> str:
    """client credentials フローでアプリケーショントークンを取得する。"""
    url = f"{_LOGIN_BASE}/{tenant_id}/oauth2/v2.0/token"
    data = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            body = json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise GraphError(f"トークン取得失敗 HTTP {exc.code}: {detail}") from exc
    token = body.get("access_token")
    if not token:
        raise GraphError(f"access_token が応答に無い: {body}")
    return token


def _get(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise GraphError(f"Graph GET 失敗 HTTP {exc.code} ({url}): {detail}") from exc


def fetch_messages_since(token: str, team_id: str, channel_id: str, since_iso_utc: str, max_pages: int = 20) -> list[dict]:
    """指定時刻(UTC ISO8601)以降に投稿されたチャネルメッセージを取得する。

    Graph の channel messages は $filter/$orderby 非対応（実測 400）。ページ全体としては
    概ね createdDateTime 降順で返るが **ページ内の順序は厳密な降順ではない**（実測）。
    そのため「1件でも古いのを見たら即打ち切り」はページ内の新しいメッセージを取りこぼす。
    正しくは: ページ内は最後まで走査して since 以降を拾い、そのページに since 以降が
    1件も無くなったら（＝全ページが古い領域に入った）次ページへ進まない。

    delta は使わない（初回に全履歴を拾う問題を避けるため）。max_pages は安全弁。
    """
    url = f"{GRAPH_BASE}/teams/{team_id}/channels/{channel_id}/messages?$top=50"
    collected: list[dict] = []
    for _ in range(max_pages):
        if not url:
            break
        page = _get(url, token)
        page_msgs = page.get("value", [])
        fresh = [m for m in page_msgs if m.get("createdDateTime", "") >= since_iso_utc]
        collected.extend(fresh)
        # このページに since 以降が1件も無ければ、以降のページは全て古いので打ち切る
        if page_msgs and not fresh:
            break
        url = page.get("@odata.nextLink")
    return collected


def get_message_replies(token: str, team_id: str, channel_id: str, message_id: str) -> list[dict]:
    """スレッド返信を取得する。"""
    url = f"{GRAPH_BASE}/teams/{team_id}/channels/{channel_id}/messages/{message_id}/replies"
    replies: list[dict] = []
    while url:
        page = _get(url, token)
        replies.extend(page.get("value", []))
        url = page.get("@odata.nextLink")
    return replies


def fetch_messages_and_replies_since(
    token: str,
    team_id: str,
    channel_id: str,
    since_iso_utc: str,
    parent_grace_days: int = 7,
    max_pages: int = 40,
) -> list[dict]:
    """since 以降に投稿された「親メッセージ＋スレッド返信」をまとめて返す。

    打刻がスレッド返信で行われる運用（親「おはようございます」に出退勤がぶら下がる）に対応。
    ポイント: 親メッセージ自体は since より古いことがある（数日前に立ったスレッドに当日の
    返信が付く）。そこで親は since より parent_grace_days 日ぶん広く遡って取得し、
    最終的に「親＋返信」を実際の since で個別にフィルタする。

    戻り値の各要素は通常の message dict（返信も同じ形）。呼び出し側は from.user / body /
    createdDateTime を同じように扱える。
    """
    since_dt = datetime.fromisoformat(since_iso_utc.replace("Z", "+00:00"))
    parent_since_dt = since_dt - timedelta(days=parent_grace_days)
    parent_since_iso = parent_since_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    parents = fetch_messages_since(token, team_id, channel_id, parent_since_iso, max_pages=max_pages)

    out: list[dict] = []
    for parent in parents:
        # 親が since 以降なら親自身も対象
        if parent.get("createdDateTime", "") >= since_iso_utc:
            out.append(parent)
        # 返信は親が since より古くても取得し、since で絞る
        # （replyToId が入っている返信は親の一部として扱う）
        for reply in get_message_replies(token, team_id, channel_id, parent["id"]):
            if reply.get("createdDateTime", "") >= since_iso_utc:
                out.append(reply)
    return out


def extract_plaintext(message: dict) -> str:
    """message.body.content（HTML の場合が多い）から素朴にテキストを抽出する。"""
    body = message.get("body") or {}
    content = body.get("content", "") or ""
    if body.get("contentType") == "html":
        # 依存を増やさず素朴にタグ除去。厳密性より Bedrock へ渡す前処理として十分。
        import re

        content = re.sub(r"<[^>]+>", " ", content)
        content = re.sub(r"&nbsp;", " ", content)
        content = re.sub(r"\s+", " ", content).strip()
    return content
