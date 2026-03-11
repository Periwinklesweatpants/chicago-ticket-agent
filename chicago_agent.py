#!/usr/bin/env python3
"""
chicago_agent.py  —  DIAGNOSTIC VERSION
Replace your current chicago_agent.py with this temporarily.
Run it once from GitHub Actions, paste the log, then we fix the real IDs.
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

TM_BASE = "https://app.ticketmaster.com/discovery/v2"

def main():
    log.info("=" * 55)
    log.info("DIAGNOSTIC — searching Ticketmaster broadly for Chicago")

    # ── TEST 1: Search by keyword only, no venue/date filter ─────────────────
    # This tells us if the API key works and what IDs Ticketmaster uses
    log.info("\n--- TEST 1: keyword search 'Chicago Musical', no filters ---")
    r = requests.get(f"{TM_BASE}/events.json", params={
        "apikey":   TM_API_KEY,
        "keyword":  "Chicago Musical",
        "stateCode": "NY",
        "size":     5,
    }, timeout=15)
    log.info("Status: %s", r.status_code)
    data = r.json()
    events = data.get("_embedded", {}).get("events", [])
    log.info("Events found: %d", len(events))
    for ev in events:
        venues = ev.get("_embedded", {}).get("venues", [{}])
        attractions = ev.get("_embedded", {}).get("attractions", [{}])
        log.info("  name='%s' id='%s' date='%s'",
            ev.get("name"), ev.get("id"),
            ev.get("dates", {}).get("start", {}).get("localDate"))
        log.info("    venue name='%s' id='%s'",
            venues[0].get("name"), venues[0].get("id"))
        log.info("    attraction name='%s' id='%s'",
            attractions[0].get("name") if attractions else "N/A",
            attractions[0].get("id") if attractions else "N/A")
        log.info("    url='%s'", ev.get("url"))
        log.info("    priceRanges=%s", ev.get("priceRanges"))

    # ── TEST 2: Search attractions endpoint for the show ──────────────────────
    log.info("\n--- TEST 2: attractions search for 'Chicago' ---")
    r2 = requests.get(f"{TM_BASE}/attractions.json", params={
        "apikey":  TM_API_KEY,
        "keyword": "Chicago",
        "classificationName": "Theatre",
        "size": 5,
    }, timeout=15)
    log.info("Status: %s", r2.status_code)
    data2 = r2.json()
    attractions = data2.get("_embedded", {}).get("attractions", [])
    log.info("Attractions found: %d", len(attractions))
    for a in attractions:
        log.info("  name='%s' id='%s'", a.get("name"), a.get("id"))

    # ── TEST 3: Search venues for Ambassador Theatre ───────────────────────────
    log.info("\n--- TEST 3: venue search for 'Ambassador Theatre' ---")
    r3 = requests.get(f"{TM_BASE}/venues.json", params={
        "apikey":    TM_API_KEY,
        "keyword":   "Ambassador Theatre",
        "stateCode": "NY",
        "size": 5,
    }, timeout=15)
    log.info("Status: %s", r3.status_code)
    data3 = r3.json()
    venues = data3.get("_embedded", {}).get("venues", [])
    log.info("Venues found: %d", len(venues))
    for v in venues:
        log.info("  name='%s' id='%s' city='%s'",
            v.get("name"), v.get("id"),
            v.get("city", {}).get("name"))

    log.info("\nDiagnostic complete. Paste this entire log back to Claude.")

if __name__ == "__main__":
    main()
