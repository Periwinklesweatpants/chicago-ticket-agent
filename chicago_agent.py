#!/usr/bin/env python3
"""
chicago_agent.py  —  DIAGNOSTIC v3
Tests the Ticketmaster Commerce API and Inventory Status API
using the real event IDs we confirmed from the previous diagnostic.

Replace your chicago_agent.py with this, run once, paste the log.
"""

import logging
import os
import sys

try:
    import requests
except ImportError:
    sys.exit("ERROR: 'requests' not installed.")

def _require(name):
    val = os.environ.get(name, "").strip()
    if not val:
        sys.exit(f"ERROR: Required secret '{name}' is not set.")
    return val

TM_API_KEY = _require("TICKETMASTER_API_KEY")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("diag")

# Real event IDs confirmed from diagnostic run on 2026-03-11
KNOWN_EVENT_IDS = [
    "Z7r9jZ1A7bz4I",   # 2026-03-12
    "Z7r9jZ1A7bz4f",   # 2026-03-13
    "Z7r9jZ1A7bz44",   # 2026-03-14
]

ATTRACTION_ID = "K8vZ9179Ip7"
VENUE_ID      = "Zkr9jZkAeP"
TM_BASE       = "https://app.ticketmaster.com"

def main():
    log.info("=" * 60)
    log.info("DIAGNOSTIC v3 — Testing Commerce API + April event search")

    # TEST 1: Commerce API /offers on a known event ID
    log.info("\n--- TEST 1: Commerce API /offers on known event ID ---")
    for eid in KNOWN_EVENT_IDS[:2]:
        url = f"{TM_BASE}/commerce/v1/events/{eid}/offers.json"
        r = requests.get(url, params={"apikey": TM_API_KEY}, timeout=15)
        log.info("Event %s status: %s", eid, r.status_code)
        if r.status_code == 200:
            data = r.json()
            log.info("  Response keys: %s", list(data.keys()))
            offers = data.get("offers", data.get("_embedded", {}).get("offers", []))
            log.info("  Offers count: %d", len(offers))
            if offers:
                log.info("  First offer: %s", str(offers[0])[:400])
        else:
            log.info("  Response: %s", r.text[:300])

    # TEST 2: Inventory Status API
    log.info("\n--- TEST 2: Inventory Status API ---")
    r2 = requests.get(
        f"{TM_BASE}/inventory-status/v1/availability",
        params={"apikey": TM_API_KEY, "events": ",".join(KNOWN_EVENT_IDS[:2])},
        timeout=15
    )
    log.info("Inventory Status status: %s", r2.status_code)
    log.info("Response: %s", r2.text[:500])

    # TEST 3: Search for April 5-10 dates
    log.info("\n--- TEST 3: April 5-10 event search ---")
    r3 = requests.get(
        f"{TM_BASE}/discovery/v2/events.json",
        params={
            "apikey": TM_API_KEY,
            "attractionId": ATTRACTION_ID,
            "venueId": VENUE_ID,
            "startDateTime": "2026-04-05T00:00:00Z",
            "endDateTime": "2026-04-10T23:59:59Z",
            "size": 20,
        },
        timeout=15
    )
    log.info("April search status: %s", r3.status_code)
    events = r3.json().get("_embedded", {}).get("events", [])
    log.info("April events found: %d", len(events))
    for ev in events:
        log.info("  id='%s' date='%s'", ev.get("id"),
            ev.get("dates", {}).get("start", {}).get("localDate"))

    # TEST 4: Commerce API on April event if found
    if events:
        april_id = events[0].get("id")
        log.info("\n--- TEST 4: Commerce API on April event %s ---", april_id)
        r4 = requests.get(
            f"{TM_BASE}/commerce/v1/events/{april_id}/offers.json",
            params={"apikey": TM_API_KEY}, timeout=15
        )
        log.info("Commerce API status: %s", r4.status_code)
        log.info("Response (1000 chars): %s", r4.text[:1000])

    # TEST 5: Direct page scrape
    log.info("\n--- TEST 5: Direct HTTP fetch of event page ---")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }
    r5 = requests.get(
        f"https://www.ticketmaster.com/event/{KNOWN_EVENT_IDS[0]}",
        headers=headers, timeout=15, allow_redirects=True
    )
    log.info("Direct page status: %s", r5.status_code)
    if "__NEXT_DATA__" in r5.text:
        log.info("  ✅ __NEXT_DATA__ JSON blob found in page")
        start = r5.text.find("__NEXT_DATA__")
        log.info("  Snippet: %s", r5.text[start:start+300])
    elif "priceRanges" in r5.text:
        log.info("  ✅ priceRanges found in page HTML")
    else:
        log.info("  ❌ No structured data — page may require JavaScript")
        log.info("  First 300 chars: %s", r5.text[:300])

    log.info("\nDiagnostic complete. Paste this full log back to Claude.")

if __name__ == "__main__":
    main()
