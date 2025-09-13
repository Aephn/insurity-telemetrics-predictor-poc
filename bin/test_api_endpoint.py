#!/usr/bin/env python3
"""Simple API test script for the validation / telemetry ingestion endpoints.

Now purely variable-driven (no CLI args) per request.

Edit CONFIG section below and run:
    python scripts/test_api_endpoint.py

Exit codes:
    0 on HTTP 200/207
    1 on network error or non-2xx
"""

from __future__ import annotations

import json
import os
import random
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

# -------------------------------------------------------------
# Global endpoints (environment-specific hardcoded for local testing)
# -------------------------------------------------------------
# NOTE: API Gateway URLs must include the stage segment (e.g., /dev). 403 'Forbidden' occurs if omitted.
# Configure via env vars API_ROOT (without stage) and API_STAGE (default 'dev').
API_ROOT = os.getenv("API_ROOT", "https://ayvkdmlnyh.execute-api.us-east-1.amazonaws.com").rstrip("/")
API_STAGE = os.getenv("API_STAGE", "dev").strip("/")
BASE_API = f"{API_ROOT}/{API_STAGE}"  # now includes stage

VALIDATE_ENDPOINT = f"{BASE_API}/validate"
TELEMETRY_ENDPOINT = f"{BASE_API}/telemetry"
STATUS_ENDPOINT = f"{BASE_API}/status"
LOCATION_ENDPOINT = f"{BASE_API}/location"
TRIPS_ENDPOINT = f"{BASE_API}/trips"

ENDPOINTS = {
    "validate": VALIDATE_ENDPOINT,
    "telemetry": TELEMETRY_ENDPOINT,
    "status": STATUS_ENDPOINT,
    "location": LOCATION_ENDPOINT,
    "trips": TRIPS_ENDPOINT,
}

# -------------------------------------------------------------
# CONFIG (adjust as needed)
# -------------------------------------------------------------
TARGET_PATH = "location"         # one of keys in ENDPOINTS or set TARGET_URL directly
TARGET_URL: str | None = None     # override full URL if desired
EVENT_COUNT = 2                   # number of valid events (ignored if DIRECT_EVENTS_FILE used)
INCLUDE_INVALID = False           # add one invalid record to force 207
PRINT_PAYLOAD = True              # print JSON payload before sending
TIMEOUT_SECS = 10.0               # HTTP timeout
REPEAT = 1                        # number of sequential requests
SLEEP_BETWEEN = 0.5               # delay between repeats (seconds)

# --- New deterministic / direct injection controls ---
RANDOM_SEED: int | None = None    # set int for deterministic pseudo-random generation
FIXED_DRIVER_ID: str | None = None  # force all events to use this driver id
FIXED_EVENT_TYPE: str | None = None  # force event_type to this string (must be one of EVENT_TYPES below)
USE_VARIANTS = True               # if False, do not randomize driver_id/trip_id each event
DIRECT_EVENTS_FILE: str | None = None  # path to JSON file containing an array of events to send as-is

EVENT_TYPES = [
    "hard_braking",
    "aggressive_turn",
    "speeding",
    "tailgating",
    "late_night_driving",
    "ping",
]

if RANDOM_SEED is not None:
    random.seed(RANDOM_SEED)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _stable_id(prefix: str, idx: int) -> str:
    # Deterministic id generation when USE_VARIANTS is False
    return f"{prefix}{idx:05d}" if not USE_VARIANTS else f"{prefix}-{random.randint(10000,99999)}"


def gen_event(idx: int, driver_id: str | None = None) -> dict:
    etype = FIXED_EVENT_TYPE or random.choice(EVENT_TYPES)
    d_id = driver_id or FIXED_DRIVER_ID or (f"D{random.randint(1000,9999)}" if USE_VARIANTS else _stable_id("D", 0))
    trip_id = _stable_id("T", idx) if not USE_VARIANTS else f"T-{random.randint(10000,99999)}"
    data = {
        "event_id": uuid.uuid4().hex if USE_VARIANTS else f"E{idx:08d}",
        "driver_id": d_id,
        "trip_id": trip_id,
        "ts": now_iso(),
        "event_type": etype,
        "latitude": round(random.uniform(25.0, 49.0), 5),
        "longitude": round(random.uniform(-124.0, -67.0), 5),
        "speed_mph": round(random.uniform(0, 85), 1),
        "heading_deg": random.randint(0, 359),
        "period_minute": int(time.time() // 60) % 100000,
    }
    if etype == "hard_braking":
        data["braking_g"] = round(random.uniform(0.1, 0.9), 2)
    if etype == "aggressive_turn":
        data["lateral_g"] = round(random.uniform(0.2, 1.2), 2)
    if etype == "speeding":
        posted = random.randint(25, 70)
        over = random.randint(5, 25)
        data["posted_speed_mph"] = posted
        data["over_speed_mph"] = over
        data["speed_mph"] = posted + over
    if etype == "tailgating":
        data["following_distance_m"] = round(random.uniform(5.0, 30.0), 1)
    if etype == "late_night_driving":
        data["local_hour"] = random.randint(0, 23)
    return data


def maybe_make_invalid(ev: dict) -> dict:
    # introduce a simple schema violation (bad speed) for testing 207 responses
    bad = dict(ev)
    bad["speed_mph"] = 500  # > 200 triggers validation error
    return bad


def build_payload(count: int, include_invalid: bool) -> list[dict]:
    if DIRECT_EVENTS_FILE:
        p = Path(DIRECT_EVENTS_FILE)
        raw = json.loads(p.read_text())
        if not isinstance(raw, list):
            raise SystemExit("DIRECT_EVENTS_FILE must contain a JSON array of event objects")
        events = raw
    else:
        events: list[dict] = []
        for i in range(count):
            ev = gen_event(i)
            events.append(ev)
    if include_invalid and events and not DIRECT_EVENTS_FILE:
        events.append(maybe_make_invalid(events[0]))
    return events


def resolve_url() -> str:
    if TARGET_URL:
        return TARGET_URL
    env_url = os.getenv("API_URL")
    if env_url:
        return env_url
    if TARGET_PATH in ENDPOINTS:
        return ENDPOINTS[TARGET_PATH]
    raise SystemExit("Unable to resolve URL (set TARGET_URL or TARGET_PATH or API_URL)")


def single_request(client: httpx.Client, url: str) -> int:
    payload = build_payload(EVENT_COUNT, INCLUDE_INVALID)
    if PRINT_PAYLOAD:
        print("Request payload:")
        print(json.dumps(payload, indent=2))
    try:
        resp = client.post(url, json=payload, headers={"Content-Type": "application/json"})
    except Exception as e:  # noqa: BLE001
        print(f"Network error: {e}", file=sys.stderr)
        return 1
    print(f"Status: {resp.status_code}")
    ct = resp.headers.get("Content-Type")
    if ct and "application/json" in ct:
        try:
            data = resp.json()
            print(json.dumps(data, indent=2))
        except Exception:
            print(resp.text)
    else:
        print(resp.text)
    if resp.status_code not in (200, 207):
        return 1
    if resp.status_code == 207:
        print("Partial failure detected (validation or forwarding issues).")
    return 0


def main() -> int:
    url = resolve_url()
    print(f"Target URL: {url}")
    rc = 0
    with httpx.Client(timeout=TIMEOUT_SECS) as client:
        for i in range(REPEAT):
            if REPEAT > 1:
                print(f"--- Request {i+1}/{REPEAT} ---")
            rc = single_request(client, url)
            if rc != 0 and REPEAT > 1:
                print("Stopping due to failure.")
                break
            if REPEAT > 1 and i < REPEAT - 1:
                time.sleep(SLEEP_BETWEEN)
    return rc


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
