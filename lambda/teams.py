"""Teams Workflows webhook 投稿の共通ユーティリティ。

Power Automate「Webhook 要求を受信したとき」の JSON 形式 {"text": ...} で投稿する。
投稿失敗はリトライし、最終的に失敗したら TeamsNotificationError を送出する
（呼び出し側で関数を失敗させ、EventBridge Scheduler のリトライ/アラートに乗せるため）。

出典: Challenge-Consulting-Firm/editor-claude-bedrock lambda/teams.py（実測済みの投稿型）。
"""

import json
import logging
import time
import urllib.request

logger = logging.getLogger()
logger.setLevel(logging.INFO)


class TeamsNotificationError(Exception):
    """リトライしても Teams への投稿に失敗した。"""


def post_teams(webhook_url: str, message: str, *, max_attempts: int = 3, backoff_seconds: float = 5.0) -> None:
    payload = json.dumps({"text": message}).encode("utf-8")
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            req = urllib.request.Request(
                webhook_url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
            )
            with urllib.request.urlopen(req, timeout=15) as res:
                if res.status >= 300:
                    raise RuntimeError(f"HTTP {res.status}")
            return
        except Exception as exc:  # noqa: BLE001 - リトライ対象を広く取る
            last_error = exc
            logger.warning("Teams 投稿失敗 (%s/%s): %s", attempt, max_attempts, exc)
            if attempt < max_attempts:
                time.sleep(backoff_seconds * attempt)
    raise TeamsNotificationError(f"Teams への通知に {max_attempts} 回失敗しました") from last_error
