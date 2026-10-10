import os
import json
import sqlite3
import time
import requests
from datetime import datetime, timezone

API_URL = "https://playglobal8.com/api/game/bet/multi/history"

BASE = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.environ.get("CRASH_DB_PATH", "/data/crash.db")
RECOVERY_FILE = os.path.join(BASE, "recovery_report.json")

POLL_SECONDS = float(os.getenv("POLL_SECONDS", "1"))
RETRY_SECONDS = float(os.getenv("RETRY_SECONDS", "5"))
MAX_RECOVERY_PAGES = int(os.getenv("MAX_RECOVERY_PAGES", "10"))

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


def now_iso():
    return datetime.now(timezone.utc).isoformat()


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
        CREATE TABLE IF NOT EXISTS missing_games (
            game_id INTEGER PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'missing',
            detected_at TEXT NOT NULL,
            resolved_at TEXT,
            source TEXT
        )
    """)

    conn.commit()
    return conn


def fetch_history(page=1, page_size=100):
    cookie = os.environ.get("CRASH_COOKIE", "").strip()

    headers = HEADERS.copy()

    # Cookie is optional.
    # Playglobal Crash API currently responds without it.
    if cookie:
        headers["Cookie"] = cookie

    payload = {
        "gameUrl": "crash",
        "page": page,
        "pageSize": page_size
    }

    response = requests.post(
        API_URL,
        headers=headers,
        json=payload,
        timeout=20
    )

    response.raise_for_status()

    result = response.json()

    if result.get("code") != 0:
        raise RuntimeError(
            f"API error: {result.get('msg')}"
        )

    return result


def extract_records(result):
    data = result.get("data") or {}
    items = data.get("list") or []

    records = []

    for item in items:
        game_id = item.get("gameId")
        raw_detail = item.get("gameDetail")

        if game_id is None or not raw_detail:
            continue

        try:
            detail = json.loads(raw_detail)
            rate = float(detail["rate"])
        except (
            ValueError,
            TypeError,
            KeyError,
            json.JSONDecodeError
        ):
            continue

        records.append({
            "game_id": int(game_id),
            "rate": rate,
            "hash": detail.get("hash"),
            "salt": detail.get("salt"),
            "begin_time": detail.get("beginTime"),
            "end_time": detail.get("endTime"),
            "prepare_time": detail.get("prepareTime"),
        })

    return records


def save_records(conn, records, source="api"):
    inserted = []

    for r in records:
        cur = conn.execute("""
            INSERT OR IGNORE INTO crashes
            (
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
            r["game_id"],
            r["rate"],
            r["hash"],
            r["salt"],
            r["begin_time"],
            r["end_time"],
            r["prepare_time"],
            now_iso()
        ))

        if cur.rowcount == 1:
            inserted.append(
                (r["game_id"], r["rate"])
            )

        # اگر قبلاً missing بوده، حل شده است.
        conn.execute("""
            UPDATE missing_games
            SET status='recovered',
                resolved_at=?,
                source=?
            WHERE game_id=?
        """, (
            now_iso(),
            source,
            r["game_id"]
        ))

    conn.commit()
    return inserted


def latest_id(conn):
    row = conn.execute("""
        SELECT MAX(game_id)
        FROM crashes
    """).fetchone()

    return row[0] if row and row[0] is not None else None


def detect_gaps(conn):
    row = conn.execute("""
        SELECT MIN(game_id), MAX(game_id)
        FROM crashes
    """).fetchone()

    if not row or row[0] is None or row[1] is None:
        return []

    minimum, maximum = row

    existing = {
        x[0]
        for x in conn.execute("""
            SELECT game_id
            FROM crashes
            WHERE game_id BETWEEN ? AND ?
        """, (minimum, maximum))
    }

    missing = []

    for game_id in range(minimum, maximum + 1):
        if game_id not in existing:
            missing.append(game_id)

    for game_id in missing:
        conn.execute("""
            INSERT OR IGNORE INTO missing_games
            (
                game_id,
                status,
                detected_at
            )
            VALUES (?, 'missing', ?)
        """, (game_id, now_iso()))

    conn.commit()

    return missing


def get_unresolved_missing(conn):
    return [
        row[0]
        for row in conn.execute("""
            SELECT game_id
            FROM missing_games
            WHERE status='missing'
            ORDER BY game_id ASC
        """).fetchall()
    ]


def recover_missing(conn):
    missing = get_unresolved_missing(conn)

    if not missing:
        return [], []

    wanted = set(missing)
    recovered = []

    print(
        f"🔎 Attempting recovery for "
        f"{len(missing)} missing IDs..."
    )

    for page in range(1, MAX_RECOVERY_PAGES + 1):

        try:
            result = fetch_history(
                page=page,
                page_size=100
            )

            records = extract_records(result)

            if not records:
                break

            found = [
                r for r in records
                if r["game_id"] in wanted
            ]

            if found:
                for r in found:
                    save_records(
                        conn,
                        [r],
                        source=f"api_page_{page}"
                    )
                    recovered.append(
                        (r["game_id"], r["rate"])
                    )
                    wanted.discard(r["game_id"])

                print(
                    f"🔄 Page {page}: "
                    f"recovered {len(found)}"
                )

            if not wanted:
                break

        except Exception as e:
            print(
                f"⚠️ Recovery page {page} failed: "
                f"{type(e).__name__}: {e}"
            )
            break

    remaining = sorted(wanted)

    return recovered, remaining


def write_recovery_report(
    recovered,
    missing,
    outage_start=None,
    outage_end=None
):
    if not recovered and not missing:
        return

    report = {
        "recovered": [
            {
                "game_id": int(game_id),
                "rate": float(rate)
            }
            for game_id, rate in recovered
        ],
        "missing": [int(x) for x in missing],
        "outage_start": outage_start,
        "recovery_time": outage_end,
        "duration_seconds": None,
        "latest_id_after_recovery": None,
    }

    if outage_start and outage_end:
        try:
            a = datetime.fromisoformat(
                outage_start
            )
            b = datetime.fromisoformat(
                outage_end
            )
            report["duration_seconds"] = (
                b - a
            ).total_seconds()
        except Exception:
            pass

    if recovered:
        report["latest_id_after_recovery"] = max(
            x[0] for x in recovered
        )

    tmp = RECOVERY_FILE + ".tmp"

    with open(
        tmp,
        "w",
        encoding="utf-8"
    ) as f:
        json.dump(
            report,
            f,
            ensure_ascii=False,
            indent=2
        )

    os.replace(
        tmp,
        RECOVERY_FILE
    )


def run():
    conn = init_db()

    print("=" * 55)
    print("LIVE CRASH COLLECTOR + GAP RECOVERY")
    print("=" * 55)
    print(f"Poll interval       : {POLL_SECONDS}s")
    print(f"Retry interval      : {RETRY_SECONDS}s")
    print(f"Recovery pages      : {MAX_RECOVERY_PAGES}")
    print(f"Database            : {DB_FILE}")
    print(f"Recovery report     : {RECOVERY_FILE}")
    print()

    outage_start = None
    was_offline = False

    try:
        while True:

            try:
                result = fetch_history(
                    page=1,
                    page_size=100
                )

                if was_offline:
                    outage_end = now_iso()

                    print()
                    print(
                        "🟢 INTERNET/API CONNECTION RESTORED"
                    )
                    print(
                        f"Recovery time: {outage_end}"
                    )

                new_records = save_records(
                    conn,
                    extract_records(result)
                )

                # کل DB را برای Gap بررسی می‌کنیم.
                gaps = detect_gaps(conn)

                recovered = []
                remaining = []

                if gaps or get_unresolved_missing(conn):
                    recovered, remaining = recover_missing(
                        conn
                    )

                    if recovered or remaining:
                        write_recovery_report(
                            recovered,
                            remaining,
                            outage_start,
                            now_iso()
                        )

                last = latest_id(conn)

                if new_records:
                    for game_id, rate in sorted(
                        new_records
                    ):
                        print(
                            f"NEW  {game_id} -> "
                            f"{rate:.2f}x"
                        )

                    print(
                        f"LAST ID: {last} | "
                        f"NEW: {len(new_records)}"
                    )
                else:
                    print(
                        f"NO NEW | LAST ID: {last}"
                    )

                if remaining:
                    print(
                        "⚠️ UNRECOVERED IDS:",
                        ", ".join(
                            str(x)
                            for x in remaining[:100]
                        )
                    )

                    if len(remaining) > 100:
                        print(
                            f"... +{len(remaining)-100} more"
                        )

                if was_offline:
                    print(
                        "🔄 Recovery process completed."
                    )
                    print()

                was_offline = False
                outage_start = None

                time.sleep(POLL_SECONDS)

            except KeyboardInterrupt:
                raise

            except Exception as e:

                if not was_offline:
                    outage_start = now_iso()

                    print()
                    print(
                        "🔴 CONNECTION/API LOST"
                    )
                    print(
                        f"Time: {outage_start}"
                    )

                was_offline = True

                print(
                    f"ERROR: {type(e).__name__}: {e}"
                )

                print(
                    f"Retrying in {RETRY_SECONDS}s..."
                )

                time.sleep(RETRY_SECONDS)

    except KeyboardInterrupt:
        print()
        print("STOPPED")

    finally:
        conn.close()


if __name__ == "__main__":
    run()
