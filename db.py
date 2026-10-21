"""
Ink Scheduler - db.py
SQLite層（スキーマ・CRUD・次回実行日時の計算）

- 送信処理（sender.py）と管理画面（Streamlit）の2プロセスから同時に使う前提。
  そのため WAL モードと busy_timeout を有効にしている。
- 日時は DB には UTC の ISO 文字列で保存する（文字列比較で大小が正しく判定できる）。
  ユーザー向けの時刻（実行時刻・once_at）は「設定のタイムゾーン」(既定 Asia/Tokyo) で扱う。
- Webhook URL は秘密情報。画面表示には必ず mask_webhook() を通すこと。
- 送信先（Webhook）は destinations テーブルに名前つきで登録し、予約・誕生日は destination_id で参照する。
  読み出し用のクエリが webhook_url（送信先の URL）と destination_name を付けて返すので、
  sender.py は row["webhook_url"] を使うだけでよい。
"""

from __future__ import annotations

import calendar
import hashlib
import hmac
import os
import re
import sqlite3
import shutil
import sys
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional
from zoneinfo import ZoneInfo

# ----------------------------------------------------------------------
# 定数
# ----------------------------------------------------------------------

# db.py と同じフォルダの data/ に置く（どこに clone しても、Docker の /app/data でも同じ動き）
DEFAULT_DB_PATH = Path(__file__).resolve().parent / "data" / "scheduler.db"

KINDS = ("once", "daily", "weekly", "monthly", "yearly")
MISSED_POLICIES = ("notify", "late_send", "skip")
LOG_STATUSES = ("sent", "failed", "notified", "skipped")

DEFAULT_SETTINGS = {
    "timezone": "Asia/Tokyo",
    "birthday_time": "09:00",
    "catchup_hours": "6",       # 取りこぼし時、この時間以内なら late_send で送る
    "review_webhook_url": "",    # 取りこぼし確認用（所長専用の非公開チャンネル）
    "admin_password": "",        # 管理画面のパスワード（ハッシュ。空なら環境変数の初期値を使う）
}
PASSWORD_MIN_LEN = 8
BACKUP_KEEP_DAYS = 14        # 自動バックアップを何日分残すか
BACKUP_TABLES = ("settings", "schedules", "birthdays", "logs")

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

CREATE TABLE IF NOT EXISTS destinations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    webhook_url TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schedules (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    webhook_url   TEXT NOT NULL DEFAULT '',        -- 旧形式。いまは使わない（移行後は空）
    destination_id INTEGER,                        -- destinations.id
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
    webhook_url   TEXT NOT NULL DEFAULT '',        -- 旧形式。いまは使わない（移行後は空）
    destination_id INTEGER,                        -- destinations.id
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


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn: sqlite3.Connection) -> None:
    """旧形式（予約・誕生日が Webhook URL を直接持つ）のDBを、送信先テーブル方式に移す。

    何度呼んでも安全。URL ごとに送信先を1つ作り、予約・誕生日をそれに結びつける。
    """
    for table in ("schedules", "birthdays"):
        if "destination_id" not in _column_names(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN destination_id INTEGER")

    urls = [
        r[0]
        for r in conn.execute(
            "SELECT webhook_url FROM schedules WHERE destination_id IS NULL AND webhook_url != '' "
            "UNION SELECT webhook_url FROM birthdays WHERE destination_id IS NULL AND webhook_url != '' "
            "ORDER BY 1"
        )
    ]
    for url in urls:
        row = conn.execute("SELECT id FROM destinations WHERE webhook_url = ? ORDER BY id", (url,)).fetchone()
        if row is not None:
            dest_id = row["id"]
        else:
            base = f"既存の送信先（{mask_webhook(url)}）"
            name, i = base, 2
            while conn.execute("SELECT 1 FROM destinations WHERE name = ?", (name,)).fetchone():
                name, i = f"{base}{i}", i + 1
            dest_id = conn.execute(
                "INSERT INTO destinations (name, webhook_url, created_at) VALUES (?, ?, ?)",
                (name, url, to_utc_str(utc_now())),
            ).lastrowid
        for table in ("schedules", "birthdays"):
            conn.execute(
                f"UPDATE {table} SET destination_id = ?, webhook_url = '' "
                "WHERE webhook_url = ? AND destination_id IS NULL",
                (dest_id, url),
            )


def init_db(path: Optional[Path] = None) -> None:
    """テーブルを作成し、旧形式のDBなら移行して、未設定の設定値に既定値を入れる（何度呼んでも安全）。"""
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        # sender と管理画面が同時に起動しても、移行が二重に走らないよう、ここで順番待ちにする
        conn.execute("BEGIN IMMEDIATE")
        _migrate(conn)
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
# 管理画面のパスワード  ※DBにはハッシュだけを保存する
# ----------------------------------------------------------------------

def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1, dklen=32)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex().encode(), digest_hex.encode())


def set_admin_password(conn: sqlite3.Connection, new_password: str) -> None:
    if len(new_password) < PASSWORD_MIN_LEN:
        raise ValueError(f"パスワードは {PASSWORD_MIN_LEN} 文字以上にしてください")
    set_setting(conn, "admin_password", hash_password(new_password))


def clear_admin_password(conn: sqlite3.Connection) -> None:
    """DBに保存したパスワードを消す（環境変数 INK_SCHEDULER_PASSWORD の初期値に戻る）。"""
    conn.execute("DELETE FROM settings WHERE key = 'admin_password'")


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
# 送信先（destinations）  ※名前つきで複数登録し、予約・誕生日から選ぶ
# ----------------------------------------------------------------------

def _clean_destination_id(value: Any) -> int:
    if value is None or value == "":
        raise ValueError("送信先を選んでください")
    try:
        return int(value)
    except (TypeError, ValueError) as e:
        raise ValueError("送信先が正しくありません") from e


def _check_destination_exists(conn: sqlite3.Connection, destination_id: int) -> None:
    if conn.execute("SELECT 1 FROM destinations WHERE id = ?", (destination_id,)).fetchone() is None:
        raise ValueError("選んだ送信先が見つかりません（削除された可能性があります）")


def _clean_destination_name(name: Any) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("送信先の名前が空です")
    if len(name) > 40:
        raise ValueError("送信先の名前は 40 文字までです")
    return name


def add_destination(
    conn: sqlite3.Connection, *, name: str, webhook_url: str, now: Optional[datetime] = None
) -> int:
    name = _clean_destination_name(name)
    url = validate_webhook(webhook_url)
    try:
        cur = conn.execute(
            "INSERT INTO destinations (name, webhook_url, created_at) VALUES (?, ?, ?)",
            (name, url, to_utc_str(now or utc_now())),
        )
    except sqlite3.IntegrityError as e:
        raise ValueError("同じ名前の送信先がすでにあります") from e
    return int(cur.lastrowid)


def get_destination(conn: sqlite3.Connection, destination_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM destinations WHERE id = ?", (destination_id,)).fetchone()


def list_destinations(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """送信先の一覧。schedule_count / birthday_count は、その送信先を使っている件数。"""
    return conn.execute(
        "SELECT d.*, "
        "(SELECT COUNT(*) FROM schedules s WHERE s.destination_id = d.id) AS schedule_count, "
        "(SELECT COUNT(*) FROM birthdays b WHERE b.destination_id = d.id) AS birthday_count "
        "FROM destinations d ORDER BY d.id"
    ).fetchall()


def update_destination(
    conn: sqlite3.Connection, destination_id: int, *,
    name: Optional[str] = None, webhook_url: Optional[str] = None,
) -> None:
    """名前・URL を変更する。webhook_url が None か空なら URL は今のまま。"""
    row = get_destination(conn, destination_id)
    if row is None:
        raise ValueError(f"送信先 #{destination_id} は存在しません")
    new_name = _clean_destination_name(row["name"] if name is None else name)
    new_url = validate_webhook(webhook_url) if (webhook_url or "").strip() else row["webhook_url"]
    try:
        conn.execute(
            "UPDATE destinations SET name = ?, webhook_url = ? WHERE id = ?",
            (new_name, new_url, destination_id),
        )
    except sqlite3.IntegrityError as e:
        raise ValueError("同じ名前の送信先がすでにあります") from e


def delete_destination(conn: sqlite3.Connection, destination_id: int) -> None:
    """使われている送信先は削除できない（予約・誕生日の送信先が消えてしまうため）。"""
    row = next((r for r in list_destinations(conn) if r["id"] == destination_id), None)
    if row is None:
        return
    if row["schedule_count"] or row["birthday_count"]:
        raise ValueError(
            f"この送信先は予約 {row['schedule_count']} 件・誕生日 {row['birthday_count']} 件で使われているため、"
            "削除できません。先に別の送信先へ変えてください"
        )
    conn.execute("DELETE FROM destinations WHERE id = ?", (destination_id,))


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
    out["destination_id"] = _clean_destination_id(f.get("destination_id"))

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
    "name", "destination_id", "content", "kind", "run_time", "once_at",
    "weekday", "month_day", "month", "missed_policy",
)

# webhook_url は「送信先テーブルのURL」を返す（sender.py はこれをそのまま使う）
_SCHEDULE_SELECT = (
    "SELECT s.id, s.name, s.destination_id, d.name AS destination_name, "
    "COALESCE(d.webhook_url, s.webhook_url) AS webhook_url, "
    "s.content, s.kind, s.run_time, s.once_at, s.weekday, s.month_day, s.month, "
    "s.missed_policy, s.enabled, s.next_run, s.last_run, s.created_at "
    "FROM schedules s LEFT JOIN destinations d ON d.id = s.destination_id"
)


def add_schedule(conn: sqlite3.Connection, *, now: Optional[datetime] = None, **fields: Any) -> int:
    now = now or utc_now()
    v = _validate_schedule(fields, get_tz(conn), now=now)
    _check_destination_exists(conn, v["destination_id"])
    nxt = v["_next"]
    cur = conn.execute(
        f"INSERT INTO schedules ({', '.join(_SCHEDULE_COLS)}, webhook_url, enabled, next_run, created_at) "
        f"VALUES ({', '.join('?' for _ in _SCHEDULE_COLS)}, '', 1, ?, ?)",
        [v[c] for c in _SCHEDULE_COLS] + [to_utc_str(nxt) if nxt else None, to_utc_str(now)],
    )
    return int(cur.lastrowid)


def get_schedule(conn: sqlite3.Connection, schedule_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(_SCHEDULE_SELECT + " WHERE s.id = ?", (schedule_id,)).fetchone()


def list_schedules(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        _SCHEDULE_SELECT + " ORDER BY s.enabled DESC, s.next_run IS NULL, s.next_run, s.id"
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
    _check_destination_exists(conn, v["destination_id"])
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
        _SCHEDULE_SELECT + " WHERE s.enabled = 1 AND s.next_run IS NOT NULL AND s.next_run <= ? "
        "ORDER BY s.next_run, s.id",
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
    out["destination_id"] = _clean_destination_id(f.get("destination_id"))

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
    "name", "month", "day", "destination_id", "template", "discord_id", "missed_policy",
)

_BIRTHDAY_SELECT = (
    "SELECT b.id, b.name, b.month, b.day, b.destination_id, d.name AS destination_name, "
    "COALESCE(d.webhook_url, b.webhook_url) AS webhook_url, "
    "b.template, b.discord_id, b.missed_policy, b.enabled, b.last_handled_on, b.created_at "
    "FROM birthdays b LEFT JOIN destinations d ON d.id = b.destination_id"
)


def add_birthday(conn: sqlite3.Connection, *, now: Optional[datetime] = None, **fields: Any) -> int:
    v = _validate_birthday(fields)
    _check_destination_exists(conn, v["destination_id"])
    cur = conn.execute(
        f"INSERT INTO birthdays ({', '.join(_BIRTHDAY_COLS)}, webhook_url, enabled, created_at) "
        f"VALUES ({', '.join('?' for _ in _BIRTHDAY_COLS)}, '', 1, ?)",
        [v[c] for c in _BIRTHDAY_COLS] + [to_utc_str(now or utc_now())],
    )
    return int(cur.lastrowid)


def get_birthday(conn: sqlite3.Connection, birthday_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(_BIRTHDAY_SELECT + " WHERE b.id = ?", (birthday_id,)).fetchone()


def list_birthdays(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(_BIRTHDAY_SELECT + " ORDER BY b.month, b.day, b.id").fetchall()


def update_birthday(conn: sqlite3.Connection, birthday_id: int, **changes: Any) -> None:
    row = get_birthday(conn, birthday_id)
    if row is None:
        raise ValueError(f"誕生日 #{birthday_id} は存在しません")
    merged = {c: row[c] for c in _BIRTHDAY_COLS}
    merged.update({k: v for k, v in changes.items() if k in _BIRTHDAY_COLS})
    v = _validate_birthday(merged)
    _check_destination_exists(conn, v["destination_id"])
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
        _BIRTHDAY_SELECT + " WHERE b.enabled = 1 AND b.month = ? AND b.day = ? ORDER BY b.id",
        (d.month, d.day),
    ).fetchall()
    if d.month == 2 and d.day == 28 and not calendar.isleap(d.year):
        rows += conn.execute(
            _BIRTHDAY_SELECT + " WHERE b.enabled = 1 AND b.month = 2 AND b.day = 29 ORDER BY b.id"
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
# バックアップ・復元
#   data/backups/ に保存する。SQLite の backup API を使うので、動いている最中でも壊れずにコピーできる。
#   自動:  scheduler-auto-YYYYMMDD.db （1日1回。古いものから消して BACKUP_KEEP_DAYS 件だけ残す）
#   手動:  scheduler-manual-YYYYMMDD-HHMMSS.db
#   復元前: scheduler-before-restore-YYYYMMDD-HHMMSS.db （復元する直前の状態。消さない）
# ----------------------------------------------------------------------

def backup_dir() -> Path:
    return get_db_path().parent / "backups"


def _copy_db(dest: Path) -> Path:
    """いまのDBを dest にコピーする（一時ファイルに書いてから置き換えるので、途中で止まっても半端なファイルが残らない）。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.unlink(missing_ok=True)
    src = sqlite3.connect(get_db_path(), timeout=10)
    try:
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    tmp.replace(dest)
    return dest


def _stamp(now: Optional[datetime], fmt: str) -> str:
    return (now or utc_now()).strftime(fmt)


def make_backup(kind: str = "manual", *, now: Optional[datetime] = None) -> Path:
    """kind: manual / before-restore。自動バックアップは ensure_daily_backup を使う。"""
    if kind not in ("manual", "before-restore"):
        raise ValueError(f"未知のバックアップ種別です: {kind}")
    return _copy_db(backup_dir() / f"scheduler-{kind}-{_stamp(now, '%Y%m%d-%H%M%S')}.db")


def prune_backups(keep: int = BACKUP_KEEP_DAYS) -> list[Path]:
    """自動バックアップだけを、新しい順に keep 件残して削除する（手動・復元前のものは消さない）。"""
    autos = sorted(backup_dir().glob("scheduler-auto-*.db"), reverse=True)
    removed = autos[keep:]
    for f in removed:
        f.unlink(missing_ok=True)
    return removed


def ensure_daily_backup(
    *, now: Optional[datetime] = None, keep: int = BACKUP_KEEP_DAYS
) -> Optional[Path]:
    """その日の自動バックアップがまだ無ければ作る。作ったときだけパスを返す。"""
    dest = backup_dir() / f"scheduler-auto-{_stamp(now, '%Y%m%d')}.db"
    if dest.exists():
        return None
    _copy_db(dest)
    prune_backups(keep)
    return dest


def list_backups() -> list[dict[str, Any]]:
    """新しい順。name / size / mtime（UTC の datetime）。"""
    out = []
    for f in backup_dir().glob("scheduler-*.db"):
        st = f.stat()
        out.append({"name": f.name, "size": st.st_size, "mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc)})
    return sorted(out, key=lambda x: x["mtime"], reverse=True)


def snapshot_bytes(conn: sqlite3.Connection) -> bytes:
    """いまのDBの中身をそのまま返す（ダウンロード用）。"""
    return conn.serialize()


def validate_backup_file(path: Path) -> None:
    """Ink Scheduler のバックアップとして使えるか調べる。ダメなら ValueError（日本語）。"""
    path = Path(path)
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as e:
        raise ValueError("ファイルを開けませんでした") from e
    try:
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        except sqlite3.DatabaseError as e:
            raise ValueError("SQLite のファイルではないか、壊れています") from e
        if result != "ok":
            raise ValueError("バックアップファイルが壊れています")
        missing = [t for t in BACKUP_TABLES if t not in tables]
        if missing:
            raise ValueError(f"Ink Scheduler のバックアップではありません（{', '.join(missing)} がありません）")
    finally:
        conn.close()


def restore_backup(src_path: Path, *, now: Optional[datetime] = None) -> Path:
    """バックアップでいまのDBを置き換える。置き換える直前の状態を before-restore として残して、そのパスを返す。

    古い形式のバックアップでも、復元後に init_db() で新しい形式へ移行する。
    """
    src_path = Path(src_path)
    validate_backup_file(src_path)
    saved = make_backup("before-restore", now=now)
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(get_db_path(), timeout=30)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    init_db()
    return saved


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


def _cli(argv: list[str]) -> int:
    init_db()
    cmd = argv[0] if argv else "init"
    if cmd == "backup":
        print(f"バックアップを作りました: {make_backup('manual')}")
    elif cmd == "restore":
        if len(argv) != 2:
            print("使い方: python db.py restore バックアップファイル")
            return 2
        try:
            saved = restore_backup(Path(argv[1]))
        except ValueError as e:
            print(f"復元できませんでした: {e}")
            return 1
        print(f"復元しました。復元前の状態は {saved} に残してあります。")
    elif cmd == "reset-password":
        # パスワードを忘れたとき:  docker compose exec admin python db.py reset-password
        with connect() as conn:
            clear_admin_password(conn)
        print("DBに保存したパスワードを消しました。.env の INK_SCHEDULER_PASSWORD（なければパスワードなし）に戻ります。")
    elif cmd == "init":
        print(f"DB を初期化しました: {get_db_path()}")
    else:
        print("使い方: python db.py [init | backup | restore ファイル | reset-password]")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
