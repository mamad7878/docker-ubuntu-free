#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sqlite3
import asyncio
import json
import re
import sys
import subprocess
import signal

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

import exact_analyzer
import rule_b_signals as rule_b


BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("CRASH_DB_PATH", "/data/crash.db")
CONFIG = os.path.join(BASE, "config.env")
RECOVERY_FILE = os.path.join(BASE, "recovery_report.json")

INTERVAL = 3

monitor_task = None
collector_process = None


def load_config():
    config = {}

    if not os.path.exists(CONFIG):
        return config

    with open(CONFIG, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()

            if (
                not line
                or line.startswith("#")
                or "=" not in line
            ):
                continue

            key, value = line.split("=", 1)

            config[key.strip()] = (
                value.strip()
                .strip('"')
                .strip("'")
            )

    return config


CONFIG_DATA = load_config()

BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN",
    CONFIG_DATA.get("TELEGRAM_BOT_TOKEN", "")
).strip()

CHAT_ID = os.getenv(
    "TELEGRAM_CHAT_ID",
    CONFIG_DATA.get("TELEGRAM_CHAT_ID", "")
).strip()


def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["📜 تاریخچه ضرایب", "📊 وضعیت"],
            ["🔍 تحلیل", "▶️ شروع"],
            ["📈 تحلیل آماری"],
            ["🔧 شکاف‌های دیتابیس"],
            ["🗑 پاک کردن تاریخچه"],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def recovery_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔎 تکمیل دستی IDهای گمشده",
                callback_data="manual_recovery"
            )
        ],
        [
            InlineKeyboardButton(
                "❌ غیرقابل بازیابی",
                callback_data="mark_unrecoverable"
            )
        ],
        [
            InlineKeyboardButton(
                "🔄 بررسی مجدد بازیابی",
                callback_data="retry_recovery"
            )
        ],
    ])


def get_latest_id():
    if not os.path.exists(DB):
        return 0

    conn = sqlite3.connect(DB)

    try:
        row = conn.execute(
            "SELECT MAX(game_id) FROM crashes"
        ).fetchone()

        return (
            row[0]
            if row and row[0] is not None
            else 0
        )

    finally:
        conn.close()


def get_new_results(last_id):
    if not os.path.exists(DB):
        return []

    conn = sqlite3.connect(DB)

    try:
        return conn.execute("""
            SELECT game_id, rate
            FROM crashes
            WHERE game_id > ?
            ORDER BY game_id ASC
        """, (last_id,)).fetchall()

    finally:
        conn.close()


def get_missing():
    if not os.path.exists(DB):
        return []

    conn = sqlite3.connect(DB)

    try:
        return [
            row[0]
            for row in conn.execute("""
                SELECT game_id
                FROM missing_games
                WHERE status='missing'
                ORDER BY game_id ASC
            """).fetchall()
        ]

    finally:
        conn.close()


def get_missing_count():
    return len(get_missing())


def format_duration(seconds):
    if seconds is None:
        return "نامشخص"

    seconds = int(seconds)

    hours, remainder = divmod(
        seconds,
        3600
    )

    minutes, seconds = divmod(
        remainder,
        60
    )

    if hours:
        return (
            f"{hours} ساعت "
            f"{minutes} دقیقه "
            f"{seconds} ثانیه"
        )

    if minutes:
        return (
            f"{minutes} دقیقه "
            f"{seconds} ثانیه"
        )

    return f"{seconds} ثانیه"


def load_recovery_report():
    if not os.path.exists(RECOVERY_FILE):
        return None

    try:
        with open(
            RECOVERY_FILE,
            "r",
            encoding="utf-8"
        ) as f:
            return json.load(f)

    except Exception as e:
        print(
            "[RECOVERY READ ERROR]",
            e
        )
        return None


def clear_recovery_report():
    try:
        os.remove(RECOVERY_FILE)
    except FileNotFoundError:
        pass
    except Exception as e:
        print(
            "[RECOVERY FILE ERROR]",
            e
        )


def make_recovery_message(report):
    recovered = report.get(
        "recovered",
        []
    )

    missing = report.get(
        "missing",
        []
    )

    outage_start = report.get(
        "outage_start",
        "نامشخص"
    )

    recovery_time = report.get(
        "recovery_time",
        "نامشخص"
    )

    duration = format_duration(
        report.get("duration_seconds")
    )

    lines = [
        "🔄 گزارش بازیابی",
        "",
        f"شروع قطعی: {outage_start}",
        f"برگشت اتصال: {recovery_time}",
        f"مدت قطعی: {duration}",
        "",
        "━━━━━━━━━━━━━━━━━━",
        f"📥 تعداد رکورد بازیابی‌شده: "
        f"{len(recovered)}",
        f"⚠️ تعداد رکورد باقی‌مانده: "
        f"{len(missing)}",
        "━━━━━━━━━━━━━━━━━━",
    ]

    if recovered:
        lines.append("")
        lines.append("✅ بازیابی خودکار:")

        for item in recovered[:100]:
            lines.append(
                f"🟢 {item['game_id']} → "
                f"{float(item['rate']):.2f}x"
            )

        if len(recovered) > 100:
            lines.append(
                f"... و {len(recovered)-100} مورد دیگر"
            )

    if missing:
        lines.extend([
            "",
            "❌ این IDها هنوز پیدا نشده‌اند:",
            ", ".join(
                str(x)
                for x in missing[:100]
            ),
        ])

        if len(missing) > 100:
            lines.append(
                f"... و {len(missing)-100} مورد دیگر"
            )

        lines.extend([
            "",
            "ضریب این موارد حدس زده نمی‌شود.",
            "اگر در تاریخچه سایت هستند، "
            "دکمه تکمیل دستی را بزن."
        ])

    else:
        lines.extend([
            "",
            "🎉 شکاف قابل مشاهده‌ای باقی نمانده است."
        ])

    return "\n".join(lines)


async def send_recovery_report(application):
    report = load_recovery_report()

    if not report:
        return

    try:
        message = make_recovery_message(
            report
        )

        missing = report.get(
            "missing",
            []
        )

        await application.bot.send_message(
            chat_id=CHAT_ID,
            text=message,
            reply_markup=(
                recovery_keyboard()
                if missing
                else None
            )
        )

        print(
            "📨 Recovery report sent to Telegram"
        )

        clear_recovery_report()

    except Exception as e:
        print(
            "[RECOVERY TELEGRAM ERROR]",
            e
        )




async def process_rule_b(application):
    """Process experimental Rule B signals without blocking normal monitoring."""
    signals_db = rule_b.SIGNALS_DB

    try:
        rule_b.reconcile(DB, signals_db)

        latest_id = get_latest_id()
        if latest_id is not None:
            rule_b.create_signal(
                latest_id,
                crash_db=DB,
                signals_db=signals_db
            )

        rule_b.reconcile(DB, signals_db)

        # This function returns tuples, not dictionaries.
        for target_id, trigger_id, rates_json in rule_b.get_unsent_signals(signals_db):
            rates = json.loads(rates_json)
            rates_text = "، ".join(f"{float(x):.2f}x" for x in rates)

            # Reconcile again immediately before sending. If the target
            # already arrived, it becomes "missed" and is no longer pending.
            rule_b.reconcile(DB, signals_db)
            pending_ids = {
                int(row[0])
                for row in rule_b.get_unsent_signals(signals_db)
            }
            if int(target_id) not in pending_ids:
                continue

            message = (
                "🧪 سیگنال آزمایشی قانون B\n\n"
                f"🎯 بازی هدف: {int(target_id)}\n"
                f"🔎 بازی محرک: {int(trigger_id)}\n"
                f"📊 پنج ضریب قبلی: {rates_text}\n"
                "شرط: پنج بازی متوالی زیر 2x\n"
                "هدف ارزیابی: بازی بعدی حداقل 1.5x\n\n"
                "⚠️ آزمایشی است؛ تضمین برد نیست."
            )

            # Recheck immediately before attempting delivery.
            if rule_b.target_exists(target_id, DB):
                rule_b.reconcile(DB, signals_db)
                continue

            # Record the attempt time before the network call, so a result
            # arriving during delivery is not automatically treated as late.
            rule_b.mark_signal_sent(target_id, signals_db)

            # Close the race if the target arrived between the first check
            # and recording the send timestamp.
            if rule_b.target_exists(target_id, DB):
                rule_b.unmark_signal_sent(target_id, signals_db)
                rule_b.reconcile(DB, signals_db)
                continue

            try:
                await application.bot.send_message(
                    chat_id=CHAT_ID,
                    text=message
                )
                print(f"[RULE B] Signal sent for target {target_id}")
            except Exception as exc:
                rule_b.unmark_signal_sent(target_id, signals_db)
                rule_b.reconcile(DB, signals_db)
                print("[RULE B SIGNAL SEND ERROR]", type(exc).__name__, exc)
                break

        rule_b.reconcile(DB, signals_db)

        # This function also returns tuples.
        for target_id, status, target_rate in rule_b.get_unsent_results(signals_db):
            if status == "win":
                result_text = (
                    f"✅ نتیجه قانون B: برد\n"
                    f"بازی {target_id}: {float(target_rate):.2f}x"
                )
            elif status == "loss":
                result_text = (
                    f"❌ نتیجه قانون B: باخت\n"
                    f"بازی {target_id}: {float(target_rate):.2f}x"
                )
            elif status == "void":
                result_text = (
                    f"⚠️ نتیجه قانون B: بازی هدف {target_id} "
                    "در داده‌ها پیدا نشد."
                )
            else:
                continue

            try:
                await application.bot.send_message(
                    chat_id=CHAT_ID,
                    text=result_text + "\n🧪 نتیجه آزمایشی است."
                )
                rule_b.mark_result_sent(target_id, signals_db)
            except Exception as exc:
                print("[RULE B RESULT SEND ERROR]", type(exc).__name__, exc)
                break

    except Exception as exc:
        print("[RULE B PROCESS ERROR]", type(exc).__name__, exc)


async def monitor_crash(application):
    last_id = get_latest_id()

    print("=" * 55)
    print("           CRASH TELEGRAM BOT")
    print("=" * 55)
    print("Database :", DB)
    print("Chat ID  :", CHAT_ID)
    print("Last ID  :", last_id)
    print("=" * 55)

    while True:

        try:
            try:
                await process_rule_b(application)
            except Exception as rule_b_exc:
                print('[RULE B MONITOR ERROR]', type(rule_b_exc).__name__, rule_b_exc)

            await send_recovery_report(
                application
            )

            new_rows = get_new_results(
                last_id
            )

            for game_id, rate in new_rows:

                message = (
                    "🎯 Crash جدید\n\n"
                    f"ID: {game_id}\n"
                    f"ضریب: {float(rate):.2f}x"
                )

                try:
                    await application.bot.send_message(
                        chat_id=CHAT_ID,
                        text=message
                    )

                    print(
                        f"📨 Telegram → "
                        f"{game_id} = "
                        f"{float(rate):.2f}x"
                    )

                    last_id = game_id

                except Exception as e:
                    print(
                        "[TELEGRAM SEND ERROR]",
                        e
                    )
                    break

            await asyncio.sleep(INTERVAL)

        except asyncio.CancelledError:
            break

        except Exception as e:
            print(
                "[MONITOR ERROR]",
                type(e).__name__,
                e
            )

            await asyncio.sleep(INTERVAL)


def start_collector():
    global collector_process

    # اگر Collector از قبل اجراست، Collector دوم اجرا نکن
    try:
        result = subprocess.run(
            ["pgrep", "-f", "python.*live_collector.py"],
            capture_output=True,
            text=True
        )

        running = [
            x.strip()
            for x in result.stdout.splitlines()
            if x.strip() and int(x.strip()) != os.getpid()
        ]

        if running:
            print(
                f"📡 Collector already running "
                f"(PID {running[0]}), no duplicate started."
            )
            return

    except Exception as e:
        print("[COLLECTOR CHECK ERROR]", e)

    collector_path = os.path.join(
        BASE,
        "live_collector.py"
    )

    if not os.path.exists(collector_path):
        print(
            "❌ live_collector.py پیدا نشد."
        )
        return

    try:
        collector_process = subprocess.Popen(
            [sys.executable, collector_path],
            cwd=BASE,
            start_new_session=True
        )

        print(
            f"📡 Collector started automatically "
            f"(PID {collector_process.pid})"
        )

    except Exception as e:
        print(
            "[COLLECTOR START ERROR]",
            e
        )


def stop_collector():
    global collector_process

    if collector_process is None:
        return

    if collector_process.poll() is not None:
        collector_process = None
        return

    try:
        print(
            f"🛑 Stopping Collector "
            f"(PID {collector_process.pid})..."
        )

        collector_process.terminate()

        try:
            collector_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            print(
                "⚠️ Collector did not stop gracefully; "
                "sending SIGKILL."
            )
            collector_process.kill()
            collector_process.wait(timeout=2)

        print("✅ Collector stopped.")

    except Exception as e:
        print(
            "[COLLECTOR STOP ERROR]",
            e
        )

    finally:
        collector_process = None


async def post_init(application):
    global monitor_task

    start_collector()

    monitor_task = asyncio.create_task(
        monitor_crash(application)
    )


async def post_shutdown(application):
    global monitor_task

    if monitor_task:
        monitor_task.cancel()

        try:
            await monitor_task
        except asyncio.CancelledError:
            pass

    stop_collector()


def get_history(limit=30):
    if not os.path.exists(DB):
        return []

    conn = sqlite3.connect(DB)

    try:
        return conn.execute(
            """
            SELECT game_id, rate
            FROM crashes
            ORDER BY game_id DESC
            LIMIT ?
            """,
            (limit,)
        ).fetchall()

    finally:
        conn.close()


async def history_cmd(update, context):
    try:
        rows = get_history(30)

        if not rows:
            await update.message.reply_text(
                "📜 تاریخچه ضرایب خالی است.",
                reply_markup=main_keyboard()
            )
            return

        lines = [
            "📜 تاریخچه آخرین ضرایب",
            ""
        ]

        for game_id, rate in rows:
            rate = float(rate)

            if rate < 2:
                icon = "🔴"
            elif rate < 10:
                icon = "🟢"
            else:
                icon = "🟡"

            lines.append(
                f"{icon} {game_id} → {rate:.2f}x"
            )

        lines.extend([
            "",
            f"📊 تعداد نمایش: {len(rows)}"
        ])

        await update.message.reply_text(
            "\n".join(lines),
            reply_markup=main_keyboard()
        )

    except Exception as e:
        await update.message.reply_text(
            f"❌ خطا در تاریخچه:\n{e}",
            reply_markup=main_keyboard()
        )


async def recovery_status_cmd(update, context):
    missing = get_missing()

    if not missing:
        await update.message.reply_text(
            "✅ هیچ ID مفقودی ثبت‌شده‌ای وجود ندارد.",
            reply_markup=main_keyboard()
        )
        return

    lines = [
        "🔧 شکاف‌های دیتابیس",
        "",
        f"⚠️ تعداد ضرایب مفقود: {len(missing)}",
        "",
        "IDهای مفقود:"
    ]

    lines.append(
        ", ".join(
            str(x)
            for x in missing[:100]
        )
    )

    if len(missing) > 100:
        lines.append(
            f"... و {len(missing)-100} مورد دیگر"
        )

    lines.extend([
        "",
        "از دکمه زیر برای واردکردن دستی استفاده کن."
    ])

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=recovery_keyboard()
    )


async def recovery_callback(update, context):
    query = update.callback_query
    await query.answer()

    if query.data == "manual_recovery":
        context.user_data[
            "waiting_manual_recovery"
        ] = True

        await query.message.reply_text(
            "🔎 تکمیل دستی\n\n"
            "ضرایب تاریخچه سایت را همینجا ارسال کن.\n\n"
            "فرمت‌های قابل قبول:\n"
            "9665701 1.24\n"
            "9665702 3.51\n\n"
            "یا:\n"
            "9665701 = 1.24x\n"
            "9665702 = 3.51x\n\n"
            "می‌توانی چندین ID را یکجا بفرستی.\n"
            "فقط IDهایی که واقعاً در تاریخچه سایت "
            "دیدی وارد کن.\n\n"
            "برای لغو: /cancel"
        )

        return

    if query.data == "retry_recovery":
        missing = get_missing()

        await query.message.reply_text(
            "🔄 Collector در تلاش برای بازیابی "
            "خودکار است.\n"
            f"IDهای فعلی: {len(missing)}"
        )

        return

    if query.data == "mark_unrecoverable":
        missing = get_missing()

        if not missing:
            await query.edit_message_text(
                "✅ دیگر ID مفقودی وجود ندارد."
            )
            return

        conn = sqlite3.connect(DB)

        try:
            conn.executemany(
                """
                UPDATE missing_games
                SET status='unrecoverable',
                    resolved_at=?
                WHERE game_id=?
                  AND status='missing'
                """,
                [
                    (
                        __import__("datetime")
                        .datetime
                        .datetime
                        .now()
                        .isoformat(),
                        game_id
                    )
                    for game_id in missing
                ]
            )

            conn.commit()

        finally:
            conn.close()

        await query.edit_message_text(
            "❌ این IDها به‌عنوان "
            "غیرقابل بازیابی ثبت شدند.\n\n"
            f"تعداد: {len(missing)}\n\n"
            "هیچ ضریبی برای آنها ساخته یا حدس زده نشد."
        )


async def cancel_cmd(update, context):
    context.user_data.pop(
        "waiting_manual_recovery",
        None
    )

    await update.message.reply_text(
        "❌ تکمیل دستی لغو شد.",
        reply_markup=main_keyboard()
    )


def parse_manual_records(text):
    records = []

    patterns = [
        re.compile(
            r"(\d{5,})\s*(?:=|:|→|-)?\s*"
            r"(\d+(?:\.\d+)?)\s*x?",
            re.IGNORECASE
        )
    ]

    for line in text.splitlines():
        line = line.strip()

        if not line:
            continue

        match = None

        for pattern in patterns:
            match = pattern.search(line)
            if match:
                break

        if not match:
            continue

        game_id = int(match.group(1))
        rate = float(match.group(2))

        if rate <= 0:
            continue

        records.append(
            (game_id, rate)
        )

    return records


async def manual_data_handler(update, context):
    if not context.user_data.get(
        "waiting_manual_recovery"
    ):
        return

    records = parse_manual_records(
        update.message.text or ""
    )

    if not records:
        await update.message.reply_text(
            "❌ هیچ رکورد قابل تشخیصی پیدا نشد.\n\n"
            "مثال:\n"
            "9665701 1.24\n"
            "9665702 3.51"
        )
        return

    missing = set(get_missing())

    accepted = []
    ignored = []

    conn = sqlite3.connect(DB)

    try:
        for game_id, rate in records:

            if game_id not in missing:
                ignored.append(
                    (game_id, rate, "not_missing")
                )
                continue

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
                VALUES (?, ?, NULL, NULL, NULL, NULL, NULL, ?)
            """, (
                game_id,
                rate,
                __import__("datetime")
                .datetime
                .datetime
                .now()
                .isoformat()
            ))

            if cur.rowcount == 1:
                conn.execute("""
                    UPDATE missing_games
                    SET status='recovered',
                        resolved_at=?,
                        source='telegram_manual'
                    WHERE game_id=?
                """, (
                    __import__("datetime")
                    .datetime
                    .datetime
                    .now()
                    .isoformat(),
                    game_id
                ))

                accepted.append(
                    (game_id, rate)
                )

        conn.commit()

    finally:
        conn.close()

    context.user_data.pop(
        "waiting_manual_recovery",
        None
    )

    remaining = get_missing()

    lines = [
        "✅ تکمیل دستی انجام شد.",
        "",
        f"🟢 ثبت‌شده: {len(accepted)}",
        f"⚠️ هنوز مفقود: {len(remaining)}",
    ]

    if accepted:
        lines.extend([
            "",
            "رکوردهای ثبت‌شده:"
        ])

        for game_id, rate in accepted:
            lines.append(
                f"🟢 {game_id} → {rate:.2f}x"
            )

    if remaining:
        lines.extend([
            "",
            "IDهای باقی‌مانده:",
            ", ".join(
                str(x)
                for x in remaining[:100]
            )
        ])

        if len(remaining) > 100:
            lines.append(
                f"... و {len(remaining)-100} مورد دیگر"
            )

        lines.extend([
            "",
            "اگر دیگر نمی‌توانی آنها را از سایت پیدا کنی، "
            "دکمه «❌ غیرقابل بازیابی» را بزن."
        ])
    else:
        lines.extend([
            "",
            "🎉 تمام IDهای مفقود فعلی تکمیل شدند."
        ])

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=(
            recovery_keyboard()
            if remaining
            else main_keyboard()
        )
    )


async def clear_history_cmd(update, context):
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ بله، پاک کن",
                callback_data="confirm_clear_history"
            ),
            InlineKeyboardButton(
                "❌ انصراف",
                callback_data="cancel_clear_history"
            ),
        ]
    ])

    await update.message.reply_text(
        "⚠️ هشدار\n\n"
        "تمام تاریخچه ضرایب ذخیره‌شده در crash.db "
        "پاک می‌شود.\n\n"
        "این کار قابل بازگشت نیست.\n\n"
        "آیا مطمئنی؟",
        reply_markup=keyboard
    )


async def clear_history_callback(update, context):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_clear_history":
        await query.edit_message_text(
            "❌ پاک کردن تاریخچه لغو شد."
        )
        return

    if query.data == "confirm_clear_history":
        conn = sqlite3.connect(DB)

        try:
            cur = conn.cursor()

            cur.execute(
                "SELECT COUNT(*) FROM crashes"
            )

            count = cur.fetchone()[0]

            cur.execute(
                "DELETE FROM crashes"
            )

            cur.execute(
                "DELETE FROM missing_games"
            )

            conn.commit()

            try:
                cur.execute("VACUUM")
            except Exception:
                pass

        finally:
            conn.close()

        await query.edit_message_text(
            "✅ تاریخچه با موفقیت پاک شد.\n\n"
            f"🗑 تعداد رکوردهای حذف‌شده: {count:,}\n\n"
            "📦 دیتابیس اکنون خالی است."
        )


async def stats_analysis_cmd(update, context):
    try:
        conn = sqlite3.connect(DB)

        rows = conn.execute("""
            SELECT game_id, rate
            FROM crashes
            ORDER BY game_id ASC
            LIMIT 5000
        """).fetchall()

        conn.close()

        if len(rows) < 100:
            await update.message.reply_text(
                f"⚠️ برای Pattern Matching حداقل 100 راند لازم است.\n\n"
                f"📊 داده موجود: {len(rows)} راند",
                reply_markup=main_keyboard()
            )
            return

        values = [float(x[1]) for x in rows]

        def bucket(x):
            if x < 1.50:
                return "A"
            elif x < 2.00:
                return "B"
            elif x < 3.00:
                return "C"
            elif x < 5.00:
                return "D"
            return "E"

        pattern_len = 5

        current_pattern = tuple(
            bucket(x)
            for x in values[-pattern_len:]
        )

        matches = []

        for i in range(
            pattern_len,
            len(values) - 1
        ):
            historical_pattern = tuple(
                bucket(x)
                for x in values[
                    i-pattern_len:i
                ]
            )

            if historical_pattern == current_pattern:
                matches.append(values[i])

        total_matches = len(matches)

        if total_matches == 0:
            await update.message.reply_text(
                "\n".join([
                    "📈 Pattern Matching + Backtest",
                    "",
                    "❌ الگوی مشابهی پیدا نشد.",
                    "",
                    f"🔹 الگوی فعلی: "
                    f"{' '.join(current_pattern)}",
                    f"📊 دیتابیس: {len(values)} راند",
                ]),
                reply_markup=main_keyboard()
            )
            return

        def calc(threshold):
            success = sum(
                x >= threshold
                for x in matches
            )
            return (
                success,
                success / total_matches * 100
            )

        s15, p15 = calc(1.50)
        s20, p20 = calc(2.00)
        s30, p30 = calc(3.00)
        s50, p50 = calc(5.00)
        s100, p100 = calc(10.00)

        current = values[-1]

        if total_matches >= 500:
            quality = "🟢 نمونه بسیار خوب"
        elif total_matches >= 200:
            quality = "🟢 نمونه خوب"
        elif total_matches >= 100:
            quality = "🟡 نمونه متوسط"
        elif total_matches >= 50:
            quality = "🟠 نمونه کم"
        else:
            quality = "🔴 نمونه بسیار کم"

        lines = [
            "📈 Pattern Matching + Backtest",
            "",
            f"🎯 آخرین ضریب: {current:.2f}x",
            f"🔎 الگوی 5 راند آخر: "
            f"{' '.join(current_pattern)}",
            "",
            f"📚 موارد مشابه تاریخی: {total_matches}",
            f"📊 حجم دیتابیس: {len(values)} راند",
            f"🧪 کیفیت نمونه: {quality}",
            "",
            "نتیجه راند بلافاصله بعد از الگو:",
            f"≥1.5x → {p15:.1f}% ({s15}/{total_matches})",
            f"≥2x → {p20:.1f}% ({s20}/{total_matches})",
            f"≥3x → {p30:.1f}% ({s30}/{total_matches})",
            f"≥5x → {p50:.1f}% ({s50}/{total_matches})",
            f"≥10x → {p100:.1f}% ({s100}/{total_matches})",
            "",
            "⚠️ این Backtest تاریخی است، نه پیش‌بینی قطعی."
        ]

        await update.message.reply_text(
            "\n".join(lines),
            reply_markup=main_keyboard()
        )

    except Exception as e:
        await update.message.reply_text(
            f"❌ خطا در Pattern Matching:\n{e}",
            reply_markup=main_keyboard()
        )


async def start(update, context):
    await update.message.reply_text(
        "🤖 Crash Analys آماده است.\n\n"
        "Collector اکنون قابلیت تشخیص شکاف، "
        "بازیابی خودکار و تکمیل دستی دارد.",
        reply_markup=main_keyboard()
    )


async def analyze_cmd(update, context):
    try:
        report = exact_analyzer.make_report()

        await update.message.reply_text(
            report,
            reply_markup=main_keyboard()
        )

    except Exception as e:
        await update.message.reply_text(
            f"❌ خطا در تحلیل:\n{e}",
            reply_markup=main_keyboard()
        )




async def rule_b_cmd(update, context):
    try:
        stats = rule_b.get_stats(rule_b.SIGNALS_DB)
        wins = int(stats.get("win", 0))
        losses = int(stats.get("loss", 0))
        pending = int(stats.get("pending", 0))
        void = int(stats.get("void", 0))
        missed = int(stats.get("missed", 0))
        decided = wins + losses
        win_rate = (100.0 * wins / decided) if decided else 0.0

        message = (
            "🧪 آمار قانون B (آزمایشی)\n\n"
            "قانون: پنج بازی قبلی همگی زیر 2x\n"
            "هدف: بازی بعدی حداقل 1.5x\n\n"
            f"✅ برد: {wins}\n"
            f"❌ باخت: {losses}\n"
            f"📈 درصد برد: {win_rate:.2f}% ({wins}/{decided})\n"
            f"⏳ در انتظار: {pending}\n"
            f"⚠️ بازی ناموجود: {void}\n"
            f"🚫 سیگنال ازدست‌رفته: {missed}\n\n"
            "این آمار تضمین‌کننده نتیجه آینده نیست."
        )
    except Exception as exc:
        print("[RULE B STATS ERROR]", type(exc).__name__, exc)
        message = "خطا در خواندن آمار قانون B؛ لاگ ترمینال را بررسی کن."

    await update.effective_message.reply_text(message)


async def status_cmd(update, context):
    try:
        if not os.path.exists(DB):
            await update.message.reply_text(
                "❌ crash.db پیدا نشد.",
                reply_markup=main_keyboard()
            )
            return

        conn = sqlite3.connect(DB)

        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM crashes"
            ).fetchone()[0]

            latest = conn.execute("""
                SELECT game_id, rate
                FROM crashes
                ORDER BY game_id DESC
                LIMIT 1
            """).fetchone()

            missing_count = conn.execute("""
                SELECT COUNT(*)
                FROM missing_games
                WHERE status='missing'
            """).fetchone()[0]

        finally:
            conn.close()

        if not latest:
            await update.message.reply_text(
                "📦 دیتابیس خالی است.",
                reply_markup=main_keyboard()
            )
            return

        game_id, rate = latest

        await update.message.reply_text(
            "📦 Crash Database\n\n"
            f"تعداد رکورد: {count:,}\n"
            f"آخرین بازی: {game_id}\n"
            f"آخرین ضریب: {float(rate):.2f}x\n"
            f"⚠️ IDهای مفقود: {missing_count}",
            reply_markup=main_keyboard()
        )

    except Exception as e:
        await update.message.reply_text(
            f"❌ خطا:\n{e}",
            reply_markup=main_keyboard()
        )


async def button_router(update, context):
    text = (update.message.text or "").strip()

    if text == "📜 تاریخچه ضرایب":
        await history_cmd(update, context)

    elif text == "📊 وضعیت":
        await status_cmd(update, context)

    elif text == "🔍 تحلیل":
        await analyze_cmd(update, context)

    elif text == "▶️ شروع":
        await start(update, context)

    elif text == "📈 تحلیل آماری":
        await stats_analysis_cmd(update, context)

    elif text == "🔧 شکاف‌های دیتابیس":
        await recovery_status_cmd(update, context)

    elif text == "🗑 پاک کردن تاریخچه":
        await clear_history_cmd(update, context)


def main():
    if not BOT_TOKEN:
        raise SystemExit(
            "❌ TELEGRAM_BOT_TOKEN "
            "در config.env تنظیم نشده."
        )

    if not CHAT_ID:
        raise SystemExit(
            "❌ TELEGRAM_CHAT_ID "
            "در config.env تنظیم نشده."
        )

    # Start Collector BEFORE Telegram polling.
    # This makes "python bot.py" start both services.
    start_collector()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(
        CommandHandler("start", start)
    )

    app.add_handler(
        CommandHandler("cancel", cancel_cmd)
    )

    app.add_handler(
        CommandHandler("analyze", analyze_cmd)
    )

    app.add_handler(
        CommandHandler("status", status_cmd)
    )

    app.add_handler(
        CommandHandler("ruleb", rule_b_cmd)
    )

    app.add_handler(
        CommandHandler("history", history_cmd)
    )

    app.add_handler(
        CallbackQueryHandler(
            clear_history_callback,
            pattern=(
                "^(confirm_clear_history|"
                "cancel_clear_history)$"
            )
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            recovery_callback,
            pattern=(
                "^(manual_recovery|"
                "mark_unrecoverable|"
                "retry_recovery)$"
            )
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            manual_data_handler
        ),
        group=0
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            button_router
        ),
        group=1
    )

    print(
        "Crash Analys Bot is running..."
    )

    print(
        "Commands: "
        "/start /analyze /status /history /cancel"
    )

    app.run_polling(
        drop_pending_updates=False,
        bootstrap_retries=-1
    )


if __name__ == "__main__":
    main()
