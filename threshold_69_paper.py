#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Independent virtual-only 6.9x Crash strategy simulator.

No real bets, payment APIs, or money transfers.
"""

import os
import math
import time
import sqlite3

CRASH_DB = os.environ.get("CRASH_DB_PATH", "/data/crash.db")

DEFAULT_STATE_DB = os.path.join(
    os.path.dirname(os.path.abspath(CRASH_DB)),
    "threshold_69_paper.db",
)

STATE_DB = os.environ.get(
    "THRESHOLD69_DB_PATH",
    DEFAULT_STATE_DB,
)

INITIAL_BANKROLL = 1_000_000
BASE_STAKE = 100
TARGET = 6.9
TARGET_CYCLE_PROFIT = 1_000
EPSILON = 0.000001


def connect():
    folder = os.path.dirname(os.path.abspath(STATE_DB))
    os.makedirs(folder, exist_ok=True)

    con = sqlite3.connect(STATE_DB, timeout=20)
    con.execute("PRAGMA busy_timeout=20000")
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db(con=None):
    own = con is None
    if own:
        con = connect()

    try:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS strategy69_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                initial_bankroll REAL NOT NULL,
                bankroll REAL NOT NULL,
                active INTEGER NOT NULL DEFAULT 0,
                started_at REAL,
                last_processed_id INTEGER NOT NULL DEFAULT 0,
                cycle_losses REAL NOT NULL DEFAULT 0,
                wins INTEGER NOT NULL DEFAULT 0,
                losses INTEGER NOT NULL DEFAULT 0,
                current_loss_streak INTEGER NOT NULL DEFAULT 0,
                max_loss_streak INTEGER NOT NULL DEFAULT 0,
                max_stake REAL NOT NULL DEFAULT 0,
                peak_bankroll REAL NOT NULL,
                max_drawdown REAL NOT NULL DEFAULT 0,
                max_drawdown_pct REAL NOT NULL DEFAULT 0,
                min_bankroll REAL NOT NULL,
                stopped_reason TEXT,
                stopped_at REAL
            );

            CREATE TABLE IF NOT EXISTS strategy69_games (
                game_id INTEGER PRIMARY KEY,
                rate REAL NOT NULL,
                stake REAL NOT NULL,
                outcome TEXT NOT NULL,
                profit REAL NOT NULL,
                bankroll_after REAL NOT NULL,
                cycle_losses_after REAL NOT NULL,
                processed_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS strategy69_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT NOT NULL UNIQUE,
                message TEXT NOT NULL,
                sent_at REAL
            );
        """)

        con.execute("""
            INSERT OR IGNORE INTO strategy69_state (
                id, initial_bankroll, bankroll, active,
                last_processed_id, peak_bankroll, min_bankroll
            )
            VALUES (1, ?, ?, 0, 0, ?, ?)
        """, (
            INITIAL_BANKROLL,
            INITIAL_BANKROLL,
            INITIAL_BANKROLL,
            INITIAL_BANKROLL,
        ))

        con.commit()
    finally:
        if own:
            con.close()


def get_latest_game(crash_db=None):
    path = crash_db or CRASH_DB

    con = sqlite3.connect(path, timeout=15)
    try:
        return con.execute("""
            SELECT game_id, rate
            FROM crashes
            ORDER BY game_id DESC
            LIMIT 1
        """).fetchone()
    finally:
        con.close()


def add_notification(con, event_key, message):
    con.execute("""
        INSERT OR IGNORE INTO strategy69_notifications
            (event_key, message)
        VALUES (?, ?)
    """, (str(event_key), str(message)))


def required_stake(cycle_losses):
    """
    First bet is 100.
    After a loss, calculate the amount needed to recover accumulated
    losses and achieve the 1,000-unit cycle profit at target 6.9x.
    """
    if cycle_losses <= 0:
        return BASE_STAKE

    return math.ceil(
        (cycle_losses + TARGET_CYCLE_PROFIT) / (TARGET - 1)
    )


def balance_snapshot(bankroll, active, cycle_losses):
    """Return the next stake and unreserved virtual balance."""
    bankroll = float(bankroll)

    if not active:
        return 0.0, bankroll

    next_stake = float(required_stake(cycle_losses))

    # Keep the next stake reserved only if it is affordable.
    if next_stake < bankroll - EPSILON:
        free_bankroll = bankroll - next_stake
    else:
        free_bankroll = bankroll

    return next_stake, free_bankroll


def balance_lines(bankroll, active, cycle_losses):
    next_stake, free_bankroll = balance_snapshot(
        bankroll, active, cycle_losses
    )
    lines = [
        f"💰 موجودی اصلی: {float(bankroll):,.2f} تومان",
        f"🟢 موجودی آزاد: {free_bankroll:,.2f} تومان",
    ]

    if active:
        lines.append(f"💵 شرط بعدی: {next_stake:,.2f} تومان")
    else:
        lines.append("💵 شرط بعدی: ندارد؛ استراتژی متوقف است")

    return "\n".join(lines)



def history_lines(con):
    """Calculate streak and win-rate statistics from recorded virtual games."""
    rows = con.execute(
        "SELECT outcome FROM strategy69_games ORDER BY game_id ASC"
    ).fetchall()

    wins = sum(outcome == "win" for (outcome,) in rows)
    losses = sum(outcome == "loss" for (outcome,) in rows)
    total = wins + losses
    rate = wins * 100.0 / total if total else 0.0

    cw = cl = mw = ml = 0
    for (outcome,) in rows:
        if outcome == "win":
            cw += 1
            cl = 0
            mw = max(mw, cw)
        elif outcome == "loss":
            cl += 1
            cw = 0
            ml = max(ml, cl)
        else:
            cw = cl = 0

    return "\n".join([
        "📚 سابقه شرط‌بندی مجازی",
        f"🎯 تعداد کل شرط‌ها: {total:,}",
        f"✅ تعداد بردها: {wins:,}",
        f"❌ تعداد باخت‌ها: {losses:,}",
        f"📊 درصد برد: {rate:.2f}%",
        f"🔥 بردهای متوالی فعلی: {cw}",
        f"💔 باخت‌های متوالی فعلی: {cl}",
        f"🏆 بیشترین برد متوالی: {mw}",
        f"📉 بیشترین باخت متوالی: {ml}",
    ])


def start_or_status(crash_db=None):
    """
    First press starts a fresh forward-only test at the current latest ID.
    Subsequent presses return status; they do not reset the bankroll.
    """
    latest = get_latest_game(crash_db)

    if latest is None:
        return "❌ دیتابیس بازی‌ها خالی است؛ شبیه‌ساز شروع نشد."

    latest_id = int(latest[0])
    con = connect()

    try:
        init_db(con)
        con.execute("BEGIN IMMEDIATE")

        state = con.execute("""
            SELECT started_at, active, stopped_reason,
                   last_processed_id, bankroll, cycle_losses
            FROM strategy69_state
            WHERE id = 1
        """).fetchone()

        (
            started_at, active, stopped_reason, last_id,
            bankroll, cycle_losses,
        ) = state

        if started_at is None:
            con.execute("""
                UPDATE strategy69_state
                SET active = 1,
                    started_at = ?,
                    last_processed_id = ?,
                    stopped_reason = NULL,
                    stopped_at = NULL
                WHERE id = 1
            """, (time.time(), latest_id))

            add_notification(
                con,
                "started",
                "🧪 تست مجازی استراتژی 6.9x شروع شد.\n"
                f"سرمایه اولیه: {INITIAL_BANKROLL:,.0f} تومان\n"
                f"{balance_lines(INITIAL_BANKROLL, True, 0)}\n"
                f"آخرین بازی هنگام شروع: {latest_id}\n"
                f"شرط از بازی بعدی محاسبه می‌شود: {latest_id + 1}\n"
                "شرط‌بندی کاملاً مجازی است."
            )

            con.commit()

            return (
                "🧪 تست استراتژی 6.9x شروع شد.\n\n"
                f"💰 سرمایه اولیه: {INITIAL_BANKROLL:,.0f} تومان\n"
                f"{balance_lines(INITIAL_BANKROLL, True, 0)}\n"
                f"🎯 ضریب هدف: {TARGET}x\n"
                f"💵 شرط اول: {BASE_STAKE:,} تومان\n"
                f"📈 سود هدف چرخه: {TARGET_CYCLE_PROFIT:,} تومان\n"
                f"📍 آخرین بازی هنگام شروع: {latest_id}\n"
                f"▶️ پردازش از بازی {latest_id + 1} آغاز می‌شود.\n\n"
                "فقط بازی‌های جدید پردازش می‌شوند؛ "
                "داده‌های تاریخی دوباره شبیه‌سازی نمی‌شوند."
            )

        con.commit()

        if active:
            state_text = "در حال اجرا"
        elif stopped_reason:
            state_text = f"متوقف‌شده: {stopped_reason}"
        else:
            state_text = "غیرفعال"

        return (
            "🧪 وضعیت استراتژی 6.9x\n\n"
            f"🚦 وضعیت: {state_text}\n"
            f"{balance_lines(bankroll, bool(active), cycle_losses)}\n"
            f"{history_lines(con)}\n\n"
            f"📍 آخرین بازی پردازش‌شده: {int(last_id)}\n\n"
            "برای جلوگیری از پاک شدن سابقه، "
            "فشردن دوباره دکمه سرمایه را ریست نمی‌کند."
        )

    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def process_tick(crash_db=None):
    """
    Process every newly recorded game once, in ascending game-ID order.
    A bet is virtual and is considered settled using the recorded rate.
    The simulator stops before a bet could reduce bankroll to zero.
    """
    path = crash_db or CRASH_DB
    con = connect()

    try:
        init_db(con)
        con.execute("BEGIN IMMEDIATE")

        state = con.execute("""
            SELECT active, bankroll, last_processed_id, cycle_losses,
                   wins, losses, current_loss_streak, max_loss_streak,
                   max_stake, peak_bankroll, max_drawdown,
                   max_drawdown_pct, min_bankroll, stopped_reason
            FROM strategy69_state
            WHERE id = 1
        """).fetchone()

        (
            active, bankroll, last_id, cycle_losses,
            wins, losses, current_streak, max_streak,
            max_stake, peak, max_dd, max_dd_pct, min_bankroll,
            stopped_reason,
        ) = state

        if not active:
            con.commit()
            return

        source = sqlite3.connect(path, timeout=15)
        try:
            rows = source.execute("""
                SELECT game_id, rate
                FROM crashes
                WHERE game_id > ?
                ORDER BY game_id ASC
            """, (int(last_id),)).fetchall()
        finally:
            source.close()

        expected_id = int(last_id) + 1

        for game_id, rate in rows:
            game_id = int(game_id)

            # Never skip a missing game ID. Wait for database recovery.
            if game_id != expected_id:
                break

            rate = float(rate)

            # Never skip an invalid result and continue with later games.
            if not math.isfinite(rate) or rate <= 0:
                break

            stake = float(required_stake(cycle_losses))

            # Stop before placing a virtual bet that cannot be covered.
            # The bankroll must remain strictly positive.
            if bankroll <= 0 or stake >= bankroll - EPSILON:
                reason = (
                    f"شرط لازم {stake:,.2f} تومان است؛ "
                    f"موجودی آزاد {bankroll:,.2f} تومان است."
                )

                failure_message = (
                    "🛑 استراتژی شکست خورد | 6.9x\n\n"
                    f"🎮 بازی بررسی‌نشده: {game_id}\n"
                    f"{balance_lines(bankroll, True, cycle_losses)}\n"
                    f"💵 شرط موردنیاز: {stake:,.2f} تومان\n"
                    "⛔ برای این بازی هیچ شرطی ثبت نشد.\n"
                    f"📌 علت شکست: {reason}\n"
                    "🚫 شبیه‌ساز متوقف شد و خودکار ادامه نمی‌دهد."
                )

                con.execute("""
                    UPDATE strategy69_state
                    SET active = 0,
                        stopped_reason = ?,
                        stopped_at = ?,
                        last_processed_id = ?
                    WHERE id = 1 AND active = 1
                """, (
                    "استراتژی شکست خورد: " + reason,
                    time.time(),
                    last_id,
                ))

                add_notification(
                    con,
                    f"strategy69_failed:{game_id}",
                    failure_message,
                )
                break

            max_stake = max(max_stake, stake)

            if rate >= TARGET:
                profit = stake * (TARGET - 1)
                bankroll += profit
                cycle_losses = 0.0
                wins += 1
                current_streak = 0
                outcome = "win"
            else:
                profit = -stake
                bankroll -= stake
                cycle_losses += stake
                losses += 1
                current_streak += 1
                max_streak = max(max_streak, current_streak)
                outcome = "loss"

            peak = max(peak, bankroll)
            drawdown = peak - bankroll
            drawdown_pct = (100.0 * drawdown / peak) if peak > 0 else 0.0
            max_dd = max(max_dd, drawdown)
            max_dd_pct = max(max_dd_pct, drawdown_pct)
            min_bankroll = min(min_bankroll, bankroll)

            con.execute("""
                INSERT OR IGNORE INTO strategy69_games (
                    game_id, rate, stake, outcome, profit,
                    bankroll_after, cycle_losses_after, processed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                game_id, rate, stake, outcome, profit,
                bankroll, cycle_losses, time.time()
            ))

            con.execute("""
                UPDATE strategy69_state
                SET bankroll = ?,
                    last_processed_id = ?,
                    cycle_losses = ?,
                    wins = ?,
                    losses = ?,
                    current_loss_streak = ?,
                    max_loss_streak = ?,
                    max_stake = ?,
                    peak_bankroll = ?,
                    max_drawdown = ?,
                    max_drawdown_pct = ?,
                    min_bankroll = ?
                WHERE id = 1
            """, (
                bankroll, game_id, cycle_losses, wins, losses,
                current_streak, max_streak, max_stake, peak,
                max_dd, max_dd_pct, min_bankroll
            ))

            expected_id = game_id + 1

            add_notification(
                con,
                f"game:{game_id}",
                f"{'✅ برد' if outcome == 'win' else '❌ باخت'} | "
                "تست مجازی استراتژی 6.9x\n\n"
                f"🎮 بازی: {game_id}\n"
                f"📊 ضریب ثبت‌شده: {rate:.2f}x\n"
                f"💵 مبلغ شرط مجازی: {stake:,.2f} تومان\n"
                f"📈 سود/زیان این بازی: {profit:+,.2f} تومان\n"
                f"{balance_lines(bankroll, True, cycle_losses)}\n"
                f"🧾 ضرر چرخه جاری: {cycle_losses:,.2f} تومان\n"
                f"📉 بیشترین افت سرمایه: {max_dd:,.2f} تومان "
                f"({max_dd_pct:.2f}%)"
            )

        con.commit()

    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def get_unsent_notifications():
    con = connect()
    try:
        init_db(con)
        return con.execute("""
            SELECT id, message
            FROM strategy69_notifications
            WHERE sent_at IS NULL
            ORDER BY id
        """).fetchall()
    finally:
        con.close()


def mark_notification_sent(notification_id):
    con = connect()
    try:
        init_db(con)
        con.execute("""
            UPDATE strategy69_notifications
            SET sent_at = ?
            WHERE id = ? AND sent_at IS NULL
        """, (time.time(), int(notification_id)))
        con.commit()
    finally:
        con.close()


def get_stats():
    con = connect()
    try:
        init_db(con)

        row = con.execute("""
            SELECT initial_bankroll, bankroll, active, started_at,
                   last_processed_id, cycle_losses, wins, losses,
                   current_loss_streak, max_loss_streak, max_stake,
                   peak_bankroll, max_drawdown, max_drawdown_pct,
                   min_bankroll, stopped_reason
            FROM strategy69_state
            WHERE id = 1
        """).fetchone()

        (
            initial, bankroll, active, started_at, last_id,
            cycle_losses, wins, losses, current_streak, max_streak,
            max_stake, peak, max_dd, max_dd_pct, min_bankroll,
            stopped_reason,
        ) = row

        total = wins + losses
        win_rate = (100.0 * wins / total) if total else 0.0
        next_stake, free_bankroll = balance_snapshot(
            bankroll, bool(active), cycle_losses
        )

        return {
            "initial": float(initial),
            "bankroll": float(bankroll),
            "free_bankroll": float(free_bankroll),
            "next_stake": float(next_stake),
            "active": bool(active),
            "started": started_at is not None,
            "last_id": int(last_id),
            "cycle_losses": float(cycle_losses),
            "wins": int(wins),
            "losses": int(losses),
            "decided": total,
            "win_rate": win_rate,
            "current_loss_streak": int(current_streak),
            "max_loss_streak": int(max_streak),
            "max_stake": float(max_stake),
            "peak": float(peak),
            "max_drawdown": float(max_dd),
            "max_drawdown_pct": float(max_dd_pct),
            "min_bankroll": float(min_bankroll),
            "net": float(bankroll) - float(initial),
            "stopped_reason": stopped_reason,
        }
    finally:
        con.close()
