#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Virtual-only Crash betting simulator.
No real bets, external betting API, or money transfers.
"""

import os
import sqlite3
import time

CRASH_DB = os.environ.get("CRASH_DB_PATH", "/data/crash.db")
PAPER_DB = os.environ.get(
    "PAPER_BETTING_DB_PATH",
    "/data/paper_betting.db"
)

INITIAL_BANKROLL = 1_000_000
STAKE_PERCENT = 0.02
TARGET = 2.00
MIN_PROBABILITY = 60.0


def q2(value):
    return round(float(value), 2)


def connect():
    folder = os.path.dirname(PAPER_DB)
    if folder:
        os.makedirs(folder, exist_ok=True)

    con = sqlite3.connect(PAPER_DB, timeout=15)
    con.execute("PRAGMA busy_timeout=15000")
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db(con=None):
    own = con is None
    if own:
        con = connect()

    try:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS paper_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                initial_bankroll INTEGER NOT NULL,
                bankroll INTEGER NOT NULL,
                stake INTEGER NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS paper_bets (
                target_game_id INTEGER PRIMARY KEY,
                trigger_game_id INTEGER NOT NULL,
                trigger_rate REAL NOT NULL,
                probability REAL NOT NULL,
                stake INTEGER NOT NULL,
                status TEXT NOT NULL,
                result_rate REAL,
                profit INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                settled_at REAL
            );

            CREATE TABLE IF NOT EXISTS paper_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT NOT NULL UNIQUE,
                message TEXT NOT NULL,
                sent_at REAL
            );
        """)

        con.execute("""
            INSERT OR IGNORE INTO paper_state
                (id, initial_bankroll, bankroll, stake, created_at)
            VALUES (1, ?, ?, ?, ?)
        """, (
            INITIAL_BANKROLL,
            INITIAL_BANKROLL,
            int(INITIAL_BANKROLL * STAKE_PERCENT),
            time.time()
        ))
        con.commit()
    finally:
        if own:
            con.close()


def add_notification(con, event_key, message):
    con.execute("""
        INSERT OR IGNORE INTO paper_notifications
            (event_key, message)
        VALUES (?, ?)
    """, (str(event_key), str(message)))


def read_crash_rows(crash_db):
    con = sqlite3.connect(crash_db, timeout=15)
    try:
        return con.execute("""
            SELECT game_id, rate
            FROM crashes
            ORDER BY game_id ASC
        """).fetchall()
    finally:
        con.close()


def historical_probability(rows):
    """
    Mirrors the 🔍 تحلیل calculation:
    find earlier occurrences of the latest multiplier rounded to 2 decimals,
    then evaluate the immediately following recorded game.
    """
    if not rows:
        return None

    latest_id, latest_rate = rows[-1]
    reference = q2(latest_rate)

    following = []

    for i in range(len(rows) - 2, -1, -1):
        if q2(rows[i][1]) == reference:
            following.append(q2(rows[i + 1][1]))

    if not following:
        return None

    successes = sum(rate >= TARGET for rate in following)
    return 100.0 * successes / len(following)


def process_tick(crash_db=None):
    """
    Called by the bot's live monitor.
    Settles any due bet once, then evaluates the latest game to decide
    whether to place one virtual bet on the next game.
    """
    crash_db = crash_db or CRASH_DB
    rows = read_crash_rows(crash_db)

    if not rows:
        return

    latest_id = int(rows[-1][0])
    # نتایج بازی‌ها از دیتابیس اصلی خوانده شده‌اند؛
    # برای تسویه نباید جدول crashes را از دیتابیس شبیه‌ساز بخوانیم.
    rates_by_id = {int(game_id): float(rate) for game_id, rate in rows}
    con = connect()

    try:
        init_db(con)
        con.execute("BEGIN IMMEDIATE")

        # Settle due bets. Each target game can only be processed once.
        pending = con.execute("""
            SELECT target_game_id, stake
            FROM paper_bets
            WHERE status = 'pending'
              AND target_game_id <= ?
            ORDER BY target_game_id
        """, (latest_id,)).fetchall()

        for target_id, stake in pending:
            result_rate = rates_by_id.get(int(target_id))

            if result_rate is None:
                # The target ID was passed without a corresponding record.
                # Void it and return the reserved virtual stake.
                con.execute("""
                    UPDATE paper_state
                    SET bankroll = bankroll + ?
                    WHERE id = 1
                """, (stake,))

                con.execute("""
                    UPDATE paper_bets
                    SET status = 'void',
                        profit = 0,
                        settled_at = ?
                    WHERE target_game_id = ?
                      AND status = 'pending'
                """, (time.time(), target_id))

                state = con.execute("""
                    SELECT bankroll FROM paper_state WHERE id = 1
                """).fetchone()

                add_notification(
                    con,
                    f"result:{target_id}",
                    "⚠️ شبیه‌ساز مجازی: شرط لغو شد\n"
                    f"🎯 بازی هدف: {target_id}\n"
                    "رکورد دقیق بازی هدف پیدا نشد؛ مبلغ مجازی برگشت داده شد.\n"
                    f"💰 موجودی آزاد: {state[0]:,} تومان"
                )
                continue

            rate = float(result_rate)
            won = q2(rate) >= TARGET

            if won:
                # Stake was deducted when the bet was placed.
                # Return stake + winnings (2x payout).
                con.execute("""
                    UPDATE paper_state
                    SET bankroll = bankroll + ?
                    WHERE id = 1
                """, (2 * stake,))
                status = "win"
                profit = stake
            else:
                status = "loss"
                profit = -stake

            con.execute("""
                UPDATE paper_bets
                SET status = ?,
                    result_rate = ?,
                    profit = ?,
                    settled_at = ?
                WHERE target_game_id = ?
                  AND status = 'pending'
            """, (status, rate, profit, time.time(), target_id))

            state = con.execute("""
                SELECT bankroll FROM paper_state WHERE id = 1
            """).fetchone()

            symbol = "✅ برد" if won else "❌ باخت"

            add_notification(
                con,
                f"result:{target_id}",
                f"{symbol} | نتیجه شبیه‌ساز مجازی\n\n"
                f"🎯 بازی: {target_id}\n"
                f"📊 ضریب واقعی: {rate:.2f}x\n"
                f"💵 مبلغ مجازی: {stake:,} تومان\n"
                f"📈 سود/زیان این شرط: {profit:+,} تومان\n"
                f"💰 موجودی آزاد: {state[0]:,} تومان"
            )

        # Do not place another bet on a target already known to the ledger.
        target_id = latest_id + 1
        already_exists = con.execute("""
            SELECT 1 FROM paper_bets WHERE target_game_id = ?
        """, (target_id,)).fetchone()

        if not already_exists:
            probability = historical_probability(rows)

            if probability is not None and probability >= MIN_PROBABILITY:
                state = con.execute("""
                    SELECT bankroll, stake
                    FROM paper_state
                    WHERE id = 1
                """).fetchone()

                bankroll = int(state[0])
                # مبلغ هر شرط برابر ۲ درصد موجودی آزاد فعلی است.
                stake = int(bankroll * STAKE_PERCENT)

                if stake > 0 and bankroll >= stake:
                    con.execute("""
                        INSERT INTO paper_bets (
                            target_game_id, trigger_game_id, trigger_rate,
                            probability, stake, status, created_at
                        ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
                    """, (
                        target_id,
                        latest_id,
                        float(rows[-1][1]),
                        probability,
                        stake,
                        time.time()
                    ))

                    con.execute("""
                        UPDATE paper_state
                        SET bankroll = bankroll - ?,
                            stake = ?
                        WHERE id = 1
                    """, (stake, stake))

                    new_balance = bankroll - stake

                    add_notification(
                        con,
                        f"signal:{target_id}",
                        "🧪 شرط مجازی جدید ثبت شد\n\n"
                        f"🔎 بازی تحلیل‌شده: {latest_id}\n"
                        f"🎯 بازی هدف: {target_id}\n"
                        f"📊 احتمال تاریخی رسیدن به {TARGET:.2f}x یا بیشتر: "
                        f"{probability:.2f}%\n"
                        f"💵 مبلغ مجازی: {stake:,} تومان\n"
                        f"💰 موجودی آزاد پس از رزرو شرط: "
                        f"{new_balance:,} تومان\n\n"
                        "شرط فقط مجازی است؛ درصد تاریخی تضمین نتیجه نیست."
                    )

                else:
                    add_notification(
                        con,
                        f"insufficient:{target_id}",
                        "⚠️ شبیه‌ساز مجازی: موجودی آزاد برای شرط جدید کافی نیست."
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
            FROM paper_notifications
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
            UPDATE paper_notifications
            SET sent_at = ?
            WHERE id = ? AND sent_at IS NULL
        """, (time.time(), notification_id))
        con.commit()
    finally:
        con.close()


def get_stats():
    con = connect()
    try:
        init_db(con)

        state = con.execute("""
            SELECT initial_bankroll, bankroll, stake
            FROM paper_state
            WHERE id = 1
        """).fetchone()

        counts = dict(con.execute("""
            SELECT status, COUNT(*)
            FROM paper_bets
            GROUP BY status
        """).fetchall())

        total_profit = con.execute("""
            SELECT COALESCE(SUM(profit), 0)
            FROM paper_bets
            WHERE status IN ('win', 'loss')
        """).fetchone()[0]

        initial, bankroll, stake = map(int, state)
        wins = int(counts.get("win", 0))
        losses = int(counts.get("loss", 0))
        pending = int(counts.get("pending", 0))
        void = int(counts.get("void", 0))
        decided = wins + losses
        win_rate = (100.0 * wins / decided) if decided else 0.0

        return {
            "initial": initial,
            "bankroll": bankroll,
            "stake": stake,
            "wins": wins,
            "losses": losses,
            "pending": pending,
            "void": void,
            "decided": decided,
            "win_rate": win_rate,
            "profit": int(total_profit),
        }
    finally:
        con.close()
