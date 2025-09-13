#!/usr/bin/env python3
"""Simple test client for the dashboard API Gateway endpoints.

Reference style: `scripts/test_api_endpoint.py` (ingestion tester).

Edit CONFIG section below and run:
    python scripts/test_dashboard_api.py

Environment variables (optional overrides):
    DASHBOARD_API_BASE  - base URL (defaults to hardcoded value below)
    DASHBOARD_URL       - full dashboard endpoint override
    DASHBOARD_HEALTH_URL- full health endpoint override

Exit codes:
    0 on success (HTTP 200 for all requested endpoints)
    1 on network error or non-200
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# Default endpoint bases (replace if environment changes)
# ---------------------------------------------------------------------------
DASHBOARD_API_BASE = os.getenv(
    "DASHBOARD_API_BASE",
    "https://7e9o2dxu72.execute-api.us-east-1.amazonaws.com/dev",
)
DASHBOARD_ENDPOINT = os.getenv(
    "DASHBOARD_URL", f"{DASHBOARD_API_BASE}/dashboard"
)
HEALTH_ENDPOINT = os.getenv(
    "DASHBOARD_HEALTH_URL", f"{DASHBOARD_API_BASE}/healthz"
)

ENDPOINTS = {
    "dashboard": DASHBOARD_ENDPOINT,
    "health": HEALTH_ENDPOINT,
}

# ---------------------------------------------------------------------------
# CONFIG (adjust as needed)
# ---------------------------------------------------------------------------
TARGETS = ["health", "dashboard"]  # order of endpoints to hit
TIMEOUT_SECS = 10.0                  # HTTP timeout
REPEAT = 1                           # number of sequential full passes
SLEEP_BETWEEN = 0.5                  # delay between passes
PRINT_JSON = True                    # pretty print JSON responses
ASSERT_FIELDS: list[str] | None = ["generated_at", "drivers"]  # keys expected in /dashboard

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fetch(client: httpx.Client, name: str, url: str) -> tuple[int, Any]:
    try:
        resp = client.get(url)
    except Exception as e:  # noqa: BLE001
        print(f"[{name}] Network error: {e}", file=sys.stderr)
        return 1, None
    status = resp.status_code
    ctype = resp.headers.get("Content-Type", "")
    body: Any
    if "application/json" in ctype:
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = resp.text
    else:
        body = resp.text
    print(f"[{name}] {status} {url}")
    if PRINT_JSON and isinstance(body, (dict, list)):
        print(json.dumps(body, indent=2)[:5000])  # safety truncate
    elif PRINT_JSON:
        print(str(body)[:5000])
    if status != 200:
        return 1, body
    return 0, body


def check_dashboard_schema(payload: Any) -> bool:
    if not isinstance(payload, dict):
        print("[dashboard] Payload not a JSON object; skipping field assertions")
        return False
    if ASSERT_FIELDS:
        missing = [k for k in ASSERT_FIELDS if k not in payload]
        if missing:
            print(f"[dashboard] Missing expected fields: {missing}")
            return False
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("Dashboard API test starting")
    print(f"Base: {DASHBOARD_API_BASE}")
    print(f"Endpoints: {ENDPOINTS}")
    rc = 0
    with httpx.Client(timeout=TIMEOUT_SECS) as client:
        for attempt in range(REPEAT):
            if REPEAT > 1:
                print(f"--- Pass {attempt+1}/{REPEAT} ---")
            for name in TARGETS:
                if name not in ENDPOINTS:
                    print(f"Unknown target '{name}' (skipping)")
                    continue
                code, body = fetch(client, name, ENDPOINTS[name])
                if code != 0:
                    rc = 1
                if name == "dashboard" and code == 0:
                    check_dashboard_schema(body)
            if rc != 0 and REPEAT > 1:
                print("Stopping early due to failure")
                break
            if REPEAT > 1 and attempt < REPEAT - 1:
                time.sleep(SLEEP_BETWEEN)
    return rc


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
