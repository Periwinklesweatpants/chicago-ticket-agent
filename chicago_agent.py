#!/usr/bin/env python3
"""
chicago_agent.py  —  Chicago Musical Ticket Alert Agent
Ticketmaster Discovery API — Final Version

HOW IT WORKS:
  Ticketmaster's free API finds the correct show dates but does not return
  pricing data (priceRanges=None). Rather than filter by price in the API,
  the agent alerts you once per day with direct links to every show date in
  your window (Apr 5-10). You click through to Ticketmaster, set quantity
  to 3, sort by price, and check if seats meet your budget.

  Deduplication: you only get ONE email per show date (not one every 30 min).
  The agent tracks which event IDs it has already alerted you about.
"""

import json
import logging
import os
import smtplib
import sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

try:
    import requests
except ImportError:
    sys.exit("ERROR: 'requests' not installed. Check requirements.txt.")

# ══════════════════════════════════════════════════════════════
#  CREDENTIALS  — from GitHub Secrets
# ══════════════════════════════════════════════════════════════

def _require(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        sys.exit(
            f"ERROR: Required secret '{name}' is not set.\n"
            f"Go to: GitHub repo → Settings → Secrets and variables → Actions"
        )
    return val

TM_API_KEY        = _require("TICKETMASTER_API_KEY")
SMTP_SENDER_EMAIL = _require("SMTP_SENDER_EMAIL")
SMTP_APP_PASSWORD = _require("SMTP_APP_PASSWORD")
ALERT_EMAIL       = _require("ALERT_EMAIL")

# ══════════════════════════════════════════════════════════════
#  SEARCH CRITERIA  — edit these to change dates or budget note
# ══════════════════════════════════════════════════════════════

DATE_FROM        = "2026-04-05"
DATE_TO          = "2026-04-10"
TICKETS_NEEDED   = 3
MAX_PRICE        = 200.00    # shown in email as a reminder to you — not filtered by API

# ── Confirmed Ticketmaster IDs (from diagnostic run 2026-03-11) ───────────────
ATTRACTION_ID = "K8vZ9179Ip7"   # Chicago The Musical (NY) at Ambassador Theatre
VENUE_ID      = "Zkr9jZkAeP"    # Ambassador Theatre-NY

# ══════════════════════════════════════════════════════════════
#  PERSISTENCE
# ══════════════════════════════════════════════════════════════

SEEN_FILE = "seen_listings.json"

def load_seen() -> set:
    if not os.path.exists(SEEN_FILE):
        return set()
    try:
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()

def save_seen(seen: set) -> None:
    with open(SEEN_FILE, "w") as f:
        json.dump(sorted(seen), f, indent=2)

# ══════════════════════════════════════════════════════════════
#  LOGGING
# ══════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("agent")

# ══════════════════════════════════════════════════════════════
#  TICKETMASTER API
# ══════════════════════════════════════════════════════════════

TM_BASE = "https://app.ticketmaster.com/discovery/v2"

def fetch_chicago_events() -> list:
    """
    Fetch all Chicago the Musical show dates in the target window
    using the confirmed attraction and venue IDs.
    """
    params = {
        "apikey":        TM_API_KEY,
        "attractionId":  ATTRACTION_ID,
        "venueId":       VENUE_ID,
        "startDateTime": f"{DATE_FROM}T00:00:00Z",
        "endDateTime":   f"{DATE_TO}T23:59:59Z",
        "size":          20,
        "sort":          "date,asc",
    }
    try:
        r = requests.get(f"{TM_BASE}/events.json", params=params, timeout=15)
        log.info("Ticketmaster API status: %s", r.status_code)

        if r.status_code == 401:
            log.error("401 Unauthorized — check TICKETMASTER_API_KEY secret.")
            return []
        if r.status_code == 429:
            log.error("429 Rate limited — will retry next scheduled run.")
            return []

        r.raise_for_status()
        data   = r.json()
        events = data.get("_embedded", {}).get("events", [])
        total  = data.get("page", {}).get("totalElements", len(events))
        log.info("Events found: %d (API total: %s)", len(events), total)
        return events

    except requests.RequestException as e:
        log.error("Ticketmaster request failed: %s", e)
        return []


def parse_event(ev: dict) -> dict:
    """Extract the fields we need from a raw Ticketmaster event."""
    dates      = ev.get("dates", {})
    start      = dates.get("start", {})
    status     = dates.get("status", {}).get("code", "").lower()
    venues     = ev.get("_embedded", {}).get("venues", [{}])

    return {
        "id":         ev.get("id", ""),
        "name":       ev.get("name", "Chicago - The Musical"),
        "url":        ev.get("url", "https://www.ticketmaster.com"),
        "date":       start.get("localDate", ""),
        "time":       start.get("localTime", ""),
        "status":     status,
        "venue":      venues[0].get("name", "Ambassador Theatre-NY"),
    }

# ══════════════════════════════════════════════════════════════
#  EMAIL
# ══════════════════════════════════════════════════════════════

def _format_dt(date: str, time: str) -> str:
    try:
        dt = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%A, %B %-d, %Y at %-I:%M %p")
    except Exception:
        return f"{date} {time}".strip()


def _event_row_html(ev: dict) -> str:
    display_dt = _format_dt(ev["date"], ev["time"])
    status_badge = ""
    if ev["status"] not in ("onsale", ""):
        status_badge = (
            f' <span style="background:#cc0000;color:#fff;padding:2px 6px;'
            f'border-radius:3px;font-size:11px;">{ev["status"].upper()}</span>'
        )

    return f"""
    <tr style="border-bottom:1px solid #eee;">
      <td style="padding:12px;">{display_dt}{status_badge}</td>
      <td style="padding:12px;">{ev['venue']}</td>
      <td style="padding:12px;text-align:center;">
        <a href="{ev['url']}"
           style="background:#C41E3A;color:#fff;padding:9px 18px;
                  border-radius:4px;text-decoration:none;font-weight:bold;">
          Check Prices →
        </a>
      </td>
    </tr>"""


def build_email_html(events: list) -> str:
    rows  = "\n".join(_event_row_html(ev) for ev in events)
    count = len(events)

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:Arial,sans-serif;max-width:820px;margin:auto;padding:20px;color:#222;">

  <div style="background:#C41E3A;padding:22px;border-radius:8px 8px 0 0;text-align:center;">
    <h1 style="color:#fff;margin:0;font-size:26px;">🎭 Chicago the Musical — Ticket Alert!</h1>
    <p style="color:#ffd0d0;margin:8px 0 0;">
      {count} show date{'s' if count != 1 else ''} available — April 5–10, 2026
    </p>
  </div>

  <div style="background:#fff8f8;padding:14px;border:1px solid #f0d0d0;font-size:14px;">
    <strong>Your search:</strong>
    &nbsp; Dates: April 5–10, 2026
    &nbsp;|&nbsp; Budget: up to ${MAX_PRICE:.0f}/ticket
    &nbsp;|&nbsp; Seats needed: {TICKETS_NEEDED} together
    &nbsp;|&nbsp; Ambassador Theatre, NYC
  </div>

  <table style="width:100%;border-collapse:collapse;margin-top:16px;font-size:14px;">
    <thead>
      <tr style="background:#f5f5f5;font-weight:bold;">
        <th style="padding:12px;text-align:left;">Show Date &amp; Time</th>
        <th style="padding:12px;text-align:left;">Venue</th>
        <th style="padding:12px;text-align:center;">Action</th>
      </tr>
    </thead>
    <tbody>
      {rows}
    </tbody>
  </table>

  <div style="margin-top:20px;padding:16px;background:#fffbe6;
              border:1px solid #ffe099;border-radius:6px;font-size:13px;line-height:1.6;">
    <strong>💡 How to find seats under ${MAX_PRICE:.0f}:</strong><br>
    1. Click <strong>Check Prices</strong> for the date you want<br>
    2. On Ticketmaster, set quantity to <strong>{TICKETS_NEEDED} tickets</strong><br>
    3. Sort by <strong>Price: Low to High</strong><br>
    4. Skip any seats labeled <em>Obstructed View</em>, <em>Limited View</em>,
       or <em>Standing Room</em><br>
    5. Use the interactive seat map to confirm you're not in the last few rows
  </div>

  <div style="margin-top:12px;padding:14px;background:#f0f4ff;
              border:1px solid #c0d0f0;border-radius:6px;font-size:13px;">
    <strong>ℹ️ About pricing:</strong> Ticketmaster shows all-in pricing (fees included)
    on the seat selection screen. The prices you see after clicking are the true final prices.
  </div>

  <p style="margin-top:18px;font-size:11px;color:#aaa;text-align:center;">
    Sent by your Chicago Ticket Agent · Runs every 30 min, 7AM–11PM ET ·
    <em>You always buy manually — this agent never purchases on your behalf.</em>
  </p>

</body>
</html>"""


def send_alert(events: list) -> bool:
    subject = (
        f"🎭 Chicago Musical — {len(events)} show date"
        f"{'s' if len(events) > 1 else ''} available Apr 5–10!"
    )
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SMTP_SENDER_EMAIL
    msg["To"]      = ALERT_EMAIL
    msg.attach(MIMEText(build_email_html(events), "html"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as s:
            s.ehlo()
            s.starttls()
            s.login(SMTP_SENDER_EMAIL, SMTP_APP_PASSWORD)
            s.sendmail(SMTP_SENDER_EMAIL, ALERT_EMAIL, msg.as_string())
        log.info("✅ Alert email sent to %s.", ALERT_EMAIL)
        return True
    except smtplib.SMTPAuthenticationError:
        log.error("❌ Gmail auth failed. Check SMTP_APP_PASSWORD secret.")
    except Exception as e:
        log.error("❌ Email error: %s", e)
    return False

# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════

def main():
    log.info("=" * 55)
    log.info("Chicago Ticket Agent — Ticketmaster edition")
    log.info("Window: %s → %s  |  Budget reminder: $%.2f", DATE_FROM, DATE_TO, MAX_PRICE)

    # 1. Fetch events
    raw_events = fetch_chicago_events()
    if not raw_events:
        log.info("No events returned. Done.")
        return

    # 2. Parse all events, skip cancelled/postponed
    parsed = []
    for raw in raw_events:
        ev = parse_event(raw)
        if ev["status"] in ("cancelled", "postponed"):
            log.info("Skipping %s — status: %s", ev["date"], ev["status"])
            continue
        log.info("Found: '%s' on %s at %s (id=%s)", ev["name"], ev["date"], ev["time"], ev["id"])
        parsed.append(ev)

    if not parsed:
        log.info("No valid events after status filter. Done.")
        return

    # 3. Deduplicate by event ID — only alert once per show date
    #    (Reset seen_listings.json if you want a fresh alert)
    seen      = load_seen()
    new_events = [ev for ev in parsed if ev["id"] not in seen]

    if not new_events:
        log.info("All %d event(s) already alerted. No email sent.", len(parsed))
        log.info("(Delete seen_listings.json from the repo to reset and re-alert.)")
        return

    log.info("%d new event(s) to alert about.", len(new_events))

    # 4. Send alert
    if send_alert(new_events):
        seen.update(ev["id"] for ev in new_events)
        save_seen(seen)

    log.info("Run complete.")


if __name__ == "__main__":
    main()
