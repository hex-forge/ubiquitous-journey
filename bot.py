import hashlib
import json
import os
from pathlib import Path

import requests


NOTICES_URL = "https://iiitt.ac.in/json/general/notices.json"
SEEN_FILE = Path("seen_notices.json")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]


def get_notices():
    response = requests.get(
        NOTICES_URL,
        timeout=30,
        headers={"User-Agent": "IIIT-Trichy-Notice-Bot/1.0"},
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
    if link.startswith("http"):
        return link

    return "https://iiitt.ac.in/" + link.lstrip("/")


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


def main():
    notices = get_notices()
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