#!/usr/bin/env python3
"""
chicago_agent.py  —  Chicago Musical Ticket Alert Agent
Ticketmaster Discovery API edition.

HOW IT WORKS:
  Ticketmaster's free public API returns priceRanges (min/max) per show date,
  not individual seat listings. So the agent alerts you when ANY tickets for
  a matching show date are available at or below your MAX_PRICE, then you
  click through to Ticketmaster to choose your specific seats.

  Each GitHub Actions run is single-shot: search → filter → email → exit.
  The workflow file calls this on a schedule.

SETUP:
  1. Get a free Ticketmaster API key at: https://developer-acct.ticketmaster.com/
  2. Add it as a GitHub Secret named TICKETMASTER_API_KEY
  3. Add your Gmail secrets (see README)
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
#  CREDENTIALS  — injected from GitHub Secrets at runtime
# ══════════════════════════════════════════════════════════════

def _require(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        sys.exit(
            f"ERROR: Required secret '{name}' is not set.\n"
            f"Go to: GitHub repo → Settings → Secrets and variables → Actions → New repository secret"
        )
    return val

TM_API_KEY         = _require("TICKETMASTER_API_KEY")
SMTP_SENDER_EMAIL  = _require("SMTP_SENDER_EMAIL")
SMTP_APP_PASSWORD  = _require("SMTP_APP_PASSWORD")
ALERT_EMAIL        = _require("ALERT_EMAIL")

# ══════════════════════════════════════════════════════════════
#  SEARCH CRITERIA  — edit these to change dates / budget
# ══════════════════════════════════════════════════════════════

DATE_FROM      = "2026-04-05"   # YYYY-MM-DD, inclusive
DATE_TO        = "2026-04-10"   # YYYY-MM-DD, inclusive
MAX_PRICE      = 200.00         # alert if ANY ticket is at or below this price
TICKETS_NEEDED = 3              # used in the alert email as a reminder to you

# ══════════════════════════════════════════════════════════════
#  PERSISTENCE  — tracks which (event_id, price) pairs were alerted
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
    Search Ticketmaster for Chicago the Musical at Ambassador Theatre, NYC.

    Key parameters:
      keyword          = "Chicago"  (the show name)
      classificationName = "Arts & Theatre"  (Broadway segment)
      venueId          = Ambassador Theatre's Ticketmaster venue ID
      startDateTime / endDateTime = our date window
      city + stateCode = New York, NY (backup filter)

    Returns a list of event dicts from Ticketmaster.
    """

     # Ambassador Theatre Ticketmaster venue ID (from ticketmaster.com/ambassador-theatre-tickets-new-york/venue/237850)
    # Artist ID for Chicago the Musical NY  (from ticketmaster.com/chicago-the-musical-ny-tickets/artist/2503066)
    AMBASSADOR_VENUE_ID   = "237850"
    CHICAGO_ATTRACTION_ID = "2503066"

    params = {
        "apikey":        TM_API_KEY,
        "attractionId":  CHICAGO_ATTRACTION_ID,   # targets this exact show, not just keyword
        "venueId":       AMBASSADOR_VENUE_ID,      # pins it to Ambassador Theatre, NYC
        "startDateTime": f"{DATE_FROM}T00:00:00Z",
        "endDateTime":   f"{DATE_TO}T23:59:59Z",
        "countryCode":   "US",
        "size":          20,
        "sort":          "date,asc",
    }

    try:
        r = requests.get(f"{TM_BASE}/events.json", params=params, timeout=15)
        log.info("Ticketmaster API status: %s", r.status_code)

        if r.status_code == 401:
            log.error("401 Unauthorized — check your TICKETMASTER_API_KEY secret.")
            return []
        if r.status_code == 429:
            log.error("429 Rate limited — too many requests. Will retry next scheduled run.")
            return []

        r.raise_for_status()
        data = r.json()

    except requests.RequestException as e:
        log.error("Ticketmaster request failed: %s", e)
        return []

    # Ticketmaster wraps results in _embedded
    embedded = data.get("_embedded", {})
    events   = embedded.get("events", [])
    total    = data.get("page", {}).get("totalElements", len(events))
    log.info("Ticketmaster: %d event(s) found (total in API: %s).", len(events), total)

    return events


def parse_event(event: dict) -> dict | None:
    """
    Extract the fields we care about from a raw Ticketmaster event.
    Returns None if the event doesn't have price info (not yet on sale).
    """
    event_id  = event.get("id", "")
    name      = event.get("name", "")
    url       = event.get("url", "https://ticketmaster.com")

    # Date/time
    dates     = event.get("dates", {})
    start     = dates.get("start", {})
    local_date = start.get("localDate", "")
    local_time = start.get("localTime", "")
    status    = dates.get("status", {}).get("code", "").lower()

    # Venue
    venues    = event.get("_embedded", {}).get("venues", [{}])
    venue_name = venues[0].get("name", "")

    # Price ranges — Ticketmaster returns min/max for the event
    price_ranges = event.get("priceRanges", [])
    if not price_ranges:
        log.info("  Event '%s' (%s) has no price data yet — skipping.", name, local_date)
        return None

    min_price = price_ranges[0].get("min", 9999)
    max_price = price_ranges[0].get("max", 9999)
    currency  = price_ranges[0].get("currency", "USD")

    return {
        "id":         event_id,
        "name":       name,
        "url":        url,
        "date":       local_date,
        "time":       local_time,
        "status":     status,
        "venue":      venue_name,
        "min_price":  min_price,
        "max_price":  max_price,
        "currency":   currency,
    }


def meets_criteria(ev: dict) -> bool:
    """
    Return True if the event has tickets available at or below MAX_PRICE.
    Ticketmaster min_price is the lowest ticket price including fees
    (Ticketmaster shows all-in pricing by default).
    """
    # Skip cancelled / postponed shows
    if ev["status"] in ("cancelled", "postponed", "rescheduled"):
        log.info("  Skipping %s — status: %s", ev["date"], ev["status"])
        return False

    # The minimum price must be at or below our ceiling
    if ev["min_price"] <= MAX_PRICE:
        log.info(
            "  ✅ MATCH: %s at %s — from $%.2f (max $%.2f)",
            ev["date"], ev["venue"], ev["min_price"], ev["max_price"]
        )
        return True

    log.info(
        "  ✗ Too expensive: %s — cheapest $%.2f (limit $%.2f)",
        ev["date"], ev["min_price"], MAX_PRICE
    )
    return False

# ══════════════════════════════════════════════════════════════
#  EMAIL
# ══════════════════════════════════════════════════════════════

def _event_row_html(ev: dict) -> str:
    try:
        dt = datetime.strptime(f"{ev['date']} {ev['time']}", "%Y-%m-%d %H:%M:%S")
        display_dt = dt.strftime("%A, %B %-d, %Y at %-I:%M %p")
    except Exception:
        display_dt = f"{ev['date']} {ev['time']}".strip()

    return f"""
    <tr style="border-bottom:1px solid #eee;">
      <td style="padding:12px;">{display_dt}</td>
      <td style="padding:12px;">{ev['venue']}</td>
      <td style="padding:12px;text-align:center;font-weight:bold;color:#2a7a2a;">
        from ${ev['min_price']:.2f}
      </td>
      <td style="padding:12px;text-align:center;color:#555;">
        up to ${ev['max_price']:.2f}
      </td>
      <td style="padding:12px;text-align:center;">
        <a href="{ev['url']}"
           style="background:#C41E3A;color:#fff;padding:9px 18px;
                  border-radius:4px;text-decoration:none;font-weight:bold;">
          View Tickets →
        </a>
      </td>
    </tr>"""


def build_email_html(matching: list) -> str:
    count = len(matching)
    rows  = "\n".join(_event_row_html(ev) for ev in matching)

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:Arial,sans-serif;max-width:820px;margin:auto;padding:20px;color:#222;">

  <div style="background:#C41E3A;padding:22px;border-radius:8px 8px 0 0;text-align:center;">
    <h1 style="color:#fff;margin:0;font-size:26px;">🎭 Chicago the Musical — Ticket Alert!</h1>
    <p style="color:#ffd0d0;margin:8px 0 0;">
      {count} show date{'s' if count != 1 else ''} found with tickets at or under ${MAX_PRICE:.0f}
    </p>
  </div>

  <div style="background:#fff8f8;padding:14px;border:1px solid #f0d0d0;font-size:14px;">
    <strong>Your search:</strong>
    &nbsp; Dates: April 5–10, 2026
    &nbsp;|&nbsp; Budget: up to ${MAX_PRICE:.0f}/ticket
    &nbsp;|&nbsp; Seats needed: {TICKETS_NEEDED}
    &nbsp;|&nbsp; Venue: Ambassador Theatre, NYC
  </div>

  <table style="width:100%;border-collapse:collapse;margin-top:16px;font-size:14px;">
    <thead>
      <tr style="background:#f5f5f5;font-weight:bold;">
        <th style="padding:12px;text-align:left;">Show Date &amp; Time</th>
        <th style="padding:12px;text-align:left;">Venue</th>
        <th style="padding:12px;text-align:center;">Lowest Ticket</th>
        <th style="padding:12px;text-align:center;">Highest Ticket</th>
        <th style="padding:12px;text-align:center;">Action</th>
      </tr>
    </thead>
    <tbody>
      {rows}
    </tbody>
  </table>

  <div style="margin-top:20px;padding:14px;background:#fffbe6;
              border:1px solid #ffe099;border-radius:6px;font-size:13px;">
    <strong>💡 How to use this alert:</strong><br>
    Click <strong>View Tickets</strong> to open the show on Ticketmaster.
    Filter by quantity (<strong>{TICKETS_NEEDED} tickets</strong>) and sort by price.
    Prices shown are Ticketmaster's all-in pricing (fees included).
    Availability changes fast — act quickly!
  </div>

  <div style="margin-top:12px;padding:14px;background:#f0f4ff;
              border:1px solid #c0d0f0;border-radius:6px;font-size:13px;">
    <strong>🚫 What to avoid on Ticketmaster:</strong>&nbsp;
    Skip seats labeled <em>Obstructed View</em>, <em>Limited View</em>,
    <em>Standing Room</em>, or seats in the last few rows of any section.
    Check the interactive seat map before buying.
  </div>

  <p style="margin-top:18px;font-size:11px;color:#aaa;text-align:center;">
    Sent by your Chicago Ticket Agent · Runs every 30 min, 7AM–11PM ET ·
    <em>Alert only — you always buy manually on Ticketmaster.</em>
  </p>

</body>
</html>"""


def send_alert(matching: list) -> bool:
    count   = len(matching)
    subject = (
        f"🎭 ALERT: Chicago Musical tickets from "
        f"${min(e['min_price'] for e in matching):.0f} found "
        f"({count} show date{'s' if count > 1 else ''})!"
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SMTP_SENDER_EMAIL
    msg["To"]      = ALERT_EMAIL
    msg.attach(MIMEText(build_email_html(matching), "html"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as s:
            s.ehlo()
            s.starttls()
            s.login(SMTP_SENDER_EMAIL, SMTP_APP_PASSWORD)
            s.sendmail(SMTP_SENDER_EMAIL, ALERT_EMAIL, msg.as_string())
        log.info("✅ Alert email sent to %s.", ALERT_EMAIL)
        return True
    except smtplib.SMTPAuthenticationError:
        log.error("❌ Gmail authentication failed. Check SMTP_APP_PASSWORD secret.")
    except Exception as e:
        log.error("❌ Email send failed: %s", e)
    return False

# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════

def main():
    log.info("=" * 55)
    log.info("Chicago Ticket Agent — Ticketmaster edition")
    log.info("Window: %s → %s  |  Max price: $%.2f", DATE_FROM, DATE_TO, MAX_PRICE)

    # 1. Fetch events from Ticketmaster
    raw_events = fetch_chicago_events()
    if not raw_events:
        log.info("No events returned from Ticketmaster. Done.")
        return

    # 2. Parse and filter
    qualifying = []
    for raw in raw_events:
        ev = parse_event(raw)
        if ev and meets_criteria(ev):
            qualifying.append(ev)

    log.info("%d event(s) met price criteria.", len(qualifying))

    if not qualifying:
        log.info("No matching events this run. Done.")
        return

    # 3. Deduplicate — build a key from event_id + min_price
    #    (re-alert if price drops further even for a previously seen event)
    seen = load_seen()
    new_matches = []
    for ev in qualifying:
        key = f"{ev['id']}::{ev['min_price']}"
        if key not in seen:
            new_matches.append(ev)

    if not new_matches:
        log.info("All qualifying events already alerted at this price. Done.")
        return

    # 4. Send alert
    log.info("Sending alert for %d new match(es)...", len(new_matches))
    if send_alert(new_matches):
        # Only mark as seen after a successful email
        seen.update(f"{ev['id']}::{ev['min_price']}" for ev in new_matches)
        save_seen(seen)

    log.info("Run complete.")


if __name__ == "__main__":
    main()
