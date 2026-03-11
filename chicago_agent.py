#!/usr/bin/env python3
"""
chicago_agent.py  —  Chicago Musical Ticket Alert Agent
GitHub Actions edition.

Designed to run as a single-shot script:
  - Reads ALL credentials from environment variables (set as GitHub Secrets)
  - Searches SeatGeek once, filters results, sends email if new seats found
  - Persists seen_listings.json in the repo so duplicates are avoided

DO NOT put real credentials in this file — they live in GitHub Secrets only.
"""

import json
import logging
import os
import smtplib
import string
import sys
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

try:
    import requests
except ImportError:
    sys.exit("ERROR: 'requests' not installed. Check requirements.txt.")

# ══════════════════════════════════════════════════════════════
#  CONFIGURATION — loaded from environment variables (GitHub Secrets)
# ══════════════════════════════════════════════════════════════

def _require(name: str) -> str:
    """Get a required environment variable or exit with a helpful message."""
    val = os.environ.get(name, "").strip()
    if not val:
        sys.exit(
            f"ERROR: Required secret '{name}' is missing.\n"
            f"Add it in GitHub → your repo → Settings → Secrets and variables → Actions."
        )
    return val

SEATGEEK_CLIENT_ID     = _require("SEATGEEK_CLIENT_ID")
SEATGEEK_CLIENT_SECRET = os.environ.get("SEATGEEK_CLIENT_SECRET", "").strip()  # optional
SMTP_SENDER_EMAIL      = _require("SMTP_SENDER_EMAIL")
SMTP_APP_PASSWORD      = _require("SMTP_APP_PASSWORD")
ALERT_EMAIL            = _require("ALERT_EMAIL")

# ── Search criteria (edit these directly if you want to change dates/budget) ──
DATE_FROM        = "2026-04-05"
DATE_TO          = "2026-04-10"
TICKETS_NEEDED   = 3
MAX_PRICE        = 225.00     # per ticket, after all fees

# ── Seat quality filters ──────────────────────────────────────
EXCLUDED_SECTION_KEYWORDS = [
    "standing", "standing room", "sro",
    "obstructed", "limited view", "partial view", "restricted view",
    "rear orchestra", "rear mezzanine",
]
BLOCK_LAST_N_ROWS = 3        # skip the last N rows of any section

# ── Persistence file (committed back to repo by the workflow) ─
SEEN_FILE = "seen_listings.json"

# ══════════════════════════════════════════════════════════════
#  LOGGING  (GitHub Actions shows stdout in the run log)
# ══════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("agent")

# ══════════════════════════════════════════════════════════════
#  ROW ORDERING  (Ambassador Theatre)
#  Orchestra: A–Z then AA–FF  /  Mezzanine: A–J
# ══════════════════════════════════════════════════════════════
_ROW_ORDER = list(string.ascii_uppercase) + [
    "AA","BB","CC","DD","EE","FF","GG","HH"
]

def _row_idx(row: str) -> int:
    r = row.strip().upper()
    return _ROW_ORDER.index(r) if r in _ROW_ORDER else -1

# ══════════════════════════════════════════════════════════════
#  SEATGEEK API
# ══════════════════════════════════════════════════════════════
SEATGEEK_BASE = "https://api.seatgeek.com/2"

def _sg_params() -> dict:
    p = {"client_id": SEATGEEK_CLIENT_ID}
    if SEATGEEK_CLIENT_SECRET:
        p["client_secret"] = SEATGEEK_CLIENT_SECRET
    return p

def fetch_events() -> list:
    end = (datetime.strptime(DATE_TO, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    params = {
        **_sg_params(),
        "q": "Chicago the Musical",
        "venue.city": "New York",
        "datetime_local.gte": f"{DATE_FROM}T00:00:00",
        "datetime_local.lte": f"{end}T00:00:00",
        "taxonomies.name": "broadway",
        "per_page": 25,
    }
    try:
        r = requests.get(f"{SEATGEEK_BASE}/events", params=params, timeout=15)
        r.raise_for_status()
        events = r.json().get("events", [])
        log.info("SeatGeek: %d event(s) in window.", len(events))
        return events
    except requests.RequestException as e:
        log.error("SeatGeek /events failed: %s", e)
        return []

def fetch_listings(event_id: int) -> list:
    params = {**_sg_params(), "event_id": event_id, "per_page": 200}
    try:
        r = requests.get(f"{SEATGEEK_BASE}/listings", params=params, timeout=15)
        r.raise_for_status()
        listings = r.json().get("listings", [])
        log.info("  Event %s: %d raw listings.", event_id, len(listings))
        return listings
    except requests.RequestException as e:
        log.error("  SeatGeek /listings failed for event %s: %s", event_id, e)
        return []

# ══════════════════════════════════════════════════════════════
#  VALIDATION
# ══════════════════════════════════════════════════════════════
def is_valid_event(event: dict) -> bool:
    title = event.get("title", "").lower()
    venue = event.get("venue", {}).get("name", "").lower()
    slug  = event.get("slug", "").lower()
    return (
        ("chicago" in title or "chicago" in slug) and
        ("ambassador" in venue or "49th" in venue)
    )

# ══════════════════════════════════════════════════════════════
#  FILTERS
# ══════════════════════════════════════════════════════════════
def section_excluded(section: str) -> bool:
    low = section.lower()
    return any(kw in low for kw in EXCLUDED_SECTION_KEYWORDS)

def row_excluded(row: str, all_rows: list) -> bool:
    if not row or not all_rows:
        return False
    sorted_rows = sorted(set(r.upper() for r in all_rows), key=_row_idx)
    last_n = set(sorted_rows[-BLOCK_LAST_N_ROWS:])
    return row.strip().upper() in last_n

def price_ok(listing: dict) -> bool:
    fee_price = listing.get("price_with_fees")
    base      = listing.get("price", 0)
    effective = fee_price if fee_price is not None else base * 1.20
    return effective <= MAX_PRICE

def qty_ok(listing: dict) -> bool:
    return listing.get("quantity", 0) >= TICKETS_NEEDED

def filter_listings(listings: list) -> list:
    # Gather all rows per section for "last N rows" logic
    sec_rows: dict[str, list] = {}
    for lst in listings:
        sec = lst.get("section", "?")
        row = lst.get("row", "")
        if row:
            sec_rows.setdefault(sec, [])
            if row.upper() not in [r.upper() for r in sec_rows[sec]]:
                sec_rows[sec].append(row)

    passed = []
    cut = {"section": 0, "row": 0, "price": 0, "qty": 0}
    for lst in listings:
        sec = lst.get("section", "")
        row = lst.get("row", "")
        if section_excluded(sec):                         cut["section"] += 1; continue
        if row_excluded(row, sec_rows.get(sec, [])):      cut["row"]     += 1; continue
        if not price_ok(lst):                             cut["price"]   += 1; continue
        if not qty_ok(lst):                               cut["qty"]     += 1; continue
        passed.append(lst)

    log.info(
        "Filter results: %d passed | dropped → section:%d row:%d price:%d qty:%d",
        len(passed), cut["section"], cut["row"], cut["price"], cut["qty"]
    )
    return passed

# ══════════════════════════════════════════════════════════════
#  DEDUPLICATION  (seen_listings.json stored in repo)
# ══════════════════════════════════════════════════════════════
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

def filter_new(pairs: list) -> list:
    seen = load_seen()
    new  = [(lst, ev) for lst, ev in pairs if str(lst.get("id","")) not in seen]
    if new:
        seen.update(str(lst.get("id","")) for lst, _ in new)
        save_seen(seen)
        log.info("%d new listing(s) (not previously alerted).", len(new))
    else:
        log.info("No new listings — all already seen.")
    return new

# ══════════════════════════════════════════════════════════════
#  EMAIL
# ══════════════════════════════════════════════════════════════
def _listing_row_html(lst: dict, ev: dict) -> str:
    section   = lst.get("section", "N/A")
    row       = lst.get("row", "N/A")
    qty       = lst.get("quantity", "?")
    base      = lst.get("price", 0)
    fee_price = lst.get("price_with_fees")
    url       = lst.get("url") or ev.get("url", "https://seatgeek.com")
    dt_str    = ev.get("datetime_local", "")

    if fee_price:
        price_display = f"${fee_price:.2f} (incl. fees)"
    else:
        price_display = f"~${base * 1.20:.2f} (est. w/fees)"

    try:
        dt_obj = datetime.fromisoformat(dt_str)
        dt_display = dt_obj.strftime("%A, %B %-d %Y at %-I:%M %p")
    except Exception:
        dt_display = dt_str or "See link"

    return f"""
    <tr style="border-bottom:1px solid #eee;">
      <td style="padding:10px;">{dt_display}</td>
      <td style="padding:10px;">{section}</td>
      <td style="padding:10px;text-align:center;">{row}</td>
      <td style="padding:10px;text-align:center;">{qty}</td>
      <td style="padding:10px;text-align:center;font-weight:bold;color:#2a7a2a;">{price_display}</td>
      <td style="padding:10px;text-align:center;">
        <a href="{url}" style="background:#C41E3A;color:#fff;padding:8px 16px;
           border-radius:4px;text-decoration:none;font-weight:bold;">
          View &amp; Buy →
        </a>
      </td>
    </tr>"""

def build_email(pairs: list) -> str:
    rows = "\n".join(_listing_row_html(lst, ev) for lst, ev in pairs)
    count = len(pairs)
    return f"""<!DOCTYPE html><html><body style="font-family:Arial,sans-serif;max-width:800px;margin:auto;padding:20px;">
  <div style="background:#C41E3A;padding:20px;border-radius:8px 8px 0 0;text-align:center;">
    <h1 style="color:#fff;margin:0;">🎭 Chicago the Musical — Ticket Alert!</h1>
    <p style="color:#ffd0d0;margin:8px 0 0;">
      {count} qualifying listing{'s' if count!=1 else ''} found for your criteria
    </p>
  </div>
  <div style="background:#fff8f8;padding:12px;border:1px solid #f0d0d0;">
    <strong>Your filters:</strong> Dates: Apr 5–10, 2026 &nbsp;|&nbsp;
    3 seats together &nbsp;|&nbsp; Max ${MAX_PRICE:.0f}/ticket after fees &nbsp;|&nbsp;
    No standing room &nbsp;|&nbsp; No obstructed views &nbsp;|&nbsp; Not last rows
  </div>
  <table style="width:100%;border-collapse:collapse;margin-top:16px;font-size:14px;">
    <thead>
      <tr style="background:#f5f5f5;font-weight:bold;">
        <th style="padding:10px;text-align:left;">Show Date &amp; Time</th>
        <th style="padding:10px;text-align:left;">Section</th>
        <th style="padding:10px;text-align:center;">Row</th>
        <th style="padding:10px;text-align:center;">Qty</th>
        <th style="padding:10px;text-align:center;">Price/Ticket</th>
        <th style="padding:10px;text-align:center;">Action</th>
      </tr>
    </thead>
    <tbody>{rows}</tbody>
  </table>
  <div style="margin-top:20px;padding:14px;background:#fffbe6;border:1px solid #ffe099;
              border-radius:6px;font-size:13px;">
    <strong>⚠️ Reminder:</strong> Always confirm the total (including all fees) on SeatGeek
    before purchasing. Ticket availability can change quickly — act fast!
  </div>
  <p style="margin-top:16px;font-size:11px;color:#aaa;text-align:center;">
    Sent by your Chicago Ticket Agent · Running every 30 min · 7AM–11PM ET ·
    <em>Alert only — you buy manually.</em>
  </p>
</body></html>"""

def send_email(pairs: list) -> bool:
    subject = (
        f"🎭 ALERT: {len(pairs)} Chicago Musical listing{'s' if len(pairs)>1 else ''} "
        f"under ${MAX_PRICE:.0f} found!"
    )
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SMTP_SENDER_EMAIL
    msg["To"]      = ALERT_EMAIL
    msg.attach(MIMEText(build_email(pairs), "html"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as s:
            s.ehlo(); s.starttls()
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
    log.info("Chicago Ticket Agent starting  [%s → %s]", DATE_FROM, DATE_TO)
    log.info("Budget: $%.2f/ticket | Seats needed: %d", MAX_PRICE, TICKETS_NEEDED)

    # 1. Find events
    raw_events   = fetch_events()
    valid_events = [ev for ev in raw_events if is_valid_event(ev)]
    log.info("%d/%d events passed venue/show check.", len(valid_events), len(raw_events))

    if not valid_events:
        log.info("No matching events found. Done.")
        return

    # 2. Fetch & filter listings for each event
    all_qualifying: list[tuple[dict, dict]] = []
    for ev in valid_events:
        raw      = fetch_listings(ev["id"])
        filtered = filter_listings(raw)
        for lst in filtered:
            if not lst.get("url"):
                lst["url"] = ev.get("url", "https://seatgeek.com")
            all_qualifying.append((lst, ev))

    log.info("Total qualifying listings across all events: %d", len(all_qualifying))

    if not all_qualifying:
        log.info("No listings met all criteria. Done.")
        return

    # 3. Deduplicate
    new_listings = filter_new(all_qualifying)
    if not new_listings:
        log.info("All qualifying listings already alerted. Done.")
        return

    # 4. Send alert
    send_email(new_listings)
    log.info("Run complete.")

if __name__ == "__main__":
    main()
