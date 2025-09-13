"""Minimal mock server to serve dashboard snapshot at /api/dashboard.
Run:
    python scripts/local/mock_dashboard_server.py [--bad-driver | --good-driver | --random-driver] [--port 8787]

Modes (mutually exclusive):
    --bad-driver     Intensifies negative driving factors each request (risk rises).
    --good-driver    Dampens negative factors / improves safety over time.
    --random-driver  Random walk of factors (default if none specified).

State: Maintains an in-memory rolling list of synthetic recent events reflecting the chosen mode.
Cloud safety: Only this local script is modified; Lambda code remains unchanged.
"""
from __future__ import annotations
import json
import argparse
import random
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
import sys
from pathlib import Path
from urllib.parse import urlparse

# --- Flexible path setup for local runs ---
# Allow execution either from repo root or any subdirectory without pre-setting PYTHONPATH.
ROOT = Path(__file__).resolve().parents[2]  # repo root (../.. from this file)
SRC_DIR = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if SRC_DIR.exists() and str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# Dual import strategy to avoid breaking cloud code (which uses package-style 'aws_lambda')
try:  # preferred (original structure when imported as a module via 'src.' prefix)
    from src.aws_lambda.dashboard_snapshot.handler import generate_snapshot  # type: ignore
except ModuleNotFoundError:  # fallback when running locally without 'src' package
    try:
        from aws_lambda.dashboard_snapshot.handler import generate_snapshot  # type: ignore
    except ModuleNotFoundError as e:  # pragma: no cover
        raise RuntimeError("Unable to locate dashboard_snapshot.handler; check your repo layout") from e


_MODE = {
    "bad": False,
    "good": False,
    "random": True,  # default
}
_EVENT_BUFFER: list[dict] = []
_MAX_EVENTS = 200
_BASE_SNAPSHOT: dict | None = None  # cached initial snapshot

def _mutate_snapshot(snapshot: dict) -> dict:
    """Adjust snapshot factors & history based on mode, and append a new synthetic recent event."""
    if not snapshot:
        return snapshot
    history = snapshot.get("history", [])
    if not history:
        return snapshot
    # Work on latest month entry
    latest = history[-1]
    factors = latest.get("factors", {})

    def clamp(v, lo=0, hi=100):
        try:
            return max(lo, min(hi, float(v)))
        except Exception:  # pragma: no cover
            return v

    # Mutate numeric factor-like metrics inside latest factors referencing safety signals
    keys = [
        ("hardBraking", "hard_braking_events_per_100mi"),
        ("aggressiveTurning", "aggressive_turning_events_per_100mi"),
        ("followingDistance", "tailgating_time_ratio"),
        ("excessiveSpeeding", "speeding_minutes_per_100mi"),
        ("lateNightDriving", "late_night_miles_per_100mi"),
    ]
    # Direction multipliers
    if _MODE["bad"]:
        mult = 1.15
        noise = (0.05, 0.25)
    elif _MODE["good"]:
        mult = 0.90
        noise = (-0.20, 0.05)
    else:  # random
        mult = random.uniform(0.95, 1.05)
        noise = (-0.10, 0.15)

    # Adjust underlying monthly scores (premium risk interplay simplified)
    latest["premium"] = float(clamp(latest.get("premium", 110) * (1.05 if _MODE["bad"] else 0.98 if _MODE["good"] else mult), 40, 400))
    latest["modelMultiplier"] = float(clamp(latest.get("modelMultiplier", 1.0) * (1.10 if _MODE["bad"] else 0.95 if _MODE["good"] else mult), 0.5, 3.0))
    latest["riskScore"] = float(clamp(latest.get("riskScore", 0.5) * (1.20 if _MODE["bad"] else 0.90 if _MODE["good"] else mult), 0.01, 5.0))
    base_score = latest.get("safetyScore", 70)
    latest["safetyScore"] = int(clamp(base_score - random.uniform(3, 7) if _MODE["bad"] else base_score + random.uniform(2, 5) if _MODE["good"] else base_score + random.uniform(-3, 3), 0, 100))

    # Build a new event reflecting change
    new_evt = None
    for label, _raw_key in keys:
        # mutate factor approximation (snapshot factors dict in history entry uses 'factors')
        cur_val = factors.get(label, 0) or 0
        delta = cur_val * (mult - 1) + random.uniform(*noise)
        new_val = max(0, cur_val + delta)
        factors[label] = new_val  # keep float until final rounding step
        if new_evt is None:  # use first mutated label to spawn an event
            sev = "high" if new_val > 15 else "moderate" if new_val > 7 else "low"
            new_evt = {
                "id": f"evt_{label}_{int(time.time())}",
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "type": label,
                "severity": sev,
                "value": int(round(new_val)),
                "speedMph": round(random.uniform(20, 80), 1),
            }

    if new_evt:
        _EVENT_BUFFER.append(new_evt)
        if len(_EVENT_BUFFER) > _MAX_EVENTS:
            del _EVENT_BUFFER[: len(_EVENT_BUFFER) - _MAX_EVENTS]

    # Expose rolling events in snapshot (combine existing with buffer)
    existing = snapshot.get("recentEvents", [])
    combined = (existing + _EVENT_BUFFER)[-50:]
    # Round factor values for display (whole numbers) without mutating historical raw floats elsewhere
    hist_factors = history[-1].get("factors", {})
    for k, v in list(hist_factors.items()):
        try:
            hist_factors[k] = int(round(float(v)))
        except Exception:  # pragma: no cover
            pass
    # Ensure events show integer 'value'
    for ev in combined:
        if "value" in ev:
            try:
                ev["value"] = int(ev["value"])
            except Exception:  # pragma: no cover
                pass
    snapshot["recentEvents"] = combined
    snapshot["mode"] = "bad" if _MODE["bad"] else "good" if _MODE["good"] else "random"
    snapshot["recentEventsCount"] = len(combined)
    snapshot["lastMutationTs"] = int(time.time())
    # Keep top-level currentFactors loosely aligned with latest factors for UI simplicity
    try:
        snapshot["currentFactors"] = history[-1].get("factors", {})
    except Exception:  # pragma: no cover
        pass
    return snapshot


def _get_cached_snapshot() -> dict:
    """Return a persistent baseline snapshot (generated once per process)."""
    global _BASE_SNAPSHOT  # noqa: PLW0603
    if _BASE_SNAPSHOT is None:
        _BASE_SNAPSHOT = generate_snapshot()
    return _BASE_SNAPSHOT


class Handler(BaseHTTPRequestHandler):
    def _set_cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def do_OPTIONS(self):  # noqa: N802 - preflight
        self.send_response(204)
        self._set_cors()
        self.end_headers()

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == '/api/dashboard':
            snap = _get_cached_snapshot()
            snap = _mutate_snapshot(snap)
            body = json.dumps(snap).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self._set_cors()
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == '/healthz':
            body = b'OK'
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self._set_cors()
            self.send_header('Content-Length', '2')
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self._set_cors()
        self.end_headers()


def run_server(port: int = 8787):
    httpd = HTTPServer(('0.0.0.0', port), Handler)
    print(f"Mock dashboard server on http://localhost:{port}/api/dashboard (health: /healthz)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        print('\nServer stopping...')
    finally:
        httpd.server_close()
        print('Server shutdown.')

def _parse_args():
    p = argparse.ArgumentParser(description="Local dashboard mock server")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--bad-driver", action="store_true", help="Continuously worsen driver risk factors")
    g.add_argument("--good-driver", action="store_true", help="Continuously improve driver risk factors")
    g.add_argument("--random-driver", action="store_true", help="Random walk driver risk factors (default)")
    p.add_argument("--port", type=int, default=8787, help="Port to listen on (default 8787)")
    return p.parse_args()


if __name__ == '__main__':
    args = _parse_args()
    if args.bad_driver:
        _MODE.update({"bad": True, "good": False, "random": False})
    elif args.good_driver:
        _MODE.update({"bad": False, "good": True, "random": False})
    else:
        _MODE.update({"bad": False, "good": False, "random": True})
    print("Mode:", "bad" if _MODE["bad"] else "good" if _MODE["good"] else "random")
    run_server(args.port)
