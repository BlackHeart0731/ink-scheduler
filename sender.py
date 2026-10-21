"""
Ink Scheduler - sender.py
送信担当。一定間隔で DB を見て、時刻になった予約と誕生日を Discord の Webhook に送る。

使い方:
    python3 sender.py          # 常駐（30秒ごとに確認）
    python3 sender.py --once   # 1回だけ確認して終了（動作確認用）

取りこぼし（NUC停止などで予定時刻を過ぎていた場合）の動き:
    - 予定から5分以内                    → 普通に送る
    - 予約の「遅れて送る」               → 設定の許容時間（既定6時間）以内なら送る。過ぎたらスキップ
    - 誕生日の「遅れて送る」             → 当日中（24時まで）なら送る。過ぎたらスキップ
    - 「確認通知」                       → 確認用Webhookに1通にまとめて知らせる（送るかは人が判断）
    - 「スキップ」                       → 送らず履歴に記録する
    どの場合も、結果は履歴(logs)に残る。

Webhook URL は秘密情報。ログ・履歴・確認通知のどこにも出さない。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable, Optional
from zoneinfo import ZoneInfo

import db

log = logging.getLogger("ink-scheduler")

USER_AGENT = "InkScheduler/0.1"          # Discord は既定の Python UA を弾くことがある
ON_TIME = timedelta(minutes=5)           # これ以内の遅れは「定刻」とみなす
BIRTHDAY_LOOKBACK_DAYS = 3               # 起動前の何日前までの誕生日を拾うか
REVIEW_MAX_CHARS = 1800                  # 確認通知1通の上限（Discord は2000文字）
PREVIEW_CHARS = 80                       # 確認通知で見せる本文の長さ
SEND_GAP_SECONDS = 1.0                   # 連続送信の間隔（Webhook は2秒に5回まで）
BACKOFF_SECONDS = (60, 120, 300, 600, 900)  # 一時的な失敗の再試行間隔


# ----------------------------------------------------------------------
# Webhook 送信
# ----------------------------------------------------------------------

@dataclass
class SendResult:
    ok: bool
    permanent: bool = False   # True: 再試行しても無駄（URL無効・削除済みなど）
    detail: str = ""


Sender = Callable[[str, dict], SendResult]


def _retry_after(err: urllib.error.HTTPError) -> float:
    try:
        body = json.loads(err.read().decode("utf-8", "replace"))
        return float(body.get("retry_after", 2.0))
    except Exception:
        pass
    try:
        return float(err.headers.get("Retry-After", 2.0))
    except Exception:
        return 2.0


def post_webhook(url: str, payload: dict, *, timeout: float = 15.0) -> SendResult:
    """Webhook に JSON を POST する。429（レート制限）は待って最大3回やり直す。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    for attempt in range(4):
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return SendResult(True, detail=f"HTTP {resp.status}")
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                time.sleep(min(_retry_after(e), 10.0))
                continue
            permanent = 400 <= e.code < 500 and e.code not in (408, 429)
            hint = "（Webhook が削除された可能性があります）" if e.code == 404 else ""
            return SendResult(False, permanent, f"HTTP {e.code}{hint}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return SendResult(False, False, f"通信エラー: {type(e).__name__}")
    return SendResult(False, False, "HTTP 429（レート制限）")


class RetryState:
    """一時的な失敗の再試行を遅らせる（メモリ上のみ。再起動でリセット）。"""

    def __init__(self) -> None:
        self._state: dict[tuple, tuple[int, datetime]] = {}

    def blocked(self, key: tuple, now: datetime) -> bool:
        st = self._state.get(key)
        return bool(st and now < st[1])

    def fail(self, key: tuple, now: datetime) -> int:
        n = self._state.get(key, (0, now))[0] + 1
        wait = BACKOFF_SECONDS[min(n - 1, len(BACKOFF_SECONDS) - 1)]
        self._state[key] = (n, now + timedelta(seconds=wait))
        return n

    def clear(self, key: tuple) -> None:
        self._state.pop(key, None)


# ----------------------------------------------------------------------
# 送信内容
# ----------------------------------------------------------------------

def schedule_payload(row: sqlite3.Row) -> dict:
    # 告知で @everyone やロールを使うことがあるので、メンションは制限しない
    return {"content": row["content"]}


def birthday_payload(row: sqlite3.Row) -> dict:
    # 名前に @everyone などが入っていても飛ばないよう、ユーザーメンションだけ許可する
    return {
        "content": db.render_birthday_message(row)[:2000],
        "allowed_mentions": {"parse": ["users"]},
    }


def review_payload(text: str) -> dict:
    # 予約本文を引用するので、メンションは一切飛ばさない
    return {"content": text, "allowed_mentions": {"parse": []}}


def _one_line(text: str, limit: int) -> str:
    s = " ".join(text.split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _fmt_local(dt: datetime, tz: ZoneInfo) -> str:
    return dt.astimezone(tz).strftime("%m/%d %H:%M")


def _minutes(td: timedelta) -> int:
    return int(td.total_seconds() // 60)


# ----------------------------------------------------------------------
# 処理対象の洗い出し
# ----------------------------------------------------------------------

@dataclass
class Item:
    source: str                 # 'schedule' | 'birthday'
    row: sqlite3.Row
    when: datetime              # 本来の送信予定（aware）
    window: timedelta           # 「遅れて送る」を許す長さ
    key: tuple
    day: Optional[date] = None  # 誕生日のみ: 祝う日

    @property
    def name(self) -> str:
        return self.row["name"]

    @property
    def policy(self) -> str:
        return self.row["missed_policy"]

    def payload(self) -> dict:
        return schedule_payload(self.row) if self.source == "schedule" else birthday_payload(self.row)

    def preview(self) -> str:
        if self.source == "schedule":
            return self.row["content"]
        return db.render_birthday_message(self.row)


def collect_items(conn: sqlite3.Connection, now: datetime, tz: ZoneInfo, catchup: timedelta) -> list[Item]:
    items: list[Item] = []

    for row in db.due_schedules(conn, now):
        items.append(Item("schedule", row, db.from_utc_str(row["next_run"]), catchup, ("schedule", row["id"])))

    hh, mm = (int(x) for x in db.get_setting(conn, "birthday_time").split(":"))
    today = now.astimezone(tz).date()
    for back in range(BIRTHDAY_LOOKBACK_DAYS, -1, -1):
        d = today - timedelta(days=back)
        due = datetime(d.year, d.month, d.day, hh, mm, tzinfo=tz)
        if now < due:
            continue
        window = datetime(d.year, d.month, d.day, tzinfo=tz) + timedelta(days=1) - due  # 当日の24時まで
        for row in db.birthdays_on(conn, d):
            if row["last_handled_on"] == d.isoformat():
                continue
            # 登録前に過ぎた送信時刻は対象外（後から登録した人に過去分を送らない）
            if due < db.from_utc_str(row["created_at"]):
                continue
            items.append(Item("birthday", row, due, window, ("birthday", row["id"], d.isoformat()), d))

    items.sort(key=lambda it: it.when)
    return items


def decide(lateness: timedelta, policy: str, window: timedelta) -> str:
    """'send' | 'notify' | 'skip'"""
    if lateness <= ON_TIME:
        return "send"
    if policy == "late_send":
        return "send" if lateness <= window else "skip"
    if policy == "notify":
        return "notify"
    return "skip"


def finish(conn: sqlite3.Connection, item: Item, status: str, detail: str, now: datetime) -> None:
    """履歴に残し、次回へ進める（予約）／処理済みにする（誕生日）。毎回 commit して二重送信を防ぐ。"""
    db.add_log(
        conn, source=item.source, ref_id=item.row["id"], name=item.name,
        status=status, scheduled_for=item.when, detail=detail, now=now,
    )
    if item.source == "schedule":
        db.advance_schedule(conn, item.row["id"], now=now)
    else:
        db.mark_birthday_handled(conn, item.row["id"], item.day)
    conn.commit()
    log.info("%s「%s」→ %s %s", "予約" if item.source == "schedule" else "誕生日", item.name, status, detail)


# ----------------------------------------------------------------------
# 確認通知（取りこぼしを1通にまとめる）
# ----------------------------------------------------------------------

def build_review_text(items: list[Item], tz: ZoneInfo) -> str:
    head = (
        f"【取りこぼしの確認】{len(items)}件\n"
        "停止中などで、予定時刻を過ぎた送信があります。"
        "送る場合は管理画面の「今すぐ送信」を使ってください。送らない場合は何もしなくて大丈夫です。\n"
    )
    lines: list[str] = []
    used = len(head)
    for i, it in enumerate(items):
        label = "予約" if it.source == "schedule" else "誕生日"
        line = (
            f"・[{label}] {_one_line(it.name, 40)} — 予定 {_fmt_local(it.when, tz)}"
            f" — {_one_line(it.preview(), PREVIEW_CHARS)}"
        )
        if used + len(line) + 1 > REVIEW_MAX_CHARS:
            lines.append(f"ほか{len(items) - i}件は管理画面の履歴で確認してください")
            break
        lines.append(line)
        used += len(line) + 1
    return head + "\n".join(lines)


def _flush_notices(
    conn: sqlite3.Connection, notices: list[Item], now: datetime, tz: ZoneInfo,
    review_url: str, send: Sender, retry: RetryState, stats: dict[str, int],
) -> None:
    if not notices:
        return
    if not review_url:
        for it in notices:
            finish(conn, it, "skipped", "確認用Webhookが未設定のため通知できず、スキップ", now)
            stats["skipped"] += 1
        return
    key = ("review",)
    if retry.blocked(key, now):
        return
    res = send(review_url, review_payload(build_review_text(notices, tz)))
    if res.ok:
        retry.clear(key)
        for it in notices:
            finish(conn, it, "notified", "確認用Webhookへ通知（送るかは手動で判断）", now)
            stats["notified"] += 1
    elif res.permanent:
        for it in notices:
            finish(conn, it, "skipped", f"確認用Webhookが無効のため通知できず、スキップ（{res.detail}）", now)
            stats["skipped"] += 1
    else:
        retry.fail(key, now)
        log.warning("確認通知の送信に失敗しました。あとで再試行します: %s", res.detail)


# ----------------------------------------------------------------------
# メイン処理
# ----------------------------------------------------------------------

def process_due(
    conn: sqlite3.Connection,
    now: datetime,
    *,
    send: Sender = post_webhook,
    retry: Optional[RetryState] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, int]:
    """時刻になった予約・誕生日を1回分処理する。結果の件数を返す。"""
    retry = retry or RetryState()
    tz = db.get_tz(conn)
    catchup = timedelta(hours=int(db.get_setting(conn, "catchup_hours")))
    review_url = db.get_setting(conn, "review_webhook_url")
    stats = {"sent": 0, "skipped": 0, "notified": 0, "failed": 0}
    notices: list[Item] = []

    for item in collect_items(conn, now, tz, catchup):
        if retry.blocked(item.key, now):
            continue
        lateness = now - item.when
        action = decide(lateness, item.policy, item.window)

        if action == "send":
            res = send(item.row["webhook_url"], item.payload())
            sleep(SEND_GAP_SECONDS)
            if res.ok:
                retry.clear(item.key)
                detail = "" if lateness <= ON_TIME else f"{_minutes(lateness)}分遅れで送信"
                finish(conn, item, "sent", detail, now)
                stats["sent"] += 1
            elif res.permanent:
                retry.clear(item.key)
                finish(conn, item, "failed", f"送信失敗: {res.detail}", now)
                stats["failed"] += 1
            else:
                if retry.fail(item.key, now) == 1:  # 履歴は最初の1回だけ（連続で埋めない）
                    db.add_log(
                        conn, source=item.source, ref_id=item.row["id"], name=item.name,
                        status="failed", scheduled_for=item.when,
                        detail=f"一時的な失敗。再試行します: {res.detail}", now=now,
                    )
                    conn.commit()
                    stats["failed"] += 1
        elif action == "skip":
            if item.policy == "skip":
                reason = "取りこぼし時の動作が「スキップ」のため送らず"
            else:
                reason = f"許容時間を過ぎたため送らず（{_minutes(lateness)}分遅れ）"
            finish(conn, item, "skipped", reason, now)
            stats["skipped"] += 1
        else:
            notices.append(item)

    _flush_notices(conn, notices, now, tz, review_url, send, retry, stats)
    return stats


def send_now(
    conn: sqlite3.Connection,
    source: str,
    ref_id: int,
    *,
    send: Sender = post_webhook,
    now: Optional[datetime] = None,
) -> SendResult:
    """管理画面の「今すぐ送信」用。予定は変えず、履歴に「手動送信」と残す。"""
    now = now or db.utc_now()
    if source == "schedule":
        row = db.get_schedule(conn, ref_id)
        payload = schedule_payload(row) if row else {}
    elif source == "birthday":
        row = db.get_birthday(conn, ref_id)
        payload = birthday_payload(row) if row else {}
    else:
        raise ValueError(f"未知の種類です: {source}")
    if row is None:
        raise ValueError("対象が存在しません")

    res = send(row["webhook_url"], payload)
    db.add_log(
        conn, source=source, ref_id=ref_id, name=row["name"],
        status="sent" if res.ok else "failed",
        detail="手動送信" if res.ok else f"手動送信に失敗: {res.detail}", now=now,
    )
    if res.ok and source == "birthday":
        # 当日の誕生日なら、あとで自動送信が重ならないよう処理済みにする
        today = now.astimezone(db.get_tz(conn)).date()
        if any(r["id"] == ref_id for r in db.birthdays_on(conn, today)):
            db.mark_birthday_handled(conn, ref_id, today)
    conn.commit()
    return res


# ----------------------------------------------------------------------
# 常駐
# ----------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Ink Scheduler 送信担当")
    ap.add_argument("--once", action="store_true", help="1回だけ確認して終了する")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db.init_db()
    with db.connect() as conn:
        removed = db.prune_logs(conn)
        if removed:
            log.info("古い履歴を %d 件削除しました", removed)

    retry = RetryState()

    if args.once:
        with db.connect() as conn:
            stats = process_due(conn, db.utc_now(), retry=retry)
        log.info("結果: %s", stats)
        return 0

    interval = float(os.environ.get("INK_SCHEDULER_INTERVAL", "30"))
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    log.info("起動しました（%s 秒ごとに確認）DB: %s", interval, db.get_db_path())
    backed_up_on: Optional[date] = None
    while not stop.is_set():
        today = db.utc_now().date()
        if backed_up_on != today:  # 1日1回、自動バックアップ（失敗しても送信は止めない）
            try:
                made = db.ensure_daily_backup()
                if made:
                    log.info("自動バックアップを作りました: %s", made.name)
                backed_up_on = today
            except Exception:
                log.exception("自動バックアップに失敗しました。次回やり直します")
        try:
            with db.connect() as conn:
                stats = process_due(conn, db.utc_now(), retry=retry)
            if any(stats.values()):
                log.info("結果: %s", stats)
        except Exception:
            log.exception("処理中にエラーが起きました。次回やり直します")
        stop.wait(interval)
    log.info("停止しました")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
