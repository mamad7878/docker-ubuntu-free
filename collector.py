import os
import json
import sqlite3
import requests
from datetime import datetime, timezone

API_URL = "https://playglobal8.com/api/game/bet/multi/history"
DB_FILE = "crash.db"

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fa",
    "Content-Type": "application/json",
    "Origin": "https://playglobal8.com",
    "Referer": "https://playglobal8.com/fa/game/crash",
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 10; K) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Mobile Safari/537.36"
    ),
}


def init_db():
    conn = sqlite3.connect(DB_FILE)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS crashes (
            game_id INTEGER PRIMARY KEY,
            rate REAL NOT NULL,
            hash TEXT,
            salt TEXT,
            begin_time INTEGER,
            end_time INTEGER,
            prepare_time INTEGER,
            collected_at TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_crashes_game_id
        ON crashes(game_id)
    """)

    conn.commit()
    return conn


def fetch_history():
    cookie = os.environ.get("CRASH_COOKIE", "").strip()

    if not cookie:
        raise RuntimeError(
            "CRASH_COOKIE تنظیم نشده است.\n"
            "اجرا کن:\n"
            "export CRASH_COOKIE='YOUR_COOKIE'"
        )

    headers = HEADERS.copy()
    headers["Cookie"] = cookie

    payload = {
        "gameUrl": "crash",
        "page": 1,
        "pageSize": 100
    }

    response = requests.post(
        API_URL,
        headers=headers,
        json=payload,
        timeout=20
    )

    print("HTTP STATUS:", response.status_code)

    response.raise_for_status()

    result = response.json()

    if result.get("code") != 0:
        raise RuntimeError(
            f"API error: {result.get('msg')}"
        )

    return result


def save_records(conn, result):
    data = result.get("data") or {}
    items = data.get("list") or []

    inserted = 0
    skipped = 0

    now = datetime.now(timezone.utc).isoformat()

    for item in items:
        game_id = item.get("gameId")
        raw_detail = item.get("gameDetail")

        if game_id is None or not raw_detail:
            continue

        try:
            detail = json.loads(raw_detail)
        except json.JSONDecodeError:
            print(f"BAD JSON gameId={game_id}")
            continue

        rate_raw = detail.get("rate")

        if rate_raw is None:
            print(f"NO RATE gameId={game_id}")
            continue

        try:
            rate = float(rate_raw)
        except (ValueError, TypeError):
            print(f"BAD RATE gameId={game_id}: {rate_raw}")
            continue

        cursor = conn.execute("""
            INSERT OR IGNORE INTO crashes (
                game_id,
                rate,
                hash,
                salt,
                begin_time,
                end_time,
                prepare_time,
                collected_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            int(game_id),
            rate,
            detail.get("hash"),
            detail.get("salt"),
            detail.get("beginTime"),
            detail.get("endTime"),
            detail.get("prepareTime"),
            now
        ))

        if cursor.rowcount == 1:
            inserted += 1
        else:
            skipped += 1

    conn.commit()

    return len(items), inserted, skipped


def show_latest(conn, limit=20):
    rows = conn.execute("""
        SELECT game_id, rate
        FROM crashes
        ORDER BY game_id DESC
        LIMIT ?
    """, (limit,)).fetchall()

    print()
    print("=" * 45)
    print("LATEST CRASHES")
    print("=" * 45)

    for game_id, rate in rows:
        print(f"{game_id}  ->  {rate:.2f}x")


def main():
    print("=" * 60)
    print("CRASH COLLECTOR")
    print("=" * 60)

    conn = init_db()

    try:
        result = fetch_history()

        data = result.get("data") or {}

        print("API PAGE:", data.get("page"))
        print("API PAGE SIZE:", data.get("pageSize"))
        print("API TOTAL:", data.get("total"))

        total, inserted, skipped = save_records(conn, result)

        print()
        print("RECEIVED:", total)
        print("INSERTED:", inserted)
        print("DUPLICATES:", skipped)

        show_latest(conn)

        db_count = conn.execute(
            "SELECT COUNT(*) FROM crashes"
        ).fetchone()[0]

        print()
        print("TOTAL IN DATABASE:", db_count)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
