import json
import os
import re
import ssl
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter

from ocr import solve_image

DOWN_ALERT_AFTER = timedelta(hours=1)
LOCAL_TZ = ZoneInfo("Asia/Kolkata")

# ============================================================
# JOURNEY CONFIGURATION
# ============================================================

TRAIN_NO = "07097 - HYB MAQ SPL"
TRAIN_NAME = "HYB MAQ SPL"
SOURCE = "ERODE JN. - ED"
DESTINATION = "KANHANGAD - KZE"
CLASS_CODE = "3A"
QUOTA = "GN"

# Date format used when sending the request to Indian Railways
JOURNEY_DATE = "18-11-2026"
# Date format used inside the Railway JSON response
TARGET_DATE = "2026-11-18"

# Alert when availability falls by this much or more
# from the last notification baseline
DROP_THRESHOLD = 10

# When available seats are at or below this number,
# alert on EVERY single drop
LOW_SEAT_LIMIT = 10

# ============================================================
# LOCAL FILES
# ============================================================

STATE_FILE = Path("state.json")
DOWNTIME_FILE = Path("downtime.json")
CAPTCHA_FILE = Path("captcha.png")
RESPONSE_FILE = Path("last_response.json")

# ============================================================
# INDIAN RAILWAYS URLs
# ============================================================

BASE_URL = "https://www.indianrail.gov.in/enquiry"
SEAT_PAGE_URL = f"{BASE_URL}/SEAT/SeatAvailability.html?locale=en"
CAPTCHA_CONFIG_URL = f"{BASE_URL}/CaptchaConfig"
CAPTCHA_IMAGE_URL = f"{BASE_URL}/captchaDraw.png"
COMMON_CAPTCHA_URL = f"{BASE_URL}/CommonCaptcha"


# ============================================================
# MAINTENANCE DETECTION
# ============================================================

class SiteDownError(Exception):
    """Railway site is in maintenance / not serving enquiries."""


# Server messages that mean maintenance (lowercase substrings)
MAINTENANCE_MARKERS = ("facnam", "facnum")


# ============================================================
# HTTP SESSION
# ============================================================

class LegacyTLSAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.set_ciphers("DEFAULT@SECLEVEL=1")
        ctx.options |= 0x4  # allow legacy server connect
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


def create_session():
    """One HTTP session so cookies are reused across requests."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/140.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    })
    session.mount("https://", LegacyTLSAdapter())
    return session


# ============================================================
# STATE MANAGEMENT
# ============================================================

def load_state():
    """Load state.json, or create an empty baseline on first run."""
    if not STATE_FILE.exists():
        return {
            "last_available": None,
            "last_notified": None,
            "history": [],
        }
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


# ============================================================
# GET CAPTCHA
# ============================================================

def get_captcha(session):
    """Load the Railway page and download its CAPTCHA image."""
    response = session.get(SEAT_PAGE_URL, timeout=30)
    response.raise_for_status()

    session.get(CAPTCHA_CONFIG_URL, timeout=30)

    # Timestamp avoids getting a cached CAPTCHA image
    params = {"_": int(time.time() * 1000)}
    response = session.get(CAPTCHA_IMAGE_URL, params=params, timeout=30)
    response.raise_for_status()

    CAPTCHA_FILE.write_bytes(response.content)


# ============================================================
# REQUEST AVAILABILITY
# ============================================================

def request_availability(session, captcha):
    """Send the solved CAPTCHA + journey details to Railway."""
    params = {
        "inputCaptcha": captcha,
        "trainNo": TRAIN_NO,
        "dt": JOURNEY_DATE,
        "sourceStation": SOURCE,
        "destinationStation": DESTINATION,
        "classc": CLASS_CODE,
        "quota": QUOTA,
        "inputPage": "SEAT",
        "language": "en",
    }

    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Referer": SEAT_PAGE_URL,
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }

    response = session.get(
        COMMON_CAPTCHA_URL,
        params=params,
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()

    data = response.json()

    # Keep the raw response for debugging
    with open(RESPONSE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    if data.get("errorMessage"):
        msg = data["errorMessage"]
        if any(m in msg.lower() for m in MAINTENANCE_MARKERS):
            raise SiteDownError(msg)
        raise RuntimeError(msg)

    return data


# ============================================================
# EXTRACT AVAILABLE SEATS
# ============================================================

def get_available(response):
    """
    Find TARGET_DATE in avlDayList and extract the number from
    a status like "AVAILABLE-0187" -> 187.
    Returns None if the status is not a normal AVAILABLE value.
    """
    if response.get("errorMessage"):
        raise RuntimeError(response["errorMessage"])

    for item in response.get("avlDayList", []):
        if item.get("availablityDate") != TARGET_DATE:
            continue

        status = item.get("availablityStatus", "")
        match = re.search(r"AVAILABLE-(\d+)", status)
        if match:
            return int(match.group(1))

        return None

    raise RuntimeError("Target date not found in response.")


# ============================================================
# TELEGRAM NOTIFICATION
# ============================================================

def send_telegram(message):
    """Send an alert via Telegram (credentials from env variables)."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    response = requests.post(
        url,
        data={"chat_id": chat_id, "text": message},
        timeout=30,
    )
    response.raise_for_status()


# ============================================================
# ALERT LOGIC
# ============================================================

def is_big_drop(previous, current):
    """True if seats fell by DROP_THRESHOLD or more since the last notification."""
    return previous is not None and previous - current >= DROP_THRESHOLD


def is_low_seat_drop(last_seen, current):
    """True if seats are <= LOW_SEAT_LIMIT and went down since the last check."""
    return (
        last_seen is not None
        and current <= LOW_SEAT_LIMIT
        and current < last_seen
    )


def build_alert_message(reason, current, drop):
    """Build the Telegram message text."""
    return (
        "🚆 Indian Railways Seat Alert\n"
        f"{reason}\n\n"
        f"Train: {TRAIN_NO} - {TRAIN_NAME}\n"
        f"Route: {SOURCE} → {DESTINATION}\n"
        "Date: 18 Nov 2026\n"
        f"Class: {CLASS_CODE}\n"
        f"Quota: {QUOTA}\n\n"
        f"Available: {current}\n"
        f"Drop: {drop}"
    )


def check_and_notify(state, current):
    """
    Decide whether to alert, send it, and update last_notified.
    The low-seat rule takes priority over the big-drop rule.
    Must run BEFORE update_history so last_available is still the old value.
    """
    previous = state.get("last_notified")
    last_seen = state.get("last_available")

    if is_low_seat_drop(last_seen, current):
        drop = last_seen - current
        reason = f"⚠️ LOW SEATS - only {current} left"
    elif is_big_drop(previous, current):
        drop = previous - current
        reason = "Big drop"
    else:
        if previous is None:  # first run: set baseline, no alert
            state["last_notified"] = current
        return

    send_telegram(build_alert_message(reason, current, drop))
    state["last_notified"] = current


def update_history(state, current):
    """Record the latest availability. Returns True if it changed."""
    if current == state.get("last_available"):
        return False

    state["last_available"] = current
    state.setdefault("history", []).append({
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "available": current,
    })
    state["history"] = state["history"][-100:]
    return True


# ============================================================
# DOWNTIME TRACKING (separate file: downtime.json)
# ============================================================

def load_downtime():
    dt = {"down_since": None, "alerted": False, "history": []}
    if DOWNTIME_FILE.exists():
        try:
            s = json.loads(DOWNTIME_FILE.read_text(encoding="utf-8"))
            dt["down_since"] = s.get("down_since")
            dt["alerted"] = bool(s.get("alerted", False))
            dt["history"] = s.get("history", [])
        except ValueError:
            pass  # corrupt file -> start clean
    return dt


def save_downtime(dt):
    DOWNTIME_FILE.write_text(json.dumps(dt, indent=2), encoding="utf-8")


def fmt_local(d):
    return d.astimezone(LOCAL_TZ).strftime("%d %b %Y, %I:%M %p IST")


def fmt_duration(delta):
    h, m = divmod(int(delta.total_seconds() // 60), 60)
    return f"{h}h {m}m" if h else f"{m}m"


def record_down(error):
    """Site unreachable or in maintenance. Alert only after 1 hour."""
    now = datetime.now(timezone.utc)
    dt = load_downtime()

    if not dt["down_since"]:
        dt["down_since"] = now.isoformat()
        save_downtime(dt)
        return

    since = datetime.fromisoformat(dt["down_since"])
    down_for = now - since
    if dt["alerted"] or down_for < DOWN_ALERT_AFTER:
        return

    try:
        send_telegram(
            "🔴 RAILWAY ENQUIRY SITE DOWN\n\n"
            f"⏱ Down for {fmt_duration(down_for)} (since {fmt_local(since)})\n"
            f"❗ Last error: {str(error)[:200]}"
        )
    except Exception as exc:
        print(f"Could not send down alert: {exc}")
        return  # alerted stays False -> retried next run

    dt["alerted"] = True
    save_downtime(dt)


def record_up():
    """Site responded. Log the outage; send 'back online' only if we alerted."""
    dt = load_downtime()
    if not dt["down_since"]:
        return

    now = datetime.now(timezone.utc)
    since = datetime.fromisoformat(dt["down_since"])

    if dt["alerted"]:
        try:
            send_telegram(
                "🟢 RAILWAY ENQUIRY SITE BACK ONLINE\n\n"
                f"⏱ Total downtime: {fmt_duration(now - since)}\n"
                f"🕒 Recovered at {fmt_local(now)}"
            )
        except Exception as exc:
            print(f"Could not send recovery alert: {exc}")
            return  # keep state -> retried next run

    dt["history"].append({
        "start": dt["down_since"],
        "end": now.isoformat(),
        "minutes": int((now - since).total_seconds() // 60),
        "alerted": dt["alerted"],
    })
    dt["history"] = dt["history"][-100:]
    dt["down_since"] = None
    dt["alerted"] = False
    save_downtime(dt)


# ============================================================
# FETCH CURRENT SEATS
# ============================================================

def fetch_current(attempts=3):
    """
    Get the current seat count. Retries on CAPTCHA/OCR failures.
    Network errors and maintenance (SiteDownError) are raised
    immediately, since retrying won't help if the site is down.
    """
    last_err = None
    for i in range(attempts):
        try:
            session = create_session()
            get_captcha(session)
            captcha = solve_image("captcha.png")
            if not captcha:
                raise ValueError("CAPTCHA cannot be empty.")
            response = request_availability(session, captcha)
            return get_available(response)
        except (requests.RequestException, SiteDownError):
            raise
        except Exception as e:
            last_err = e
            print(f"Attempt {i + 1}/{attempts} failed: {e}")
    raise last_err


# ============================================================
# MAIN BOT
# ============================================================

def main():
    reply_only = os.getenv("MODE") == "reply"
    state = load_state()

    try:
        current = fetch_current()
    except (requests.RequestException, SiteDownError) as e:
        print(f"Railway site unreachable/maintenance: {e}")
        if reply_only:
            send_telegram("⚠️ Railway site isn't responding right now (maintenance?). Try again in a bit.")
        else:
            record_down(e)
        return
    except Exception as e:
        print(f"Check failed: {e}")
        if reply_only:
            send_telegram(f"⚠️ Couldn't get seats: {e}")
            return
        record_up()  # site answered, so it's up; just a CAPTCHA/parse problem
        raise

    # Site responded
    if not reply_only:
        record_up()

    if reply_only:
        shown = current if current is not None else "no numeric availability right now"
        send_telegram(
            f"🚆 {TRAIN_NO}\n"
            f"{SOURCE} → {DESTINATION}\n"
            f"18 Nov 2026 · {CLASS_CODE} · {QUOTA}\n\n"
            f"Available now: {shown}"
        )
        return  # reply mode never touches state.json

    if current is None:
        print("Availability is not currently AVAILABLE.")
        return

    check_and_notify(state, current)

    update_history(state, current)
    save_state(state)  # always save, so last_notified is never lost

    print(current)


# ============================================================
# START PROGRAM
# ============================================================

if __name__ == "__main__":
    main()
