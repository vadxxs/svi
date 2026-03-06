# bot.py
# pip install -U python-telegram-bot[job-queue] requests

import os
import re
import json
import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple, List
from datetime import datetime

import requests
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("dtek_bot")

DTEK_BASE = "https://www.dtek-oem.com.ua"
SHUTDOWNS_URL = f"{DTEK_BASE}/ua/shutdowns"
AJAX_URL = f"{DTEK_BASE}/ua/ajax"
METHOD = "getHomeNum"

DEFAULT_CHECK_SECONDS = 90
ADDRESSES_FILE = os.environ.get("ADDRESSES_FILE", "adresses.txt")

ASK_CITY, ASK_STREET_QUERY, ASK_STREET_PICK, ASK_HOUSE, ASK_INTERVAL = range(5)

SESSION = requests.Session()
SESSION.trust_env = False

BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": DTEK_BASE,
    "Referer": SHUTDOWNS_URL,
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
}

EMO = {
    "bolt": "⚡",
    "plug": "🔌",
    "check": "✅",
    "cross": "❌",
    "warn": "⚠️",
    "gear": "⚙️",
    "city": "🏙️",
    "road": "🛣️",
    "home": "🏠",
    "clock": "⏱️",
    "bell": "🔔",
    "stop": "🛑",
    "search": "🔎",
    "list": "📋",
    "back": "⬅️",
    "next": "➡️",
    "cancel": "✖️",
    "info": "ℹ️",
    "pin": "📍",
    "sparkles": "✨",
    "refresh": "🔄",
}

BTN = {
    "next": f"{EMO['next']} Далі",
    "back": f"{EMO['back']} Назад",
    "cancel": f"{EMO['cancel']} Скасувати",
    "set": f"{EMO['gear']} Налаштувати адресу",
    "status": f"{EMO['info']} Статус",
    "stop": f"{EMO['stop']} Вимкнути сповіщення",
    "interval": f"{EMO['clock']} Інтервал",
}


@dataclass(frozen=True)
class Address:
    city: str
    street: str
    house: str


def normalize_house(h: str) -> str:
    return h.strip()


def build_payload(city: str, street: str) -> Dict[str, str]:
    update_fact = datetime.now().strftime("%d.%m.%Y %H:%M")
    return {
        "method": METHOD,
        "data[0][name]": "city",
        "data[0][value]": city,
        "data[1][name]": "street",
        "data[1][value]": street,
        "data[2][name]": "updateFact",
        "data[2][value]": update_fact,
    }


def extract_house(api_json: Dict[str, Any], house: str) -> Tuple[Optional[Dict[str, Any]], str]:
    data = api_json.get("data") or {}
    ts = api_json.get("updateTimestamp") or ""
    return data.get(normalize_house(house)), ts


def _fmt_dt(s: str) -> str:
    return s.strip() if (s or "").strip() else "—"


def format_status(addr: Address, house_obj: Optional[Dict[str, Any]], update_ts: str) -> str:
    header = (
        f"{EMO['pin']} Адреса: {addr.city}, {addr.street}, буд. {addr.house}\n"
        f"{EMO['clock']} Оновлено: {_fmt_dt(update_ts)}\n"
        "━━━━━━━━━━━━━━━━━━\n"
    )

    if house_obj is None:
        return header + f"{EMO['warn']} Будинок не знайдено у відповіді. Перевір номер/літеру (14А, 18Г/1)."

    sub_type = (house_obj.get("sub_type") or "").strip()
    start_date = (house_obj.get("start_date") or "").strip()
    end_date = (house_obj.get("end_date") or "").strip()
    reasons = house_obj.get("sub_type_reason") or []
    reasons_txt = ", ".join(str(x) for x in reasons if x)

    if sub_type and start_date and end_date:
        return (
            header
            + f"{EMO['bolt']} Статус: ВІДКЛЮЧЕННЯ\n"
            + f"{EMO['plug']} Тип: {sub_type}\n"
            + f"🟢 Початок: {start_date}\n"
            + f"🔴 Кінець: {end_date}\n"
            + (f"{EMO['info']} Код(и): {reasons_txt}\n" if reasons_txt else "")
        )

    return (
        header
        + f"{EMO['check']} Статус: немає активного відключення.\n"
        + (f"{EMO['info']} Код(и): {reasons_txt}\n" if reasons_txt else "")
    )


def make_state_snapshot(house_obj: Optional[Dict[str, Any]], update_ts: str) -> Dict[str, Any]:
    if house_obj is None:
        return {
            "exists": False,
            "update_ts": (update_ts or "").strip(),
        }

    return {
        "exists": True,
        "update_ts": (update_ts or "").strip(),
        "sub_type": (house_obj.get("sub_type") or "").strip(),
        "start_date": (house_obj.get("start_date") or "").strip(),
        "end_date": (house_obj.get("end_date") or "").strip(),
        "reasons": list(house_obj.get("sub_type_reason") or []),
    }


def hash_snapshot(snapshot: Dict[str, Any]) -> str:
    raw = json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _parse_csrf_from_html(html: str) -> Optional[str]:
    m = re.search(r'name=["\']csrf-token["\']\s+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
    if m:
        return m.group(1)

    m = re.search(r'csrf(?:Token)?["\']?\s*[:=]\s*["\']([^"\']+)["\']', html, re.IGNORECASE)
    if m:
        return m.group(1)

    return None


def ensure_session_and_csrf(timeout: int = 25) -> str:
    r = SESSION.get(
        SHUTDOWNS_URL,
        headers={"User-Agent": BASE_HEADERS["User-Agent"], "Accept": "text/html,*/*"},
        timeout=timeout,
    )
    r.raise_for_status()

    csrf = _parse_csrf_from_html(r.text)
    if not csrf:
        raise RuntimeError("Не вдалося знайти csrf-token у HTML /ua/shutdowns")

    return csrf


def fetch_dtek(city: str, street: str, timeout: int = 25) -> Dict[str, Any]:
    csrf = ensure_session_and_csrf(timeout=timeout)
    headers = dict(BASE_HEADERS)
    headers["x-csrf-token"] = csrf

    payload = build_payload(city, street)
    r = SESSION.post(AJAX_URL, data=payload, headers=headers, timeout=timeout)

    try:
        data = r.json()
    except Exception:
        raise RuntimeError(f"DTEK non-JSON response. HTTP {r.status_code}. Body: {r.text[:300]}")

    if not isinstance(data, dict) or not data.get("result", False):
        raise RuntimeError(f"DTEK API error. HTTP {r.status_code}. Body: {data}")

    return data


def job_name(user_id: int) -> str:
    return f"watch:{user_id}"


def load_address_book(path: str) -> Dict[str, List[str]]:
    try_paths = [path]
    if not os.path.isabs(path):
        try_paths.append(os.path.join(os.path.dirname(__file__), path))

    last_err = None
    for p in try_paths:
        try:
            with open(p, "r", encoding="utf-8") as f:
                obj = json.load(f)
            streets = obj.get("streets")
            if not isinstance(streets, dict):
                raise RuntimeError("Invalid address book: missing 'streets' dict")
            cleaned: Dict[str, List[str]] = {}
            for city, arr in streets.items():
                if isinstance(city, str) and isinstance(arr, list):
                    cleaned[city] = [x for x in arr if isinstance(x, str)]
            return cleaned
        except Exception as e:
            last_err = e

    raise RuntimeError(f"Cannot load {path}. Last error: {last_err}")


def _normalize_for_search(s: str) -> str:
    return " ".join(s.lower().split())


def _top_matches(streets: List[str], query: str, limit: int = 25) -> List[str]:
    q = _normalize_for_search(query)
    if not q:
        return []
    return [s for s in streets if q in _normalize_for_search(s)][:limit]


def _chunk_keyboard(items: List[str], page: int, page_size: int) -> ReplyKeyboardMarkup:
    start = page * page_size
    chunk = items[start:start + page_size]

    rows: List[List[str]] = [[s] for s in chunk]
    nav: List[str] = []
    if page > 0:
        nav.append(BTN["back"])
    if start + page_size < len(items):
        nav.append(BTN["next"])
    if nav:
        rows.append(nav)
    rows.append([BTN["cancel"]])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)


def _main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [BTN["status"], BTN["set"]],
            [BTN["interval"], BTN["stop"]],
        ],
        resize_keyboard=True,
    )


def _parse_interval(text: str) -> Optional[int]:
    t = (text or "").strip().lower()
    m = re.match(r"^(\d{1,4})\s*(с|сек|секунд|s|sec)?$", t)
    if m:
        v = int(m.group(1))
        return v if 15 <= v <= 3600 else None

    m = re.match(r"^(\d{1,3})\s*(хв|хвилин|m|min|minute|minutes)$", t)
    if m:
        v = int(m.group(1)) * 60
        return v if 15 <= v <= 3600 else None

    return None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"{EMO['sparkles']} DTEK-бот для перевірки відключень.\n\n"
        f"/set — налаштувати адресу\n"
        f"/status — показати статус\n"
        f"/interval — змінити інтервал перевірки\n"
        f"/stop — вимкнути сповіщення",
        reply_markup=_main_menu_kb(),
    )


async def set_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    book: Dict[str, List[str]] = context.application.bot_data.get("addr_book", {})
    cities = sorted(book.keys(), key=lambda x: x.lower())
    if not cities:
        await update.message.reply_text(f"{EMO['warn']} Довідник адрес порожній.")
        return ConversationHandler.END

    context.user_data["city_page"] = 0
    await update.message.reply_text(
        f"{EMO['city']} Обери населений пункт:",
        reply_markup=_chunk_keyboard(cities, page=0, page_size=15),
    )
    return ASK_CITY


async def on_city(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()

    if text == BTN["cancel"]:
        await update.message.reply_text(f"{EMO['cancel']} Скасовано.", reply_markup=_main_menu_kb())
        return ConversationHandler.END

    book: Dict[str, List[str]] = context.application.bot_data.get("addr_book", {})
    cities = sorted(book.keys(), key=lambda x: x.lower())
    page = int(context.user_data.get("city_page", 0))

    if text == BTN["next"]:
        page = min(page + 1, max(0, (len(cities) - 1) // 15))
        context.user_data["city_page"] = page
        await update.message.reply_text(f"{EMO['city']} Обери населений пункт:", reply_markup=_chunk_keyboard(cities, page, 15))
        return ASK_CITY

    if text == BTN["back"]:
        page = max(0, page - 1)
        context.user_data["city_page"] = page
        await update.message.reply_text(f"{EMO['city']} Обери населений пункт:", reply_markup=_chunk_keyboard(cities, page, 15))
        return ASK_CITY

    if text not in book:
        await update.message.reply_text(f"{EMO['warn']} Обери пункт з клавіатури.")
        return ASK_CITY

    context.user_data["city"] = text
    context.user_data.pop("street", None)

    await update.message.reply_text(
        f"{EMO['search']} Введи частину назви вулиці:",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ASK_STREET_QUERY


async def on_street_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = (update.message.text or "").strip()

    if q == BTN["cancel"]:
        await update.message.reply_text(f"{EMO['cancel']} Скасовано.", reply_markup=_main_menu_kb())
        return ConversationHandler.END

    city = context.user_data.get("city", "")
    book: Dict[str, List[str]] = context.application.bot_data.get("addr_book", {})
    streets = book.get(city) or []

    matches = _top_matches(streets, q, limit=25)
    if not matches:
        await update.message.reply_text(f"{EMO['warn']} Нічого не знайдено. Спробуй іншу частину назви.")
        return ASK_STREET_QUERY

    context.user_data["street_matches"] = matches
    kb = ReplyKeyboardMarkup([[s] for s in matches] + [[BTN["cancel"]]], resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text(f"{EMO['road']} Обери вулицю зі списку:", reply_markup=kb)
    return ASK_STREET_PICK


async def on_street_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()

    if text == BTN["cancel"]:
        await update.message.reply_text(f"{EMO['cancel']} Скасовано.", reply_markup=_main_menu_kb())
        return ConversationHandler.END

    matches: List[str] = context.user_data.get("street_matches") or []
    if text not in matches:
        await update.message.reply_text(f"{EMO['warn']} Обери вулицю з клавіатури.")
        return ASK_STREET_QUERY

    context.user_data["street"] = text
    await update.message.reply_text(f"{EMO['home']} Введи номер будинку:", reply_markup=ReplyKeyboardRemove())
    return ASK_HOUSE


async def _restart_watch_job(context: ContextTypes.DEFAULT_TYPE, user_id: int, chat_id: int, interval: int) -> None:
    for j in context.application.job_queue.get_jobs_by_name(job_name(user_id)):
        j.schedule_removal()

    context.application.job_queue.run_repeating(
        check_job,
        interval=interval,
        first=1,
        name=job_name(user_id),
        data={"user_id": user_id, "chat_id": chat_id},
    )
    log.info("watch job restarted: user_id=%s interval=%s", user_id, interval)


async def on_house(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    city = context.user_data.get("city", "").strip()
    street = context.user_data.get("street", "").strip()
    house = normalize_house(update.message.text)

    if not city or not street:
        await update.message.reply_text(f"{EMO['warn']} Адреса не повна. Використай /set", reply_markup=_main_menu_kb())
        return ConversationHandler.END

    addr = Address(city=city, street=street, house=house)

    try:
        api_json = fetch_dtek(city, street)
        house_obj, ts = extract_house(api_json, house)
        snapshot = make_state_snapshot(house_obj, ts)
        msg = format_status(addr, house_obj, ts)
    except Exception as e:
        log.exception("fetch failed")
        await update.message.reply_text(f"{EMO['cross']} Помилка запиту до DTEK: {e}", reply_markup=_main_menu_kb())
        return ConversationHandler.END

    context.user_data["addr"] = {"city": city, "street": street, "house": house}
    context.user_data["last_state_hash"] = hash_snapshot(snapshot)

    interval = int(context.user_data.get("interval", DEFAULT_CHECK_SECONDS))

    await update.message.reply_text(msg, reply_markup=_main_menu_kb())
    await _restart_watch_job(context, update.effective_user.id, update.effective_chat.id, interval)
    await update.message.reply_text(
        f"{EMO['bell']} Сповіщення увімкнено.\n{EMO['clock']} Перевірка кожні {interval} с.",
        reply_markup=_main_menu_kb(),
    )
    return ConversationHandler.END


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    addr_dict = context.user_data.get("addr")
    if not addr_dict:
        await update.message.reply_text(f"{EMO['warn']} Адресу не задано. Використай /set", reply_markup=_main_menu_kb())
        return

    addr = Address(**addr_dict)
    try:
        api_json = fetch_dtek(addr.city, addr.street)
        house_obj, ts = extract_house(api_json, addr.house)
        msg = format_status(addr, house_obj, ts)
        await update.message.reply_text(msg, reply_markup=_main_menu_kb())
    except Exception as e:
        log.exception("status failed")
        await update.message.reply_text(f"{EMO['cross']} Помилка запиту до DTEK: {e}", reply_markup=_main_menu_kb())


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    jobs = context.application.job_queue.get_jobs_by_name(job_name(user_id))
    for j in jobs:
        j.schedule_removal()
    await update.message.reply_text(
        f"{EMO['stop']} Сповіщення вимкнено." if jobs else f"{EMO['info']} Сповіщення не активні.",
        reply_markup=_main_menu_kb(),
    )


async def interval_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    current = int(context.user_data.get("interval", DEFAULT_CHECK_SECONDS))
    kb = ReplyKeyboardMarkup(
        [
            ["30 с", "60 с", "90 с"],
            ["2 хв", "5 хв", "10 хв"],
            [BTN["cancel"]],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await update.message.reply_text(
        f"{EMO['clock']} Поточний інтервал: {current} с.\n"
        f"Вибери або введи свій (15..3600 с):",
        reply_markup=kb,
    )
    return ASK_INTERVAL


async def on_interval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()

    if text == BTN["cancel"]:
        await update.message.reply_text(f"{EMO['cancel']} Скасовано.", reply_markup=_main_menu_kb())
        return ConversationHandler.END

    v = _parse_interval(text)
    if v is None:
        await update.message.reply_text(f"{EMO['warn']} Невірний формат.")
        return ASK_INTERVAL

    context.user_data["interval"] = v

    addr = context.user_data.get("addr")
    if addr:
        await _restart_watch_job(context, update.effective_user.id, update.effective_chat.id, v)

    await update.message.reply_text(f"{EMO['check']} Інтервал встановлено: {v} с.", reply_markup=_main_menu_kb())
    return ConversationHandler.END


async def check_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = context.job.data["user_id"]
    chat_id = context.job.data["chat_id"]

    user_data = context.application.user_data.get(user_id)
    if not user_data:
        log.info("check_job: no user_data for user_id=%s", user_id)
        return

    addr_dict = user_data.get("addr")
    old_hash = user_data.get("last_state_hash")
    if not addr_dict:
        log.info("check_job: no addr for user_id=%s", user_id)
        return

    addr = Address(**addr_dict)

    try:
        api_json = fetch_dtek(addr.city, addr.street)
        house_obj, ts = extract_house(api_json, addr.house)

        snapshot = make_state_snapshot(house_obj, ts)
        new_hash = hash_snapshot(snapshot)

        log.info(
            "check_job: user_id=%s old_hash=%s new_hash=%s snapshot=%s",
            user_id,
            old_hash,
            new_hash,
            snapshot,
        )

        if new_hash != old_hash:
            user_data["last_state_hash"] = new_hash
            msg = format_status(addr, house_obj, ts)

            await context.bot.send_message(
                chat_id=chat_id,
                text=f"{EMO['refresh']} Оновлення розкладу:\n\n{msg}",
            )
            log.info("check_job: update sent to user_id=%s", user_id)

    except Exception:
        log.exception("watch job failed")


async def on_menu_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    if text == BTN["status"]:
        await status_cmd(update, context)
        return ConversationHandler.END
    if text == BTN["set"]:
        return await set_cmd(update, context)
    if text == BTN["stop"]:
        await stop_cmd(update, context)
        return ConversationHandler.END
    if text == BTN["interval"]:
        return await interval_cmd(update, context)

    return None


def main() -> None:
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit("Set BOT_TOKEN env var")

    addr_book = load_address_book(ADDRESSES_FILE)

    app = ApplicationBuilder().token(token).build()
    app.bot_data["addr_book"] = addr_book

    conv_set = ConversationHandler(
        entry_points=[
            CommandHandler("set", set_cmd),
            MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Regex(rf"^{re.escape(BTN['set'])}$"), set_cmd),
        ],
        states={
            ASK_CITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_city)],
            ASK_STREET_QUERY: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_street_query)],
            ASK_STREET_PICK: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_street_pick)],
            ASK_HOUSE: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_house)],
        },
        fallbacks=[CommandHandler("stop", stop_cmd)],
        allow_reentry=True,
    )

    conv_interval = ConversationHandler(
        entry_points=[
            CommandHandler("interval", interval_cmd),
            MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Regex(rf"^{re.escape(BTN['interval'])}$"), interval_cmd),
        ],
        states={
            ASK_INTERVAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_interval)],
        },
        fallbacks=[CommandHandler("stop", stop_cmd)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv_set)
    app.add_handler(conv_interval)
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_menu_buttons))

    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
