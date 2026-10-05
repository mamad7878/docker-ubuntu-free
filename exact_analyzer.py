#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import sqlite3
from pathlib import Path

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

DB_PATH = os.environ.get("CRASH_DB_PATH", "/data/crash.db")
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.env")

def load_config():
    config = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                config[key.strip()] = value.strip().strip('"').strip("'")
    return config

CONFIG = load_config()
BOT_TOKEN = CONFIG.get("TELEGRAM_BOT_TOKEN", "").strip()
SAMPLE_COUNT = 10
TARGETS = (1.20, 1.50, 1.70, 2.00, 3.00)


def q2(x):
    return round(float(x), 2)


def get_rows():
    con = sqlite3.connect(DB_PATH)
    try:
        return con.execute(
            "SELECT game_id, rate FROM crashes ORDER BY game_id ASC"
        ).fetchall()
    finally:
        con.close()


def analyze():
    rows = get_rows()
    if not rows:
        raise RuntimeError("دیتابیس خالی است.")

    latest_id, latest_mult = rows[-1]
    target = q2(latest_mult)

    # Search backwards, excluding the latest record itself.
    matches = []
    for i in range(len(rows) - 2, -1, -1):
        if q2(rows[i][1]) == target:
            if i + 1 < len(rows):
                matches.append((rows[i], rows[i + 1]))
                if len(matches) >= SAMPLE_COUNT:
                    break

    return rows, latest_id, latest_mult, matches


def make_report():
    rows, latest_id, latest_mult, matches = analyze()
    target = q2(latest_mult)

    out = [
        "🎯 Crash Exact Analyzer",
        "",
        f"آخرین بازی: {latest_id}",
        f"ضریب فعلی: {target:.2f}x",
        "",
        "━━━━━━━━━━━━━━━━━━",
        f"📚 ۱۰ مورد قبلی دقیقاً {target:.2f}x",
        "━━━━━━━━━━━━━━━━━━",
    ]

    if not matches:
        out.append("❌ مورد قبلی با همین ضریب پیدا نشد.")
        out.append(f"تعداد نمونه موجود: 0")
        return "\n".join(out)

    for n, (ref, nxt) in enumerate(matches, 1):
        ref_id, ref_mult = ref
        next_id, next_mult = nxt
        out.append(
            f"{n}) {ref_id} → {q2(ref_mult):.2f}x  |  بعدی: {next_id} → {q2(next_mult):.2f}x"
        )

    following = [q2(nxt[1]) for _, nxt in matches]
    n = len(following)

    out += [
        "",
        "━━━━━━━━━━━━━━━━━━",
        "📊 آمار راند بلافاصله بعد",
        "━━━━━━━━━━━━━━━━━━",
        f"نمونه استفاده‌شده: {n}",
    ]

    for t in TARGETS:
        count = sum(x >= t for x in following)
        pct = count / n * 100
        out.append(f"≥ {t:.2f}x → {count}/{n} = {pct:.1f}%")

    out += [
        "",
        "⚠️ این درصدها فقط فراوانی تاریخی نمونه‌های پیدا‌شده هستند؛",
        "نتیجه راند بعدی را تضمین نمی‌کنند.",
    ]
    return "\n".join(out)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Crash Exact Analyzer آماده است.\n\n"
        "/analyze — تحلیل آخرین ضریب\n"
        "/status — وضعیت دیتابیس"
    )


async def analyze_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.reply_text(make_report())
    except Exception as e:
        await update.message.reply_text(f"❌ خطا: {e}")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        rows = get_rows()
        if not rows:
            await update.message.reply_text("❌ دیتابیس خالی است.")
            return
        gid, mult = rows[-1]
        await update.message.reply_text(
            f"📦 دیتابیس: {Path(DB_PATH).name}\n"
            f"تعداد رکورد: {len(rows):,}\n"
            f"آخرین بازی: {gid}\n"
            f"آخرین ضریب: {q2(mult):.2f}x"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ خطا: {e}")


def main():
    if not BOT_TOKEN:
        raise SystemExit(
            "CRASH_BOT_TOKEN تنظیم نشده است.\n"
            "اجرا: export CRASH_BOT_TOKEN='توکن_ربات'"
        )

    if not os.path.exists(DB_PATH):
        raise SystemExit(f"دیتابیس پیدا نشد: {DB_PATH}")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("analyze", analyze_cmd))
    app.add_handler(CommandHandler("status", status_cmd))

    print("Crash Exact Analyzer is running")
    print("DB:", DB_PATH)
    app.run_polling()


if __name__ == "__main__":
    main()
