"""
Ink Scheduler - app.py（管理画面）

起動:
    streamlit run app.py

環境変数:
    INK_SCHEDULER_DB        DBファイルの場所（省略時は db.py と同じフォルダの data/scheduler.db）
    INK_SCHEDULER_PASSWORD  設定すると、画面を開くときにパスワードを求める

画面は予約・誕生日・設定を編集するだけ。実際の送信は sender.py が行う（別プロセス）。
Webhook URL は秘密情報なので、画面には末尾4文字だけ表示する。
"""

from __future__ import annotations

import hmac
import os
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import streamlit as st

import db
import sender

st.set_page_config(page_title="Ink Scheduler", page_icon="🖤", layout="wide")

KIND_LABELS = {"once": "1回だけ", "daily": "毎日", "weekly": "毎週", "monthly": "毎月", "yearly": "毎年"}
POLICY_LABELS = {"notify": "確認通知", "late_send": "遅れて送る", "skip": "スキップ"}
STATUS_LABELS = {"sent": "送信", "failed": "失敗", "notified": "通知", "skipped": "スキップ"}
SOURCE_LABELS = {"schedule": "予約", "birthday": "誕生日", "system": "システム"}
WEEKDAYS = "月火水木金土日"
DAY_CHOICES = list(range(1, 32)) + [-1]
NEW_URL = "__new__"
KEEP_URL = "__keep__"


# ----------------------------------------------------------------------
# 共通
# ----------------------------------------------------------------------

def require_login() -> None:
    password = os.environ.get("INK_SCHEDULER_PASSWORD", "")
    if not password or st.session_state.get("authed"):
        return
    st.title("Ink Scheduler")
    entered = st.text_input("パスワード", type="password", key="login_pw")
    if st.button("ログイン", key="login_btn"):
        if hmac.compare_digest(entered.encode(), password.encode()):
            st.session_state["authed"] = True
            st.rerun()
        else:
            st.error("パスワードが違います")
    st.stop()


@st.cache_resource
def init_once() -> bool:
    db.init_db()
    return True


def flash(message: str, kind: str = "success") -> None:
    st.session_state["_flash"] = (kind, message)


def show_flash() -> None:
    item = st.session_state.pop("_flash", None)
    if item:
        getattr(st, item[0])(item[1])


def done(conn, message: str) -> None:
    """保存して画面を更新する。st.rerun() は例外で抜けるため、先に commit しておく。"""
    conn.commit()
    flash(message)
    st.rerun()


def nonce(name: str) -> int:
    return st.session_state.get(f"_n_{name}", 0)


def bump(name: str) -> None:
    st.session_state[f"_n_{name}"] = nonce(name) + 1


def local_str(utc_str, tz: ZoneInfo, fmt: str = "%Y-%m-%d %H:%M") -> str:
    if not utc_str:
        return "—"
    return db.from_utc_str(utc_str).astimezone(tz).strftime(fmt)


def day_label(x: int) -> str:
    return "月末" if x == -1 else f"{x}日"


def describe_schedule(r) -> str:
    kind, t = r["kind"], r["run_time"]
    if kind == "once":
        return r["once_at"].replace("T", " ")
    if kind == "daily":
        return f"毎日 {t}"
    if kind == "weekly":
        return f"毎週{WEEKDAYS[r['weekday']]}曜 {t}"
    if kind == "monthly":
        return f"毎月{day_label(r['month_day'])} {t}"
    return f"毎年{r['month']}月{day_label(r['month_day'])} {t}"


def schedule_state(r) -> str:
    if r["enabled"]:
        return "有効"
    return "完了" if (r["kind"] == "once" and r["last_run"]) else "停止"


def used_webhooks(conn) -> list[str]:
    urls = {r["webhook_url"] for r in db.list_schedules(conn)}
    urls |= {r["webhook_url"] for r in db.list_birthdays(conn)}
    return sorted(urls)


def webhook_input(conn, key: str, *, keep: str | None = None) -> str:
    """送信先の選択。フォーム内でも動くよう、選択欄と入力欄を両方出す。"""
    options: list[tuple[str, str]] = []
    if keep:
        options.append((KEEP_URL, f"今のまま（{db.mask_webhook(keep)}）"))
    for u in used_webhooks(conn):
        if u != keep:
            options.append((u, f"使用中のURLを使う（{db.mask_webhook(u)}）"))
    options.append((NEW_URL, "新しいURLを入力する"))
    idx = st.selectbox(
        "送信先", range(len(options)), format_func=lambda i: options[i][1], key=f"{key}_wh_choice"
    )
    new = st.text_input(
        "新しいURL（上で「新しいURLを入力する」を選んだときだけ使います）",
        type="password", key=f"{key}_wh_new", placeholder="https://discord.com/api/webhooks/…",
    )
    value = options[idx][0]
    if value == NEW_URL:
        return new.strip()
    return keep if value == KEEP_URL else value


def schedule_timing_fields(kind: str, key: str, tz: ZoneInfo, d=None) -> dict:
    """種類に合わせた日時の入力欄。db.add_schedule / update_schedule に渡す項目を返す。"""
    out: dict = {}
    if kind == "once":
        if d is not None and d["once_at"]:
            default = datetime.fromisoformat(d["once_at"])
        else:
            default = (datetime.now(tz) + timedelta(hours=1)).replace(second=0, microsecond=0, tzinfo=None)
        c1, c2 = st.columns(2)
        day = c1.date_input("日付", value=default.date(), key=f"{key}_date")
        tm = c2.time_input("時刻", value=default.time(), key=f"{key}_time", step=60)
        out["once_at"] = datetime.combine(day, tm).strftime("%Y-%m-%dT%H:%M")
        return out

    tm = st.time_input(
        "時刻", value=dtime.fromisoformat(d["run_time"] if d is not None else "09:00"),
        key=f"{key}_rt", step=60,
    )
    out["run_time"] = tm.strftime("%H:%M")
    if kind == "weekly":
        out["weekday"] = st.selectbox(
            "曜日", range(7), index=d["weekday"] if d is not None else 0,
            format_func=lambda i: f"{WEEKDAYS[i]}曜日", key=f"{key}_wd",
        )
    if kind == "yearly":
        out["month"] = st.selectbox(
            "月", range(1, 13), index=(d["month"] - 1) if d is not None else 0,
            format_func=lambda m: f"{m}月", key=f"{key}_mo",
        )
    if kind in ("monthly", "yearly"):
        out["month_day"] = st.selectbox(
            "日", DAY_CHOICES, index=DAY_CHOICES.index(d["month_day"]) if d is not None else 0,
            format_func=day_label, key=f"{key}_md",
        )
    return out


def recompute_schedules(conn) -> None:
    """タイムゾーン変更後に、有効な予約の次回実行日時を計算し直す。"""
    for r in db.list_schedules(conn):
        if r["enabled"]:
            try:
                db.update_schedule(conn, r["id"])
            except ValueError:
                pass  # 過去になった1回だけの予約など


# ----------------------------------------------------------------------
# 予約
# ----------------------------------------------------------------------

def render_schedules(conn, tz: ZoneInfo) -> None:
    rows = db.list_schedules(conn)
    st.subheader("予約の一覧")
    if rows:
        st.dataframe(
            [
                {
                    "ID": r["id"], "名前": r["name"], "タイミング": describe_schedule(r),
                    "次回": local_str(r["next_run"], tz) if r["enabled"] else "—",
                    "送信先": db.mask_webhook(r["webhook_url"]),
                    "取りこぼし": POLICY_LABELS[r["missed_policy"]], "状態": schedule_state(r),
                    "本文": r["content"].replace("\n", " ")[:40],
                }
                for r in rows
            ],
            hide_index=True, width="stretch",
        )
    else:
        st.info("まだ予約がありません。下のフォームから追加してください。")

    st.subheader("予約を追加")
    n = nonce("add_sched")
    kind = st.radio(
        "種類", list(KIND_LABELS), format_func=KIND_LABELS.get, horizontal=True, key=f"as_kind_{n}"
    )
    with st.form(f"as_form_{n}"):
        name = st.text_input("名前（管理用）", key=f"as_name_{n}")
        content = st.text_area("本文", key=f"as_content_{n}", height=120, help="Discord に送る文章です（2000文字まで）")
        url = webhook_input(conn, f"as_{n}")
        timing = schedule_timing_fields(kind, f"as_{n}", tz)
        policy = st.selectbox(
            "取りこぼし時の動作", list(POLICY_LABELS), format_func=POLICY_LABELS.get, key=f"as_pol_{n}",
            help="NUC停止などで予定時刻を過ぎたときの動作です",
        )
        submitted = st.form_submit_button("追加する", key=f"as_submit_{n}")
    if submitted:
        try:
            new_id = db.add_schedule(
                conn, name=name, webhook_url=url, content=content, kind=kind, missed_policy=policy, **timing
            )
        except ValueError as e:
            st.error(str(e))
        else:
            bump("add_sched")
            done(conn, f"予約を追加しました（#{new_id}）")

    if not rows:
        return
    st.subheader("予約を操作")
    ids = [r["id"] for r in rows]
    sel = st.selectbox(
        "対象", ids, key="sched_sel",
        format_func=lambda i: next(f"#{r['id']} {r['name']}" for r in rows if r["id"] == i),
    )
    row = db.get_schedule(conn, sel)
    confirm = st.checkbox("確認しました（今すぐ送信・削除に必要）", key=f"sched_confirm_{sel}")
    c1, c2, c3 = st.columns(3)
    if c1.button("今すぐ送信", key=f"sched_now_{sel}", disabled=not confirm):
        res = sender.send_now(conn, "schedule", sel)
        if res.ok:
            done(conn, "送信しました（予定はそのままです）")
        st.error(f"送信に失敗しました: {res.detail}")
        conn.commit()
    if row["enabled"]:
        if c2.button("停止する", key=f"sched_off_{sel}"):
            db.set_schedule_enabled(conn, sel, False)
            done(conn, "停止しました")
    elif row["kind"] != "once" or not row["last_run"]:
        if c2.button("再開する", key=f"sched_on_{sel}"):
            try:
                db.set_schedule_enabled(conn, sel, True)
            except ValueError as e:
                st.error(str(e))
            else:
                done(conn, "再開しました（止まっていた間の分は送りません）")
    if c3.button("削除する", key=f"sched_del_{sel}", disabled=not confirm):
        db.delete_schedule(conn, sel)
        done(conn, "削除しました")

    with st.expander("この予約を編集"):
        with st.form(f"es_form_{sel}"):
            e_name = st.text_input("名前", value=row["name"], key=f"es_{sel}_name")
            e_content = st.text_area("本文", value=row["content"], key=f"es_{sel}_content", height=120)
            e_url = webhook_input(conn, f"es_{sel}", keep=row["webhook_url"])
            e_timing = schedule_timing_fields(row["kind"], f"es_{sel}", tz, row)
            e_policy = st.selectbox(
                "取りこぼし時の動作", list(POLICY_LABELS), format_func=POLICY_LABELS.get,
                index=list(POLICY_LABELS).index(row["missed_policy"]), key=f"es_{sel}_pol",
            )
            st.caption(f"種類は「{KIND_LABELS[row['kind']]}」のままです。変えたい場合は、削除して追加し直してください。")
            saved = st.form_submit_button("保存する", key=f"es_submit_{sel}")
        if saved:
            try:
                db.update_schedule(
                    conn, sel, name=e_name, content=e_content, webhook_url=e_url,
                    missed_policy=e_policy, **e_timing,
                )
            except ValueError as e:
                st.error(str(e))
            else:
                done(conn, "保存しました")


# ----------------------------------------------------------------------
# 誕生日
# ----------------------------------------------------------------------

def render_birthdays(conn) -> None:
    rows = db.list_birthdays(conn)
    st.subheader("誕生日の一覧")
    if rows:
        st.dataframe(
            [
                {
                    "ID": r["id"], "名前": r["name"], "誕生日": f"{r['month']}月{r['day']}日",
                    "送信先": db.mask_webhook(r["webhook_url"]), "Discord ID": r["discord_id"] or "—",
                    "取りこぼし": POLICY_LABELS[r["missed_policy"]], "状態": "有効" if r["enabled"] else "停止",
                    "テンプレート": r["template"].replace("\n", " ")[:40],
                }
                for r in rows
            ],
            hide_index=True, width="stretch",
        )
    else:
        st.info("まだ誕生日がありません。下のフォームから追加してください。")
    st.caption(
        "誕生日は月日だけ保存します（年は保存しません）。送信時刻は「設定」で変えられます。"
        "2月29日生まれは、うるう年以外は2月28日に送ります。"
    )

    st.subheader("誕生日を追加")
    n = nonce("add_bd")
    with st.form(f"ab_form_{n}"):
        name = st.text_input("名前", key=f"ab_name_{n}")
        c1, c2 = st.columns(2)
        month = c1.selectbox("月", range(1, 13), format_func=lambda m: f"{m}月", key=f"ab_mo_{n}")
        day = c2.selectbox("日", range(1, 32), format_func=lambda d: f"{d}日", key=f"ab_day_{n}")
        url = webhook_input(conn, f"ab_{n}")
        template = st.text_area(
            "メッセージ", value="{name}さん、お誕生日おめでとうございます！", key=f"ab_tpl_{n}", height=100,
            help="{name} が名前に置き換わります",
        )
        discord_id = st.text_input("Discord ID（任意）", key=f"ab_did_{n}", help="入れると <@ID> でメンションします")
        policy = st.selectbox(
            "取りこぼし時の動作", list(POLICY_LABELS), format_func=POLICY_LABELS.get,
            index=list(POLICY_LABELS).index("late_send"), key=f"ab_pol_{n}",
        )
        submitted = st.form_submit_button("追加する", key=f"ab_submit_{n}")
    if submitted:
        try:
            new_id = db.add_birthday(
                conn, name=name, month=month, day=day, webhook_url=url,
                template=template, discord_id=discord_id, missed_policy=policy,
            )
        except ValueError as e:
            st.error(str(e))
        else:
            bump("add_bd")
            done(conn, f"誕生日を追加しました（#{new_id}）")

    if not rows:
        return
    st.subheader("誕生日を操作")
    ids = [r["id"] for r in rows]
    sel = st.selectbox(
        "対象", ids, key="bd_sel",
        format_func=lambda i: next(f"#{r['id']} {r['name']}（{r['month']}/{r['day']}）" for r in rows if r["id"] == i),
    )
    row = db.get_birthday(conn, sel)
    confirm = st.checkbox("確認しました（今すぐ送信・削除に必要）", key=f"bd_confirm_{sel}")
    c1, c2, c3 = st.columns(3)
    if c1.button("今すぐ送信", key=f"bd_now_{sel}", disabled=not confirm):
        res = sender.send_now(conn, "birthday", sel)
        if res.ok:
            done(conn, "送信しました")
        st.error(f"送信に失敗しました: {res.detail}")
        conn.commit()
    if row["enabled"]:
        if c2.button("停止する", key=f"bd_off_{sel}"):
            db.update_birthday(conn, sel, enabled=False)
            done(conn, "停止しました")
    elif c2.button("再開する", key=f"bd_on_{sel}"):
        db.update_birthday(conn, sel, enabled=True)
        done(conn, "再開しました")
    if c3.button("削除する", key=f"bd_del_{sel}", disabled=not confirm):
        db.delete_birthday(conn, sel)
        done(conn, "削除しました")

    with st.expander("この誕生日を編集"):
        with st.form(f"eb_form_{sel}"):
            e_name = st.text_input("名前", value=row["name"], key=f"eb_{sel}_name")
            c1, c2 = st.columns(2)
            e_month = c1.selectbox(
                "月", range(1, 13), index=row["month"] - 1, format_func=lambda m: f"{m}月", key=f"eb_{sel}_mo"
            )
            e_day = c2.selectbox(
                "日", range(1, 32), index=row["day"] - 1, format_func=lambda d: f"{d}日", key=f"eb_{sel}_day"
            )
            e_url = webhook_input(conn, f"eb_{sel}", keep=row["webhook_url"])
            e_tpl = st.text_area("メッセージ", value=row["template"], key=f"eb_{sel}_tpl", height=100)
            e_did = st.text_input("Discord ID（任意）", value=row["discord_id"] or "", key=f"eb_{sel}_did")
            e_policy = st.selectbox(
                "取りこぼし時の動作", list(POLICY_LABELS), format_func=POLICY_LABELS.get,
                index=list(POLICY_LABELS).index(row["missed_policy"]), key=f"eb_{sel}_pol",
            )
            saved = st.form_submit_button("保存する", key=f"eb_submit_{sel}")
        if saved:
            try:
                db.update_birthday(
                    conn, sel, name=e_name, month=e_month, day=e_day, webhook_url=e_url,
                    template=e_tpl, discord_id=e_did, missed_policy=e_policy,
                )
            except ValueError as e:
                st.error(str(e))
            else:
                done(conn, "保存しました")


# ----------------------------------------------------------------------
# 履歴
# ----------------------------------------------------------------------

def render_logs(conn, tz: ZoneInfo) -> None:
    st.subheader("送信履歴")
    chosen = st.multiselect(
        "表示する状態", list(STATUS_LABELS), default=list(STATUS_LABELS),
        format_func=STATUS_LABELS.get, key="log_filter",
    )
    rows = [r for r in db.list_logs(conn, 300) if r["status"] in chosen]
    if not rows:
        st.info("履歴はまだありません。")
        return
    st.dataframe(
        [
            {
                "時刻": local_str(r["created_at"], tz, "%m/%d %H:%M:%S"),
                "種類": SOURCE_LABELS.get(r["source"], r["source"]),
                "名前": r["name"], "状態": STATUS_LABELS[r["status"]],
                "予定": local_str(r["scheduled_for"], tz, "%m/%d %H:%M"),
                "詳細": r["detail"] or "",
            }
            for r in rows
        ],
        hide_index=True, width="stretch",
    )


# ----------------------------------------------------------------------
# 設定
# ----------------------------------------------------------------------

def render_settings(conn) -> None:
    st.subheader("基本設定")
    with st.form("settings_form"):
        tzname = st.text_input("タイムゾーン", value=db.get_setting(conn, "timezone"), help="例: Asia/Tokyo")
        btime = st.time_input(
            "誕生日の送信時刻", value=dtime.fromisoformat(db.get_setting(conn, "birthday_time")), step=60
        )
        catchup = st.number_input(
            "予約を遅れて送る許容時間（時間）", min_value=0, max_value=168, step=1,
            value=int(db.get_setting(conn, "catchup_hours")),
            help="「遅れて送る」の予約が、この時間以内なら送ります。誕生日は当日中です",
        )
        saved = st.form_submit_button("保存する", key="settings_submit")
    if saved:
        try:
            old_tz = db.get_setting(conn, "timezone")
            db.set_setting(conn, "timezone", tzname)
            db.set_setting(conn, "birthday_time", btime.strftime("%H:%M"))
            db.set_setting(conn, "catchup_hours", str(int(catchup)))
            if tzname.strip() != old_tz:
                recompute_schedules(conn)
        except ValueError as e:
            conn.rollback()
            st.error(str(e))
        else:
            done(conn, "設定を保存しました")

    st.subheader("確認用Webhook（取りこぼしの通知先）")
    st.caption("所長だけが見られる非公開チャンネルのWebhookを入れてください。予約本文が引用されます。")
    current = db.get_setting(conn, "review_webhook_url")
    st.write("現在: " + (db.mask_webhook(current) if current else "未設定"))
    new_url = st.text_input(
        "新しいURL", type="password", key="rv_new", placeholder="https://discord.com/api/webhooks/…"
    )
    c1, c2, c3 = st.columns(3)
    if c1.button("保存する", key="rv_save"):
        try:
            db.set_setting(conn, "review_webhook_url", new_url)
        except ValueError as e:
            conn.rollback()
            st.error(str(e))
        else:
            done(conn, "確認用Webhookを保存しました")
    if c2.button("テスト送信", key="rv_test"):
        url = new_url.strip() or current
        try:
            url = db.validate_webhook(url)
        except ValueError as e:
            st.error(str(e))
        else:
            res = sender.post_webhook(url, sender.review_payload("【接続テスト】Ink Scheduler から確認用Webhookへ送信できています。"))
            if res.ok:
                st.success("送信できました。Discord のチャンネルを確認してください。")
            else:
                st.error(f"送信できませんでした: {res.detail}")
    if c3.button("設定を消す", key="rv_clear", disabled=not current):
        db.set_setting(conn, "review_webhook_url", "")
        done(conn, "確認用Webhookを消しました")


# ----------------------------------------------------------------------
# 画面
# ----------------------------------------------------------------------

def main() -> None:
    require_login()
    init_once()
    st.title("Ink Scheduler")
    show_flash()

    with db.connect() as conn:
        tz = db.get_tz(conn)
        needs_review = any(r["missed_policy"] == "notify" for r in db.list_schedules(conn)) or any(
            r["missed_policy"] == "notify" for r in db.list_birthdays(conn)
        )
        if needs_review and not db.get_setting(conn, "review_webhook_url"):
            st.warning(
                "取りこぼし時の動作が「確認通知」の項目がありますが、確認用Webhookが未設定です。"
                "「設定」タブで登録してください（未設定のままだと通知されず、スキップされます）。"
            )
        tab_s, tab_b, tab_l, tab_c = st.tabs(["予約", "誕生日", "履歴", "設定"])
        with tab_s:
            render_schedules(conn, tz)
        with tab_b:
            render_birthdays(conn)
        with tab_l:
            render_logs(conn, tz)
        with tab_c:
            render_settings(conn)


main()
