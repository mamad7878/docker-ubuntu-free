import os
import json
import sqlite3
from datetime import datetime, timezone

CRASH_DB = os.environ.get(
    "CRASH_DB_PATH", "/data/crash.db"
)

SIGNALS_DB = os.environ.get(
    "RULE_B_SIGNALS_DB_PATH",
    os.path.join(os.path.dirname(CRASH_DB), "rule_b_signals.db")
)

LOOKBACK = 5
PREVIOUS_LIMIT = 2.0
TARGET_RATE = 1.5


def now():
    return datetime.now(timezone.utc).isoformat()



def connect_signals(path=None):
    path = path or SIGNALS_DB
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS rule_b_signals (
            target_game_id INTEGER PRIMARY KEY,
            trigger_game_id INTEGER NOT NULL,
            previous_rates TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            target_rate REAL,
            created_at TEXT NOT NULL,
            resolved_at TEXT,
            signal_sent_at TEXT,
            result_sent_at TEXT,
            delivery_status TEXT NOT NULL DEFAULT 'pending',
            delivery_attempted_at TEXT
        )
    """)

    columns = {
        row[1] for row in conn.execute(
            "PRAGMA table_info(rule_b_signals)"
        )
    }

    if "delivery_status" not in columns:
        conn.execute("""
            ALTER TABLE rule_b_signals
            ADD COLUMN delivery_status TEXT NOT NULL DEFAULT 'pending'
        """)

    if "delivery_attempted_at" not in columns:
        conn.execute("""
            ALTER TABLE rule_b_signals
            ADD COLUMN delivery_attempted_at TEXT
        """)

    # Preserve settled historical records conservatively.
    conn.execute("""
        UPDATE rule_b_signals
        SET delivery_status = 'unknown'
        WHERE delivery_status = 'pending'
          AND status IN ('win', 'loss', 'void')
          AND signal_sent_at IS NOT NULL
    """)

    conn.execute("""
        UPDATE rule_b_signals
        SET delivery_status = 'missed'
        WHERE delivery_status = 'pending'
          AND status = 'missed'
    """)

    # An old timestamp on an unresolved record does not prove delivery.
    conn.execute("""
        UPDATE rule_b_signals
        SET delivery_status = 'unknown'
        WHERE delivery_status = 'pending'
          AND status = 'pending'
          AND signal_sent_at IS NOT NULL
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_rule_b_status
        ON rule_b_signals(status)
    """)

    conn.commit()
    return conn

def get_previous_five(crash_db, trigger_id):
    conn = sqlite3.connect(crash_db, timeout=10)

    try:
        rows = conn.execute("""
            SELECT game_id, rate
            FROM crashes
            WHERE game_id <= ?
            ORDER BY game_id DESC
            LIMIT 5
        """, (trigger_id,)).fetchall()
    finally:
        conn.close()

    rows.reverse()

    if len(rows) != LOOKBACK:
        return []

    ids = [int(row[0]) for row in rows]

    # فقط بازی‌های متوالی معتبرند؛ شکاف باعث سیگنال نمی‌شود.
    if ids[-1] != int(trigger_id):
        return []

    if any(
        ids[i] + 1 != ids[i + 1]
        for i in range(len(ids) - 1)
    ):
        return []

    return [(game_id, float(rate)) for game_id, rate in rows]


def create_signal(trigger_id, crash_db=None, signals_db=None):
    crash_db = crash_db or CRASH_DB
    signals_db = signals_db or SIGNALS_DB
    target_id = int(trigger_id) + 1

    previous = get_previous_five(crash_db, trigger_id)

    if len(previous) != LOOKBACK:
        return None

    if not all(rate < PREVIOUS_LIMIT for _, rate in previous):
        return None

    # اگر نتیجه بازی هدف از قبل در دیتابیس باشد،
    # دیگر سیگنال گذشته‌نگر ایجاد نمی‌کنیم.
    conn = sqlite3.connect(crash_db, timeout=10)

    try:
        exists = conn.execute(
            "SELECT 1 FROM crashes WHERE game_id = ?",
            (target_id,)
        ).fetchone()
    finally:
        conn.close()

    if exists:
        return None

    rates = [rate for _, rate in previous]

    conn = connect_signals(signals_db)

    try:
        cur = conn.execute("""
            INSERT OR IGNORE INTO rule_b_signals (
                target_game_id,
                trigger_game_id,
                previous_rates,
                status,
                created_at
            )
            VALUES (?, ?, ?, 'pending', ?)
        """, (
            target_id,
            int(trigger_id),
            json.dumps(rates),
            now()
        ))

        conn.commit()

        if cur.rowcount != 1:
            return None

        return {
            "target_game_id": target_id,
            "trigger_game_id": int(trigger_id),
            "previous_rates": rates,
            "status": "pending"
        }
    finally:
        conn.close()



def reconcile(crash_db=None, signals_db=None):
    """Resolve outcomes without treating uncertain delivery as a sent signal."""
    crash_db = crash_db or CRASH_DB
    signals_db = signals_db or SIGNALS_DB

    source = sqlite3.connect(crash_db, timeout=10)
    try:
        latest = source.execute(
            "SELECT MAX(game_id) FROM crashes"
        ).fetchone()[0]

        if latest is None:
            return

        conn = connect_signals(signals_db)
        try:
            pending = conn.execute("""
                SELECT target_game_id, signal_sent_at,
                       delivery_status, delivery_attempted_at
                FROM rule_b_signals
                WHERE status = 'pending'
            """).fetchall()

            for target_id, sent_at, delivery, attempted_at in pending:
                # Give an in-flight Telegram request up to 120 seconds.
                if delivery == "attempting":
                    stale = True
                    if attempted_at:
                        try:
                            started = datetime.fromisoformat(attempted_at)
                            age = (
                                datetime.now(timezone.utc) - started
                            ).total_seconds()
                            stale = age > 120
                        except (ValueError, TypeError):
                            stale = True

                    if not stale:
                        continue

                    delivery = "unknown"
                    conn.execute("""
                        UPDATE rule_b_signals
                        SET delivery_status = 'unknown'
                        WHERE target_game_id = ?
                          AND status = 'pending'
                    """, (target_id,))

                row = source.execute("""
                    SELECT rate FROM crashes WHERE game_id = ?
                """, (target_id,)).fetchone()

                if row is not None:
                    rate = float(row[0])

                    if delivery == "sent":
                        status = "win" if rate >= TARGET_RATE else "loss"
                    elif delivery == "unknown":
                        status = "delivery_unknown"
                    else:
                        status = "missed"

                    conn.execute("""
                        UPDATE rule_b_signals
                        SET status = ?,
                            target_rate = ?,
                            resolved_at = ?
                        WHERE target_game_id = ?
                          AND status = 'pending'
                    """, (status, rate, now(), target_id))

                elif int(latest) > int(target_id):
                    if delivery == "sent":
                        status = "void"
                    elif delivery == "unknown":
                        status = "delivery_unknown"
                    else:
                        status = "missed"

                    conn.execute("""
                        UPDATE rule_b_signals
                        SET status = ?,
                            resolved_at = ?
                        WHERE target_game_id = ?
                          AND status = 'pending'
                    """, (status, now(), target_id))

            conn.commit()
        finally:
            conn.close()
    finally:
        source.close()

def get_stats(signals_db=None):
    conn = connect_signals(signals_db)

    try:
        rows = conn.execute("""
            SELECT status, COUNT(*)
            FROM rule_b_signals
            GROUP BY status
            ORDER BY status
        """).fetchall()

        return dict(rows)
    finally:
        conn.close()



def get_streak_stats(signals_db=None):
    """Calculate historical and current win/loss streaks from settled signals."""
    conn = connect_signals(signals_db)
    try:
        rows = conn.execute("""
            SELECT target_game_id, status
            FROM rule_b_signals
            WHERE status IN ('win', 'loss')
            ORDER BY target_game_id
        """).fetchall()
    finally:
        conn.close()

    max_win = max_loss = 0
    current_win = current_loss = 0

    for _, status in rows:
        if status == "win":
            current_win += 1
            current_loss = 0
            max_win = max(max_win, current_win)
        else:
            current_loss += 1
            current_win = 0
            max_loss = max(max_loss, current_loss)

    return {
        "max_win_streak": max_win,
        "max_loss_streak": max_loss,
        "current_win_streak": current_win,
        "current_loss_streak": current_loss,
    }



def get_unsent_signals(signals_db=None):
    conn = connect_signals(signals_db)
    try:
        return conn.execute("""
            SELECT target_game_id, trigger_game_id, previous_rates
            FROM rule_b_signals
            WHERE status = 'pending'
              AND delivery_status = 'pending'
              AND signal_sent_at IS NULL
            ORDER BY target_game_id
        """).fetchall()
    finally:
        conn.close()

def target_exists(target_id, crash_db=None):
    """Check whether the target result has already entered the crash DB."""
    crash_db = crash_db or CRASH_DB
    conn = sqlite3.connect(crash_db, timeout=10)
    try:
        return conn.execute(
            "SELECT 1 FROM crashes WHERE game_id = ?",
            (int(target_id),)
        ).fetchone() is not None
    finally:
        conn.close()



def mark_signal_sent(target_id, signals_db=None):
    """Record delivery only after Telegram confirms a successful send."""
    conn = connect_signals(signals_db)
    try:
        cur = conn.execute("""
            UPDATE rule_b_signals
            SET signal_sent_at = ?,
                delivery_status = 'sent'
            WHERE target_game_id = ?
              AND status = 'pending'
              AND delivery_status = 'attempting'
              AND signal_sent_at IS NULL
        """, (now(), int(target_id)))
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def unmark_signal_sent(target_id, signals_db=None):
    """Reset only a signal that has not been sent or attempted."""
    conn = connect_signals(signals_db)
    try:
        conn.execute("""
            UPDATE rule_b_signals
            SET signal_sent_at = NULL,
                delivery_status = 'pending',
                delivery_attempted_at = NULL
            WHERE target_game_id = ?
              AND status = 'pending'
              AND delivery_status = 'pending'
        """, (int(target_id),))
        conn.commit()
    finally:
        conn.close()


def mark_signal_attempting(target_id, signals_db=None):
    conn = connect_signals(signals_db)
    try:
        cur = conn.execute("""
            UPDATE rule_b_signals
            SET delivery_status = 'attempting',
                delivery_attempted_at = ?
            WHERE target_game_id = ?
              AND status = 'pending'
              AND delivery_status = 'pending'
              AND signal_sent_at IS NULL
        """, (now(), int(target_id)))
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def mark_signal_unknown(target_id, signals_db=None):
    """Do not automatically retry an ambiguous Telegram delivery."""
    conn = connect_signals(signals_db)
    try:
        cur = conn.execute("""
            UPDATE rule_b_signals
            SET delivery_status = 'unknown'
            WHERE target_game_id = ?
              AND status = 'pending'
              AND delivery_status = 'attempting'
              AND signal_sent_at IS NULL
        """, (int(target_id),))
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()

def get_unsent_results(signals_db=None):
    conn = connect_signals(signals_db)

    try:
        return conn.execute("""
            SELECT target_game_id, status, target_rate
            FROM rule_b_signals
            WHERE status IN ('win', 'loss', 'void')
              AND signal_sent_at IS NOT NULL
              AND delivery_status = 'sent'
              AND result_sent_at IS NULL
            ORDER BY target_game_id
        """).fetchall()
    finally:
        conn.close()

def mark_result_sent(target_id, signals_db=None):
    conn = connect_signals(signals_db)

    try:
        conn.execute("""
            UPDATE rule_b_signals
            SET result_sent_at = ?
            WHERE target_game_id = ?
              AND result_sent_at IS NULL
        """, (now(), int(target_id)))
        conn.commit()
    finally:
        conn.close()
