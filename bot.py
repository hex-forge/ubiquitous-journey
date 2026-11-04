import hashlib
import json
import os
from pathlib import Path

import requests

from urllib.parse import quote, urlsplit, urlunsplit

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

FILES_BASE = "https://store.iiitt.ac.in/downloads/"

NOTICES_URL = "https://iiitt.ac.in/json/general/notices.json"
SEEN_FILE = Path("seen_notices.json")

HEALTH_FILE = Path("health_state.json")
DOWN_THRESHOLD = timedelta(hours=1)
LOCAL_TZ = ZoneInfo("Asia/Kolkata")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]


def get_notices():
    response = requests.get(
        NOTICES_URL,
        timeout=30,
        headers={"User-Agent": "hello_world"},
    )
    response.raise_for_status()

    return response.json().get("data", [])


def get_notice_id(notice):
    text = "|".join([
        notice.get("title", ""),
        notice.get("date", ""),
        notice.get("link", ""),
    ])

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_seen():
    if not SEEN_FILE.exists():
        return set()

    return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))


def save_seen(seen):
    SEEN_FILE.write_text(
        json.dumps(sorted(seen), indent=2),
        encoding="utf-8",
    )



def make_url(link):
    link = link.strip()

    if link.startswith("http"):
        # Absolute URL: just encode the path (spaces etc.), keep it otherwise intact
        parts = urlsplit(link)
        return urlunsplit(
            (parts.scheme, parts.netloc, quote(parts.path, safe="/%"), parts.query, parts.fragment)
        )

    # Relative path: files are served from the store subdomain
    return FILES_BASE + quote(link.lstrip("/"), safe="/%")

def send_telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    response = requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "text": message,
            "disable_web_page_preview": False,
        },
        timeout=30,
    )

    response.raise_for_status()
def load_health():
    state = {"down_since": None, "alerted": False, "history": []}
    if HEALTH_FILE.exists():
        try:
            s = json.loads(HEALTH_FILE.read_text(encoding="utf-8"))
            state["down_since"] = s.get("down_since")
            state["alerted"] = bool(s.get("alerted", False))
            state["history"] = s.get("history", [])
        except ValueError:
            pass
    return state


def save_health(state):
    HEALTH_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def fmt_local(dt):
    return dt.astimezone(LOCAL_TZ).strftime("%d %b %Y, %I:%M %p IST")


def fmt_duration(delta):
    h, m = divmod(int(delta.total_seconds() // 60), 60)
    return f"{h}h {m}m" if h else f"{m}m"


def handle_failure(error):
    now = datetime.now(timezone.utc)
    state = load_health()

    if not state["down_since"]:
        state["down_since"] = now.isoformat()
        save_health(state)
        return

    down_since = datetime.fromisoformat(state["down_since"])
    down_for = now - down_since
    if state["alerted"] or down_for < DOWN_THRESHOLD:
        return

    try:
        send_telegram(
            "🚨 IIIT NOTICES ENDPOINT DOWN\n\n"
            f"⏱ Down for {fmt_duration(down_for)} (since {fmt_local(down_since)})\n"
            f"❗ Last error: {str(error)[:200]}"
        )
    except Exception as exc:
        print(f"Could not send down alert: {exc}")
        return
    state["alerted"] = True
    save_health(state)


def handle_recovery():
    state = load_health()
    if not state["down_since"]:
        return

    now = datetime.now(timezone.utc)
    down_since = datetime.fromisoformat(state["down_since"])

    if state["alerted"]:
        try:
            send_telegram(
                "✅ IIIT NOTICES ENDPOINT BACK ONLINE\n\n"
                f"⏱ Total downtime: {fmt_duration(now - down_since)}\n"
                f"🕒 Recovered at {fmt_local(now)}"
            )
        except Exception as exc:
            print(f"Could not send recovery alert: {exc}")
            return

    state["history"].append({
        "start": state["down_since"],
        "end": now.isoformat(),
        "minutes": int((now - down_since).total_seconds() // 60),
        "alerted": state["alerted"],
    })
    state["history"] = state["history"][-100:]  # keep the last 100 outages
    state["down_since"] = None
    state["alerted"] = False
    save_health(state)

def main():
    try:
        notices = get_notices()
    except Exception as exc:
        print(f"Endpoint check failed: {exc}")
        handle_failure(exc)
        return

    handle_recovery()
    seen = load_seen()

    current_ids = set()
    new_notices = []

    for notice in notices:
        notice_id = get_notice_id(notice)
        current_ids.add(notice_id)

        if notice_id not in seen:
            new_notices.append(notice)

    # First run = establish baseline.
    if not SEEN_FILE.exists() or not seen:
        save_seen(current_ids)
        print(f"Baseline created with {len(current_ids)} notices.")
        return

    if not new_notices:
        print("No new notices.")
        return

    for notice in reversed(new_notices):
        title = notice.get("title", "New notice")
        date = notice.get("date", "")
        link = make_url(notice.get("link", ""))

        message = (
            "🔔 NEW IIIT TRICHY NOTICE\n\n"
            f"📢 {title}\n\n"
            f"📅 {date}\n"
            f"📄 {link}"
        )

        send_telegram(message)
        print(f"Sent: {title}")

    save_seen(current_ids)


if __name__ == "__main__":
    main()
