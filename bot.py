import os
import time
import json
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright

# --- CONFIGURATION ---
TARGET_URL = "https://tevis.ekom21.de/fra/select2?md=35"
MY_CURRENT_APPOINTMENT = int(os.getenv("MY_CURRENT_APPOINTMENT", "20260210"))  # YYYYMMDD

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
DEFAULT_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")  # where daily pings + alerts go

STATE_FILE = "state.json"

TIMEZONE = os.getenv("BOT_TIMEZONE", "Europe/Berlin")
HEARTBEAT_HOUR = int(os.getenv("HEARTBEAT_HOUR", "9"))  # 09:xx local time
HEARTBEAT_WINDOW_MINUTES = 10  # run every 5 min -> 10 min window is safe


def tg_api(method: str, params: dict | None = None) -> dict:
    if not BOT_TOKEN:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    r = requests.post(url, data=params or {}, timeout=25)
    if not r.ok:
        raise RuntimeError(f"Telegram API error {r.status_code}: {r.text}")
    return r.json()


def send_telegram(chat_id: str, text: str) -> None:
    if not chat_id:
        print("send_telegram skipped: missing chat_id")
        return
    try:
        tg_api("sendMessage", {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True
        })
    except Exception as e:
        print("Telegram send failed:", e)


def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


def check_for_appointments() -> int | None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            current_time = time.strftime("%H:%M:%S")
            print(f"[{current_time}] Checking page...")

            page.goto(TARGET_URL, wait_until="domcontentloaded")

            if page.is_visible("#cookie_msg_btn_no"):
                page.click("#cookie_msg_btn_no")

            page.wait_for_selector("#inputBox-5635", timeout=15000)
            page.click("#button-plus-5635")
            page.click("#WeiterButton")

            page.wait_for_selector("#TevisDialog", timeout=15000)
            page.click("#TevisDialog .modal-footer .btn-ok")

            page.wait_for_selector("div.suggest_location_single", timeout=15000)
            page.click("#WeiterButton")

            try:
                page.wait_for_selector(".suggestion_form", timeout=7000)
            except Exception:
                print(f"[{current_time}] No slots visible.")
                return None

            date_inputs = page.locator("form.suggestion_form input[name='date']").all()
            found_dates = set()

            for inp in date_inputs:
                val = inp.get_attribute("value")
                if val and val.isdigit():
                    found_dates.add(int(val))

            if not found_dates:
                print(f"[{current_time}] No dates found.")
                return None

            earliest = min(found_dates)
            print(f"[{current_time}] Earliest: {earliest}")
            return earliest

        except Exception as e:
            print("Error:", e)
            return None
        finally:
            browser.close()


def maybe_send_daily_heartbeat(state: dict) -> None:
    if not DEFAULT_CHAT_ID:
        return

    now = datetime.now(ZoneInfo(TIMEZONE))
    today = now.date().isoformat()

    last_ping_date = state.get("last_ping_date")

    # Only send once per day, inside a time window around HEARTBEAT_HOUR
    if last_ping_date == today:
        return

    if now.hour == HEARTBEAT_HOUR and now.minute < HEARTBEAT_WINDOW_MINUTES:
        send_telegram(DEFAULT_CHAT_ID, f"🤖 Still alive ✅ ({now.strftime('%Y-%m-%d %H:%M %Z')})")
        state["last_ping_date"] = today


def process_incoming_commands(state: dict) -> None:
    """
    Poll Telegram updates (since last_update_id), respond to /check,
    and store new offset in state.
    """
    last_update_id = state.get("last_update_id", 0)
    try:
        resp = tg_api("getUpdates", {
            "offset": last_update_id + 1,
            "timeout": 0,
            # Don't restrict allowed_updates here; we want message + /start, etc.
        })
    except Exception as e:
        print("getUpdates failed:", e)
        return

    updates = resp.get("result", [])
    if not updates:
        return

    max_update_id = last_update_id
    for u in updates:
        max_update_id = max(max_update_id, u.get("update_id", max_update_id))

        msg = u.get("message")
        if not msg:
            continue

        chat_id = str(msg["chat"]["id"])
        text = (msg.get("text") or "").strip()

        if not text:
            continue

        # Commands you can send to the bot
        if text.lower() in ("/start", "start"):
            send_telegram(chat_id, "Hi! Send /check to fetch the latest appointment I can see.")
        elif text.lower() in ("/check", "check", "/status", "status"):
            earliest = check_for_appointments()
            if earliest is None:
                send_telegram(chat_id, "I couldn't see any slots right now (or the page didn't load). Try again later.")
            else:
                note = "✅ earlier than your current appointment!" if earliest < MY_CURRENT_APPOINTMENT else "not earlier than your current appointment."
                send_telegram(
                    chat_id,
                    "🗓 Latest check result:\n"
                    f"Earliest: {earliest}\n"
                    f"Current:  {MY_CURRENT_APPOINTMENT}\n"
                    f"Result:   {note}\n"
                    f"Page: {TARGET_URL}"
                )
        # You can add more commands here if you want.

    state["last_update_id"] = max_update_id


def main():
    if not BOT_TOKEN:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")

    state = load_state()

    # 1) Reply to any Telegram messages since last run (e.g., /check)
    process_incoming_commands(state)

    # 2) Daily heartbeat ping (to DEFAULT_CHAT_ID)
    maybe_send_daily_heartbeat(state)

    # 3) Scheduled “alert” behavior: notify when an earlier date appears
    earliest = check_for_appointments()
    if earliest is not None and earliest < MY_CURRENT_APPOINTMENT:
        last_notified = state.get("last_notified_earliest")
        if str(earliest) != str(last_notified):
            if DEFAULT_CHAT_ID:
                send_telegram(
                    DEFAULT_CHAT_ID,
                    "✅ Earlier appointment found!\n"
                    f"Earliest: {earliest}\n"
                    f"Current:  {MY_CURRENT_APPOINTMENT}\n"
                    f"{TARGET_URL}"
                )
            state["last_notified_earliest"] = earliest

    save_state(state)


if __name__ == "__main__":
    main()
