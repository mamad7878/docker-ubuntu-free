#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Independent virtual-only Strategy 1.5.

Only processes actual Rule A signals that:
- were confirmed sent to Telegram,
- have a settled target result,
- occurred after this strategy was first started.

No real bets or money transfers.
"""

import os
import math
import time
import sqlite3

import rule_b_signals as rule_a

CRASH_DB = os.environ.get("CRASH_DB_PATH", "/data/crash.db")

DEFAULT_STATE_DB = os.path.join(
    os.path.dirname(os.path.abspath(CRASH_DB)),
    "strategy_15_paper.db",
)

STATE_DB = os.environ.get("STRATEGY15_DB_PATH", DEFAULT_STATE_DB)

INITIAL_BANKROLL = 1_000_000
BASE_STAKE = 100
TARGET = 1.5
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
            CREATE TABLE IF NOT EXISTS strategy15_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                initial_bankroll REAL NOT NULL,
                bankroll REAL NOT NULL,
                active INTEGER NOT NULL DEFAULT 0,
                started_at REAL,
                start_game_id INTEGER NOT NULL DEFAULT 0,
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

            CREATE TABLE IF NOT EXISTS strategy15_games (
                game_id INTEGER PRIMARY KEY,
                rate REAL NOT NULL,
                stake REAL NOT NULL,
                outcome TEXT NOT NULL,
                profit REAL NOT NULL,
                bankroll_after REAL NOT NULL,
                cycle_losses_after REAL NOT NULL,
                processed_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS strategy15_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT NOT NULL UNIQUE,
                message TEXT NOT NULL,
                sent_at REAL
            );
        """)

        con.execute("""
            INSERT OR IGNORE INTO strategy15_state (
                id, initial_bankroll, bankroll, active,
                start_game_id, peak_bankroll, min_bankroll
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
    source = sqlite3.connect(path, timeout=15)
    try:
        return source.execute("""
            SELECT game_id, rate
            FROM crashes
            ORDER BY game_id DESC
            LIMIT 1
        """).fetchone()
    finally:
        source.close()


def add_notification(con, event_key, message):
    con.execute("""
        INSERT OR IGNORE INTO strategy15_notifications
            (event_key, message)
        VALUES (?, ?)
    """, (str(event_key), str(message)))


def required_stake(cycle_losses):
    """Stake needed to recover cycle losses and target 1,000 net profit."""
    if cycle_losses <= 0:
        return BASE_STAKE

    return math.ceil(
        (float(cycle_losses) + TARGET_CYCLE_PROFIT) / (TARGET - 1)
    )


def balance_snapshot(bankroll, active, cycle_losses):
    bankroll = float(bankroll)

    if not active:
        return 0.0, bankroll

    next_stake = float(required_stake(cycle_losses))

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
    rows = con.execute("""
        SELECT outcome
        FROM strategy15_games
        ORDER BY game_id ASC
    """).fetchall()

    wins = sum(outcome == "win" for (outcome,) in rows)
    losses = sum(outcome == "loss" for (outcome,) in rows)
    total = wins + losses
    win_rate = (100.0 * wins / total) if total else 0.0

    current_wins = current_losses = 0
    max_wins = max_losses = 0

    for (outcome,) in rows:
        if outcome == "win":
            current_wins += 1
            current_losses = 0
            max_wins = max(max_wins, current_wins)
        elif outcome == "loss":
            current_losses += 1
            current_wins = 0
            max_losses = max(max_losses, current_losses)

    return "\n".join([
        "📚 سابقه شرط‌بندی مجازی استراتژی 1.5",
        f"🎯 تعداد شرط‌ها: {total:,}",
        f"✅ برد: {wins:,}",
        f"❌ باخت: {losses:,}",
        f"📊 درصد برد: {win_rate:.2f}%",
        f"🔥 بیشترین برد متوالی: {max_wins}",
        f"💔 بیشترین باخت متوالی: {max_losses}",
        f"🔥 برد متوالی فعلی: {current_wins}",
        f"💔 باخت متوالی فعلی: {current_losses}",
    ])


def start_or_status(crash_db=None):
    latest = get_latest_game(crash_db)

    if latest is None:
        return "❌ دیتابیس بازی‌ها خالی است؛ استراتژی 1.5 شروع نشد."

    latest_id = int(latest[0])
    con = connect()

    try:
        init_db(con)
        con.execute("BEGIN IMMEDIATE")

        state = con.execute("""
            SELECT started_at, active, stopped_reason,
                   start_game_id, bankroll, cycle_losses
            FROM strategy15_state
            WHERE id = 1
        """).fetchone()

        started_at, active, stopped_reason, start_id, bankroll, cycle_losses = state

        if started_at is None:
            con.execute("""
                UPDATE strategy15_state
                SET active = 1,
                    started_at = ?,
                    start_game_id = ?,
                    stopped_reason = NULL,
                    stopped_at = NULL
                WHERE id = 1
            """, (time.time(), latest_id))

            add_notification(
                con,
                "started",
                "🧪 استراتژی مجازی 1.5 شروع شد.\n\n"
                f"💰 سرمایه اولیه: {INITIAL_BANKROLL:,.0f} تومان\n"
                f"{balance_lines(INITIAL_BANKROLL, True, 0)}\n"
                f"📍 آخرین بازی هنگام شروع: {latest_id}\n"
                f"▶️ فقط سیگنال‌های قانون A برای بازی‌های بعد از {latest_id} بررسی می‌شوند.\n"
                "شرط‌بندی کاملاً مجازی است."
            )
            con.commit()

            return (
                "🧪 استراتژی 1.5 شروع شد.\n\n"
                f"💰 سرمایه اولیه: {INITIAL_BANKROLL:,.0f} تومان\n"
                f"{balance_lines(INITIAL_BANKROLL, True, 0)}\n"
                f"🎯 ضریب هدف: {TARGET:.2f}x\n"
                f"💵 شرط پایه: {BASE_STAKE:,} تومان\n"
                f"📈 سود هدف چرخه: {TARGET_CYCLE_PROFIT:,} تومان\n"
                f"📍 آخرین بازی هنگام شروع: {latest_id}\n\n"
                "فقط سیگنال‌های واقعی و ارسال‌شده قانون A "
                "برای بازی‌های جدید استفاده می‌شوند.\n"
                "داده‌های تاریخی دوباره شبیه‌سازی نمی‌شوند."
            )

        con.commit()

        if active:
            status = "در حال اجرا"
        elif stopped_reason:
            status = f"متوقف‌شده: {stopped_reason}"
        else:
            status = "غیرفعال"

        return (
            "🧪 وضعیت استراتژی 1.5\n\n"
            f"🚦 وضعیت: {status}\n"
            f"{balance_lines(bankroll, bool(active), cycle_losses)}\n"
            f"{history_lines(con)}\n\n"
            f"📍 شناسه مبنای شروع: {int(start_id)}\n"
            "فشردن دوباره دکمه، موجودی یا سابقه را ریست نمی‌کند."
        )
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def process_tick(crash_db=None):
    """
    Settle only Rule A signals that were confirmed sent and resolved.
    Each target game is processed at most once.
    """
    path = crash_db or CRASH_DB
    con = connect()

    try:
        init_db(con)
        con.execute("BEGIN IMMEDIATE")

        state = con.execute("""
            SELECT active, bankroll, start_game_id, cycle_losses,
                   wins, losses, current_loss_streak, max_loss_streak,
                   max_stake, peak_bankroll, max_drawdown,
                   max_drawdown_pct, min_bankroll, stopped_reason
            FROM strategy15_state
            WHERE id = 1
        """).fetchone()

        (
            active, bankroll, start_id, cycle_losses,
            wins, losses, current_streak, max_streak,
            max_stake, peak, max_dd, max_dd_pct, min_bankroll,
            stopped_reason,
        ) = state

        if not active:
            con.commit()
            return

        # The signal DB is read-only here. Rule A remains responsible
        # for generating signals and tracking Telegram delivery.
        # Read crash results and Rule A signals through one SQLite
        # connection, explicitly attaching the independent signal DB.
        source = sqlite3.connect(path, timeout=15)
        try:
            source.execute(
                "ATTACH DATABASE ? AS signal_store",
                (rule_a.SIGNALS_DB,)
            )
            signals = source.execute("""
                SELECT s.target_game_id, c.rate
                FROM signal_store.rule_b_signals AS s
                JOIN main.crashes AS c
                  ON c.game_id = s.target_game_id
                WHERE s.target_game_id > ?
                  AND s.signal_sent_at IS NOT NULL
                  AND s.status IN ('win', 'loss')
                  AND s.target_rate IS NOT NULL
                ORDER BY s.target_game_id ASC
            """, (int(start_id),)).fetchall()
        finally:
            source.close()

        for game_id, rate in signals:
            game_id = int(game_id)
            rate = float(rate)

            already = con.execute("""
                SELECT 1 FROM strategy15_games WHERE game_id = ?
            """, (game_id,)).fetchone()

            if already:
                continue

            if not math.isfinite(rate) or rate <= 0:
                continue

            stake = float(required_stake(cycle_losses))

            # Preserve a strictly positive bankroll.
            if bankroll <= 0 or stake >= bankroll - EPSILON:
                reason = (
                    f"شرط لازم {stake:,.2f} تومان است؛ "
                    f"موجودی {bankroll:,.2f} تومان است."
                )

                con.execute("""
                    UPDATE strategy15_state
                    SET active = 0,
                        stopped_reason = ?,
                        stopped_at = ?
                    WHERE id = 1 AND active = 1
                """, ("استراتژی متوقف شد: " + reason, time.time()))

                add_notification(
                    con,
                    f"strategy15_failed:{game_id}",
                    "🛑 استراتژی 1.5 متوقف شد.\n\n"
                    f"🎮 بازی هدف بررسی‌نشده: {game_id}\n"
                    f"{balance_lines(bankroll, True, cycle_losses)}\n"
                    f"💵 شرط لازم: {stake:,.2f} تومان\n"
                    "⛔ برای این بازی شرط مجازی ثبت نشد.\n"
                    f"📌 علت: {reason}\n"
                    "🚫 استراتژی خودکار ادامه نمی‌یابد."
                )
                active = 0
                stopped_reason = reason
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
                INSERT INTO strategy15_games (
                    game_id, rate, stake, outcome, profit,
                    bankroll_after, cycle_losses_after, processed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                game_id, rate, stake, outcome, profit,
                bankroll, cycle_losses, time.time()
            ))

            con.execute("""
                UPDATE strategy15_state
                SET bankroll = ?,
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
                bankroll, cycle_losses, wins, losses,
                current_streak, max_streak, max_stake,
                peak, max_dd, max_dd_pct, min_bankroll
            ))

            add_notification(
                con,
                f"game:{game_id}",
                f"{'✅ برد' if outcome == 'win' else '❌ باخت'} | "
                "استراتژی مجازی 1.5\n\n"
                f"🎮 بازی هدف قانون A: {game_id}\n"
                f"📊 ضریب ثبت‌شده: {rate:.2f}x\n"
                f"💵 شرط مجازی: {stake:,.2f} تومان\n"
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
            FROM strategy15_notifications
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
            UPDATE strategy15_notifications
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
                   start_game_id, cycle_losses, wins, losses,
                   current_loss_streak, max_loss_streak, max_stake,
                   peak_bankroll, max_drawdown, max_drawdown_pct,
                   min_bankroll, stopped_reason
            FROM strategy15_state
            WHERE id = 1
        """).fetchone()

        (
            initial, bankroll, active, started_at, start_id,
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
            "start_id": int(start_id),
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
