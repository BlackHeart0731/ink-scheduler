"""
Ink Scheduler - db.py
SQLite層（スキーマ・CRUD・次回実行日時の計算）

- 送信処理（sender.py）と管理画面（Streamlit）の2プロセスから同時に使う前提。
  そのため WAL モードと busy_timeout を有効にしている。
- 日時は DB には UTC の ISO 文字列で保存する（文字列比較で大小が正しく判定できる）。
  ユーザー向けの時刻（実行時刻・once_at）は「設定のタイムゾーン」(既定 Asia/Tokyo) で扱う。
- Webhook URL は秘密情報。画面表示には必ず mask_webhook() を通すこと。
"""

from __future__ import annotations

import calendar
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional
from zoneinfo import ZoneInfo

# ----------------------------------------------------------------------
# 定数
# ----------------------------------------------------------------------

DEFAULT_DB_PATH = Path.home() / "ink-scheduler" / "data" / "scheduler.db"

KINDS = ("once", "daily", "weekly", "monthly", "yearly")
MISSED_POLICIES = ("notify", "late_send", "skip")
LOG_STATUSES = ("sent", "failed", "notified", "skipped")

DEFAULT_SETTINGS = {
    "timezone": "Asia/Tokyo",
    "birthday_time": "09:00",
    "catchup_hours": "6",       # 取りこぼし時、この時間以内なら late_send で送る
    "review_webhook_url": "",    # 取りこぼし確認用（所長専用の非公開チャンネル）
}

WEBHOOK_RE = re.compile(
    r"^https://(?:(?:canary|ptb)\.)?discord(?:app)?\.com/api(?:/v\d+)?/webhooks/\d+/[\w-]+/?$"
)
TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


# ----------------------------------------------------------------------
# 接続・初期化
# ----------------------------------------------------------------------

def get_db_path() -> Path:
    """環境変数 INK_SCHEDULER_DB があればそれを優先する。"""
    env = os.environ.get("INK_SCHEDULER_DB")
    return Path(env).expanduser() if env else DEFAULT_DB_PATH


@contextmanager
def connect(path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    """接続を開き、正常終了で commit、例外で rollback して閉じる。"""
    db_path = Path(path) if path else get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schedules (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    webhook_url   TEXT NOT NULL,
    content       TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('once','daily','weekly','monthly','yearly')),
    run_time      TEXT NOT NULL DEFAULT '09:00',   -- HH:MM（once では未使用）
    once_at       TEXT,                            -- once 用: ローカル時刻 YYYY-MM-DDTHH:MM
    weekday       INTEGER,                         -- weekly 用: 0=月 ... 6=日
    month_day     INTEGER,                         -- monthly/yearly 用: 1-31、-1=月末
    month         INTEGER,                         -- yearly 用: 1-12
    missed_policy TEXT NOT NULL DEFAULT 'notify'
                  CHECK (missed_policy IN ('notify','late_send','skip')),
    enabled       INTEGER NOT NULL DEFAULT 1,
    next_run      TEXT,                            -- UTC ISO。NULL なら実行予定なし
    last_run      TEXT,                            -- UTC ISO
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_schedules_due ON schedules(enabled, next_run);

CREATE TABLE IF NOT EXISTS birthdays (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    month         INTEGER NOT NULL CHECK (month BETWEEN 1 AND 12),
    day           INTEGER NOT NULL CHECK (day BETWEEN 1 AND 31),
    webhook_url   TEXT NOT NULL,
    template      TEXT NOT NULL DEFAULT '{name}さん、お誕生日おめでとうございます！',
    discord_id    TEXT,                            -- 任意。入れると <@ID> でメンションできる
    missed_policy TEXT NOT NULL DEFAULT 'late_send'
                  CHECK (missed_policy IN ('notify','late_send','skip')),
    enabled       INTEGER NOT NULL DEFAULT 1,
    last_handled_on TEXT,                          -- 最後に処理した日 YYYY-MM-DD（送信・通知・スキップのどれでも。二重処理防止）
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS logs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL CHECK (source IN ('schedule','birthday','system')),
    ref_id        INTEGER,
    name          TEXT NOT NULL,
    scheduled_for TEXT,                            -- 本来の送信予定（UTC ISO）
    status        TEXT NOT NULL CHECK (status IN ('sent','failed','notified','skipped')),
    detail        TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_created ON logs(created_at DESC);
"""


def init_db(path: Optional[Path] = None) -> None:
    """テーブルを作成し、未設定の設定値に既定値を入れる（何度呼んでも安全）。"""
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value)
            )


# ----------------------------------------------------------------------
# ユーティリティ
# ----------------------------------------------------------------------

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_utc_str(dt: datetime) -> str:
    """aware な datetime を UTC ISO 文字列にする（保存形式を1つに固定）。"""
    if dt.tzinfo is None:
        raise ValueError("タイムゾーンなしの日時は保存できません")
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def from_utc_str(s: str) -> datetime:
    return datetime.fromisoformat(s)


def mask_webhook(url: str) -> str:
    """画面表示用。末尾4文字だけ見せる。"""
    if not url:
        return ""
    return f"…{url.rstrip('/')[-4:]}"


def validate_webhook(url: str, *, allow_empty: bool = False) -> str:
    url = (url or "").strip()
    if not url:
        if allow_empty:
            return ""
        raise ValueError("Webhook URL が空です")
    if not WEBHOOK_RE.match(url):
        raise ValueError(
            "Webhook URL の形式が正しくありません"
            "（https://discord.com/api/webhooks/… の形式のみ使えます）"
        )
    return url


def _parse_time(s: str) -> tuple[int, int]:
    m = TIME_RE.match((s or "").strip())
    if not m:
        raise ValueError("時刻は HH:MM 形式（例: 09:00）で入力してください")
    return int(m.group(1)), int(m.group(2))


def _last_day(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def _clamp_day(year: int, month: int, day: int) -> int:
    """day=-1 は月末。存在しない日（例: 2月31日）はその月の末日に丸める。"""
    last = _last_day(year, month)
    return last if day == -1 else min(day, last)


def get_tz(conn: sqlite3.Connection) -> ZoneInfo:
    return ZoneInfo(get_setting(conn, "timezone"))


# ----------------------------------------------------------------------
# 設定
# ----------------------------------------------------------------------

def get_setting(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is not None:
        return row["value"]
    return DEFAULT_SETTINGS[key]


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    if key not in DEFAULT_SETTINGS:
        raise ValueError(f"未知の設定項目です: {key}")
    value = (value or "").strip()
    if key == "timezone":
        try:
            ZoneInfo(value)
        except Exception as e:
            raise ValueError(f"タイムゾーンが不正です: {value}") from e
    elif key == "birthday_time":
        _parse_time(value)
    elif key == "catchup_hours":
        if not value.isdigit() or not (0 <= int(value) <= 168):
            raise ValueError("取りこぼしの許容時間は 0〜168 の整数で指定してください")
    elif key == "review_webhook_url":
        value = validate_webhook(value, allow_empty=True)
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


# ----------------------------------------------------------------------
# 次回実行日時の計算
# ----------------------------------------------------------------------

def next_occurrence(
    *,
    kind: str,
    after: datetime,
    tz: ZoneInfo,
    run_time: str = "09:00",
    once_at: Optional[str] = None,
    weekday: Optional[int] = None,
    month_day: Optional[int] = None,
    month: Optional[int] = None,
) -> Optional[datetime]:
    """after より後で最初の実行日時（aware）を返す。もう無ければ None。"""
    after = after.astimezone(tz)

    if kind == "once":
        if not once_at:
            return None
        naive = datetime.fromisoformat(once_at)
        dt = naive.replace(tzinfo=tz)
        return dt if dt > after else None

    hh, mm = _parse_time(run_time)

    def at(d: date) -> datetime:
        return datetime(d.year, d.month, d.day, hh, mm, tzinfo=tz)

    if kind == "daily":
        cand = at(after.date())
        return cand if cand > after else at(after.date() + timedelta(days=1))

    if kind == "weekly":
        if weekday is None:
            return None
        for add in range(8):
            d = after.date() + timedelta(days=add)
            if d.weekday() == weekday and at(d) > after:
                return at(d)
        return None

    if kind == "monthly":
        if month_day is None:
            return None
        y, m = after.year, after.month
        for _ in range(14):
            cand = at(date(y, m, _clamp_day(y, m, month_day)))
            if cand > after:
                return cand
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        return None

    if kind == "yearly":
        if month is None or month_day is None:
            return None
        for y in range(after.year, after.year + 9):  # うるう日の丸めも吸収できる幅
            cand = at(date(y, month, _clamp_day(y, month, month_day)))
            if cand > after:
                return cand
        return None

    raise ValueError(f"未知の種類です: {kind}")


# ----------------------------------------------------------------------
# 予約（schedules）
# ----------------------------------------------------------------------

def _validate_schedule(f: dict[str, Any], tz: ZoneInfo, *, now: datetime) -> dict[str, Any]:
    """入力を検証・整形して返す。不正なら ValueError（日本語メッセージ）。"""
    out = dict(f)

    out["name"] = (f.get("name") or "").strip()
    if not out["name"]:
        raise ValueError("予約の名前が空です")
    out["content"] = (f.get("content") or "").strip()
    if not out["content"]:
        raise ValueError("本文が空です")
    if len(out["content"]) > 2000:
        raise ValueError("本文が長すぎます（Discord の上限は 2000 文字です）")
    out["webhook_url"] = validate_webhook(f.get("webhook_url", ""))

    kind = f.get("kind")
    if kind not in KINDS:
        raise ValueError(f"種類は {', '.join(KINDS)} のどれかです")
    if f.get("missed_policy", "notify") not in MISSED_POLICIES:
        raise ValueError(f"取りこぼし時の動作は {', '.join(MISSED_POLICIES)} のどれかです")
    out["missed_policy"] = f.get("missed_policy", "notify")

    out["run_time"] = f.get("run_time") or "09:00"
    _parse_time(out["run_time"])

    # 種類に関係ない項目は None にそろえる
    out.setdefault("once_at", None)
    out.setdefault("weekday", None)
    out.setdefault("month_day", None)
    out.setdefault("month", None)

    if kind == "once":
        if not out["once_at"]:
            raise ValueError("1回だけの予約には日時が必要です")
        try:
            datetime.fromisoformat(out["once_at"])
        except ValueError as e:
            raise ValueError("日時は YYYY-MM-DDTHH:MM 形式で指定してください") from e
        out["weekday"] = out["month_day"] = out["month"] = None
    elif kind == "daily":
        out["once_at"] = out["weekday"] = out["month_day"] = out["month"] = None
    elif kind == "weekly":
        if out["weekday"] is None or not (0 <= int(out["weekday"]) <= 6):
            raise ValueError("曜日は 0（月）〜6（日）で指定してください")
        out["once_at"] = out["month_day"] = out["month"] = None
    elif kind == "monthly":
        md = out["month_day"]
        if md is None or not (md == -1 or 1 <= int(md) <= 31):
            raise ValueError("日付は 1〜31、または -1（月末）で指定してください")
        out["once_at"] = out["weekday"] = out["month"] = None
    elif kind == "yearly":
        md, mo = out["month_day"], out["month"]
        if mo is None or not (1 <= int(mo) <= 12):
            raise ValueError("月は 1〜12 で指定してください")
        if md is None or not (md == -1 or 1 <= int(md) <= 31):
            raise ValueError("日付は 1〜31、または -1（月末）で指定してください")
        out["once_at"] = out["weekday"] = None

    nxt = next_occurrence(
        kind=kind, after=now, tz=tz, run_time=out["run_time"], once_at=out["once_at"],
        weekday=out["weekday"], month_day=out["month_day"], month=out["month"],
    )
    if kind == "once" and nxt is None:
        raise ValueError("1回だけの予約の日時が過去になっています")
    out["_next"] = nxt
    return out


_SCHEDULE_COLS = (
    "name", "webhook_url", "content", "kind", "run_time", "once_at",
    "weekday", "month_day", "month", "missed_policy",
)


def add_schedule(conn: sqlite3.Connection, *, now: Optional[datetime] = None, **fields: Any) -> int:
    now = now or utc_now()
    v = _validate_schedule(fields, get_tz(conn), now=now)
    nxt = v["_next"]
    cur = conn.execute(
        f"INSERT INTO schedules ({', '.join(_SCHEDULE_COLS)}, enabled, next_run, created_at) "
        f"VALUES ({', '.join('?' for _ in _SCHEDULE_COLS)}, 1, ?, ?)",
        [v[c] for c in _SCHEDULE_COLS] + [to_utc_str(nxt) if nxt else None, to_utc_str(now)],
    )
    return int(cur.lastrowid)


def get_schedule(conn: sqlite3.Connection, schedule_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()


def list_schedules(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM schedules ORDER BY enabled DESC, next_run IS NULL, next_run, id"
    ).fetchall()


def update_schedule(
    conn: sqlite3.Connection, schedule_id: int, *, now: Optional[datetime] = None, **changes: Any
) -> None:
    """指定した項目だけ変更する。検証し直し、次回実行日時も再計算する。"""
    now = now or utc_now()
    row = get_schedule(conn, schedule_id)
    if row is None:
        raise ValueError(f"予約 #{schedule_id} は存在しません")
    merged = {c: row[c] for c in _SCHEDULE_COLS}
    merged.update({k: v for k, v in changes.items() if k in _SCHEDULE_COLS})
    v = _validate_schedule(merged, get_tz(conn), now=now)
    enabled = int(changes["enabled"]) if "enabled" in changes else row["enabled"]
    nxt = v["_next"]
    conn.execute(
        f"UPDATE schedules SET {', '.join(c + ' = ?' for c in _SCHEDULE_COLS)}, "
        "enabled = ?, next_run = ? WHERE id = ?",
        [v[c] for c in _SCHEDULE_COLS] + [enabled, to_utc_str(nxt) if nxt else None, schedule_id],
    )


def set_schedule_enabled(
    conn: sqlite3.Connection, schedule_id: int, enabled: bool, *, now: Optional[datetime] = None
) -> None:
    """有効/無効の切り替え。再開時は次回実行日時を今から計算し直す（溜まった分は送らない）。"""
    update_schedule(conn, schedule_id, now=now, enabled=enabled)


def delete_schedule(conn: sqlite3.Connection, schedule_id: int) -> None:
    conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))


def due_schedules(conn: sqlite3.Connection, now: Optional[datetime] = None) -> list[sqlite3.Row]:
    """実行時刻を過ぎている有効な予約（古い順）。"""
    now = now or utc_now()
    return conn.execute(
        "SELECT * FROM schedules WHERE enabled = 1 AND next_run IS NOT NULL AND next_run <= ? "
        "ORDER BY next_run, id",
        (to_utc_str(now),),
    ).fetchall()


def advance_schedule(
    conn: sqlite3.Connection, schedule_id: int, *, now: Optional[datetime] = None
) -> None:
    """処理済みにして次回実行日時を進める。

    次回は「今より後」で計算する（止まっていた間の分を連続で送り直さないため）。
    1回だけの予約は無効化して next_run を空にする。
    """
    now = now or utc_now()
    row = get_schedule(conn, schedule_id)
    if row is None:
        return
    nxt = next_occurrence(
        kind=row["kind"], after=now, tz=get_tz(conn), run_time=row["run_time"],
        once_at=row["once_at"], weekday=row["weekday"],
        month_day=row["month_day"], month=row["month"],
    )
    enabled = 0 if row["kind"] == "once" else row["enabled"]
    conn.execute(
        "UPDATE schedules SET last_run = ?, next_run = ?, enabled = ? WHERE id = ?",
        (to_utc_str(now), to_utc_str(nxt) if nxt else None, enabled, schedule_id),
    )


# ----------------------------------------------------------------------
# 誕生日（birthdays）  ※年は保存しない（月日のみ）
# ----------------------------------------------------------------------

def _validate_birthday(f: dict[str, Any]) -> dict[str, Any]:
    out = dict(f)
    out["name"] = (f.get("name") or "").strip()
    if not out["name"]:
        raise ValueError("名前が空です")
    month, day = f.get("month"), f.get("day")
    if month is None or not (1 <= int(month) <= 12):
        raise ValueError("月は 1〜12 で指定してください")
    if day is None or not (1 <= int(day) <= _last_day(2024, int(month))):  # 2024=うるう年
        raise ValueError("その月に存在しない日付です")
    out["month"], out["day"] = int(month), int(day)
    out["webhook_url"] = validate_webhook(f.get("webhook_url", ""))

    template = (f.get("template") or "").strip()
    out["template"] = template or "{name}さん、お誕生日おめでとうございます！"
    if len(out["template"]) > 1500:
        raise ValueError("テンプレートが長すぎます")

    did = (f.get("discord_id") or "").strip()
    if did and not did.isdigit():
        raise ValueError("Discord ID は数字のみです（開発者モードで「ユーザーIDをコピー」）")
    out["discord_id"] = did or None

    out["missed_policy"] = f.get("missed_policy", "late_send")
    if out["missed_policy"] not in MISSED_POLICIES:
        raise ValueError(f"取りこぼし時の動作は {', '.join(MISSED_POLICIES)} のどれかです")
    return out


_BIRTHDAY_COLS = (
    "name", "month", "day", "webhook_url", "template", "discord_id", "missed_policy",
)


def add_birthday(conn: sqlite3.Connection, *, now: Optional[datetime] = None, **fields: Any) -> int:
    v = _validate_birthday(fields)
    cur = conn.execute(
        f"INSERT INTO birthdays ({', '.join(_BIRTHDAY_COLS)}, enabled, created_at) "
        f"VALUES ({', '.join('?' for _ in _BIRTHDAY_COLS)}, 1, ?)",
        [v[c] for c in _BIRTHDAY_COLS] + [to_utc_str(now or utc_now())],
    )
    return int(cur.lastrowid)


def get_birthday(conn: sqlite3.Connection, birthday_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM birthdays WHERE id = ?", (birthday_id,)).fetchone()


def list_birthdays(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM birthdays ORDER BY month, day, id").fetchall()


def update_birthday(conn: sqlite3.Connection, birthday_id: int, **changes: Any) -> None:
    row = get_birthday(conn, birthday_id)
    if row is None:
        raise ValueError(f"誕生日 #{birthday_id} は存在しません")
    merged = {c: row[c] for c in _BIRTHDAY_COLS}
    merged.update({k: v for k, v in changes.items() if k in _BIRTHDAY_COLS})
    v = _validate_birthday(merged)
    enabled = int(changes["enabled"]) if "enabled" in changes else row["enabled"]
    conn.execute(
        f"UPDATE birthdays SET {', '.join(c + ' = ?' for c in _BIRTHDAY_COLS)}, enabled = ? "
        "WHERE id = ?",
        [v[c] for c in _BIRTHDAY_COLS] + [enabled, birthday_id],
    )


def delete_birthday(conn: sqlite3.Connection, birthday_id: int) -> None:
    conn.execute("DELETE FROM birthdays WHERE id = ?", (birthday_id,))


def birthdays_on(conn: sqlite3.Connection, d: date) -> list[sqlite3.Row]:
    """その日が誕生日の人（有効なもの）。2/29 生まれは平年なら 2/28 に祝う。"""
    rows = conn.execute(
        "SELECT * FROM birthdays WHERE enabled = 1 AND month = ? AND day = ? ORDER BY id",
        (d.month, d.day),
    ).fetchall()
    if d.month == 2 and d.day == 28 and not calendar.isleap(d.year):
        rows += conn.execute(
            "SELECT * FROM birthdays WHERE enabled = 1 AND month = 2 AND day = 29 ORDER BY id"
        ).fetchall()
    return rows


def mark_birthday_handled(conn: sqlite3.Connection, birthday_id: int, d: date) -> None:
    """その日の誕生日を処理済みにする（送った・通知した・スキップした、のどれでも）。"""
    conn.execute("UPDATE birthdays SET last_handled_on = ? WHERE id = ?", (d.isoformat(), birthday_id))


def render_birthday_message(row: sqlite3.Row) -> str:
    """{name} を差し込む。discord_id があれば先頭にメンションを付ける。"""
    body = row["template"].replace("{name}", row["name"])
    if row["discord_id"]:
        body = f"<@{row['discord_id']}> {body}"
    return body


# ----------------------------------------------------------------------
# 履歴（logs）
# ----------------------------------------------------------------------

def add_log(
    conn: sqlite3.Connection,
    *,
    source: str,
    name: str,
    status: str,
    ref_id: Optional[int] = None,
    scheduled_for: Optional[datetime] = None,
    detail: str = "",
    now: Optional[datetime] = None,
) -> int:
    if status not in LOG_STATUSES:
        raise ValueError(f"未知のステータスです: {status}")
    # Webhook URL がうっかり履歴に残らないよう、detail 内の URL は伏せる
    safe_detail = re.sub(r"https://\S*discord(?:app)?\.com/api\S*", "[webhook]", detail or "")
    cur = conn.execute(
        "INSERT INTO logs (source, ref_id, name, scheduled_for, status, detail, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (source, ref_id, name, to_utc_str(scheduled_for) if scheduled_for else None,
         status, safe_detail, to_utc_str(now or utc_now())),
    )
    return int(cur.lastrowid)


def list_logs(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM logs ORDER BY id DESC LIMIT ?", (int(limit),)
    ).fetchall()


def prune_logs(conn: sqlite3.Connection, keep_days: int = 180, *, now: Optional[datetime] = None) -> int:
    """古い履歴を削除して件数を返す。"""
    cutoff = to_utc_str((now or utc_now()) - timedelta(days=keep_days))
    return conn.execute("DELETE FROM logs WHERE created_at < ?", (cutoff,)).rowcount


if __name__ == "__main__":
    init_db()
    print(f"DB を初期化しました: {get_db_path()}")
