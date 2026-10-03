#!/usr/bin/env python3

import sqlite3
import os
from statistics import mean

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "crash.db")


def analyze(limit=15):
    conn = sqlite3.connect(DB)

    rows = conn.execute("""
        SELECT game_id, rate
        FROM crashes
        ORDER BY game_id DESC
        LIMIT ?
    """, (limit,)).fetchall()

    conn.close()

    if not rows:
        return None

    rows = list(reversed(rows))
    values = [float(x[1]) for x in rows]

    count = len(values)
    avg = mean(values)
    minimum = min(values)
    maximum = max(values)

    below_2 = sum(x < 2.0 for x in values)
    at_least_2 = sum(x >= 2.0 for x in values)

    low_120 = sum(x < 1.20 for x in values)
    high_300 = sum(x >= 3.00 for x in values)

    latest_id, latest_rate = rows[-1]

    return {
        "rows": rows,
        "count": count,
        "average": avg,
        "minimum": minimum,
        "maximum": maximum,
        "below_2": below_2,
        "at_least_2": at_least_2,
        "low_120": low_120,
        "high_300": high_300,
        "latest_id": latest_id,
        "latest_rate": latest_rate,
    }


def make_report(data):
    if not data:
        return "هنوز داده‌ای برای تحلیل وجود ندارد."

    count = data["count"]

    rate_2 = (
        data["at_least_2"] / count * 100
        if count else 0
    )

    lines = [
        "🎯 Crash Analysis",
        "",
        f"🆔 آخرین ID: {data['latest_id']}",
        f"📈 آخرین ضریب: {data['latest_rate']:.2f}x",
        "",
        f"📊 تعداد بررسی: {count}",
        f"📉 کمترین: {data['minimum']:.2f}x",
        f"📈 بیشترین: {data['maximum']:.2f}x",
        f"📐 میانگین: {data['average']:.2f}x",
        "",
        f"🔴 زیر 2x: {data['below_2']} "
        f"({100-rate_2:.1f}%)",
        f"🟢 2x یا بیشتر: {data['at_least_2']} "
        f"({rate_2:.1f}%)",
        f"⚡ زیر 1.20x: {data['low_120']}",
        f"🚀 3x یا بیشتر: {data['high_300']}",
    ]

    lines.append("")
    lines.append("آخرین ضرایب:")

    for game_id, rate in reversed(data["rows"]):
        lines.append(
            f"{game_id} → {rate:.2f}x"
        )

    return "\n".join(lines)


if __name__ == "__main__":
    data = analyze(15)
    print(make_report(data))
