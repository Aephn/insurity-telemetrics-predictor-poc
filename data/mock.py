"""Mock Telematics Data Generator
=================================

Generates synthetic, event-level telematics records suitable for ingestion into a
future API endpoint or feature pipeline for the usage-based insurance prototype.

Goals:
 - Provide realistic(ish) distributions for core safety behaviors (braking, speeding, etc.).
 - Maintain easy extensibility: add new event types or per-event attributes via simple config.
 - Support both streaming-like (infinite/loop) mode and batch file generation.
 - Enable reproducibility through a seed.

Usage Examples:
  # Write 5,000 events to JSON Lines
  python data/mock.py --events 5000 --out telemetry_events.jsonl

  # Stream events to stdout (Ctrl+C to stop)
  python data/mock.py --stream

  # CSV output with custom driver count
  python data/mock.py --events 2000 --drivers 25 --format csv --out events.csv

Schema (base fields):
  event_id: str (UUID)
  driver_id: str
  ts: ISO8601 timestamp (UTC)
  event_type: str (one of configured types)
  latitude, longitude: float (approx bounding box)
  speed_mph: float
  heading_deg: int (0-359)
  trip_id: str (stable during a synthetic trip window)
  period_minute: int (minutes since simulated start)
  ...type-specific attributes (e.g., braking_g, lateral_g, over_speed_mph)

Extending:
  1. Add an entry in EVENT_TYPE_CONFIG with generation logic.
  2. (Optional) Add aggregation logic in future feature engineering code.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional

try:
    from typing import TypedDict
except ImportError:  # Python <3.8 fallback
    TypedDict = dict  # type: ignore

ISO = "%Y-%m-%dT%H:%M:%S.%fZ"


class TelemetryEvent(TypedDict, total=False):
    event_id: str
    driver_id: str
    trip_id: str
    ts: str
    event_type: str
    latitude: float
    longitude: float
    speed_mph: float
    heading_deg: int
    period_minute: int
    # Optional dynamic keys per event type


# -------------------------------- Configuration ---------------------------------


@dataclass
class EventTypeSpec:
    name: str
    probability: float  # relative frequency weight
    attribute_fn: Callable[[random.Random], Dict[str, Any]]  # adds type-specific fields


def braking_attrs(rng: random.Random) -> Dict[str, Any]:
    # braking_g: peak deceleration (g forces)
    return {
        "braking_g": round(rng.uniform(0.25, 0.9), 2),
        "abs_activation": rng.random() < 0.15,
    }


def aggressive_turn_attrs(rng: random.Random) -> Dict[str, Any]:
    return {
        "lateral_g": round(rng.uniform(0.3, 1.1), 2),
        "turn_direction": rng.choice(["left", "right"]),
    }


def speeding_attrs(rng: random.Random) -> Dict[str, Any]:
    over = rng.uniform(5, 35)
    return {
        "posted_speed_mph": int(rng.choice([25, 35, 45, 55, 65, 70])),
        "over_speed_mph": round(over, 1),
        "duration_sec": int(rng.uniform(10, 240)),
    }


def tailgating_attrs(rng: random.Random) -> Dict[str, Any]:
    return {
        "following_distance_m": round(rng.uniform(4.0, 15.0), 1),
        "speed_context_mph": int(rng.uniform(20, 75)),
    }


def late_night_attrs(rng: random.Random) -> Dict[str, Any]:
    # Tag events specifically in late-night window for feature creation.
    return {"local_hour": rng.choice([0, 1, 2, 3])}


EVENT_TYPE_CONFIG: List[EventTypeSpec] = [
    EventTypeSpec("hard_braking", 0.18, braking_attrs),
    EventTypeSpec("aggressive_turn", 0.12, aggressive_turn_attrs),
    EventTypeSpec("speeding", 0.22, speeding_attrs),
    EventTypeSpec("tailgating", 0.10, tailgating_attrs),
    EventTypeSpec("late_night_driving", 0.08, late_night_attrs),
    # Background 'normal' telemetry pings (no safety incident) to provide exposure context
    EventTypeSpec("ping", 0.30, lambda rng: {}),
]

TOTAL_WEIGHT = sum(e.probability for e in EVENT_TYPE_CONFIG)


@dataclass
class GeneratorConfig:
    drivers: int = 10
    seed: int = 42
    base_lat: float = 37.7749  # SF ref
    base_lon: float = -122.4194
    lat_jitter_deg: float = 0.25
    lon_jitter_deg: float = 0.25
    min_speed: float = 0.0
    max_speed: float = 82.0
    trip_avg_minutes: int = 28
    trip_std_minutes: int = 9
    # Rate of new trip start probability per minute per driver when idle
    trip_start_prob: float = 0.07
    # Extreme variance mode introduces driver risk profiles & amplified behaviors
    extreme_variance: bool = False


RISK_PROFILE_KEYS = [
    "ultra_safe",
    "safe",
    "moderate",
    "risky",
    "ultra_risky",
]

# Multipliers applied to base event probabilities per driver profile (extreme mode only)
PROFILE_EVENT_MULTIPLIERS: Dict[str, Dict[str, float]] = {
    "ultra_safe": {
        "hard_braking": 0.1,
        "aggressive_turn": 0.1,
        "speeding": 0.12,
        "tailgating": 0.08,
        "late_night_driving": 0.05,
        "ping": 2.5,
    },
    "safe": {
        "hard_braking": 0.4,
        "aggressive_turn": 0.4,
        "speeding": 0.5,
        "tailgating": 0.4,
        "late_night_driving": 0.4,
        "ping": 1.6,
    },
    "moderate": {  # baseline ~1.0
        "hard_braking": 1.0,
        "aggressive_turn": 1.0,
        "speeding": 1.0,
        "tailgating": 1.0,
        "late_night_driving": 1.0,
        "ping": 1.0,
    },
    "risky": {
        "hard_braking": 3.9,
        "aggressive_turn": 6.8,
        "speeding": 5.0,
        "tailgating": 4.2,
        "late_night_driving": 5.4,
        "ping": 0.7,
    },
    "ultra_risky": {
        "hard_braking": 10.2,
        "aggressive_turn": 9.0,
        "speeding": 10.5,
        "tailgating": 9.8,
        "late_night_driving": 12.2,
        "ping": 0.2,
    },
}

# Profile selection weights in extreme variance mode
PROFILE_SELECTION_WEIGHTS = [0.05, 0.25, 0.40, 0.20, 0.10]  # must align with RISK_PROFILE_KEYS


class TelemetryGenerator:
    def __init__(self, config: GeneratorConfig):
        self.cfg = config
        self.rng = random.Random(config.seed)
        self._driver_state: Dict[str, Dict[str, Any]] = {}
        self._driver_profile: Dict[str, str] = {}
        self._driver_car: Dict[str, Dict[str, Any]] = {}

    def _assign_car(self, driver_id: str) -> Dict[str, Any]:
        """Assign static car attributes per driver (value & sportiness).

        car_value: approximate replacement cost (USD) drawn log-normal by category.
        car_sportiness: 0-1 scale capturing performance/driver risk appetite proxy.
        car_type: categorical label for future embedding / analysis.
        """
        car = self._driver_car.get(driver_id)
        if car:
            return car
        # simple categorical distribution
        car_types = [
            ("economy", 0.30, 18_000, 0.15),
            ("sedan", 0.35, 28_000, 0.25),
            ("suv", 0.18, 40_000, 0.30),
            ("luxury", 0.10, 65_000, 0.40),
            ("sports", 0.05, 85_000, 0.70),
            ("super", 0.02, 140_000, 0.90),
        ]
        r = self.rng.random()
        acc = 0.0
        chosen = car_types[-1]
        for ct, w, base_val, sport in car_types:
            acc += w
            if r <= acc:
                chosen = (ct, w, base_val, sport)
                break
        ct, _w, base_val, sport = chosen
        # value variation: +/- 20% noise log-normal-ish
        value = int(base_val * self.rng.uniform(0.85, 1.25))
        car = {
            "car_type": ct,
            "car_value": value,
            "car_sportiness": round(sport + self.rng.uniform(-0.05, 0.05), 3),
        }
        car["car_sportiness"] = float(min(1.0, max(0.0, car["car_sportiness"])))
        self._driver_car[driver_id] = car
        return car

    def _assign_profile(self, driver_id: str) -> str:
        if not self.cfg.extreme_variance:
            return "moderate"
        profile = self._driver_profile.get(driver_id)
        if profile:
            return profile
        # weighted choice
        r = self.rng.random()
        acc = 0.0
        for key, w in zip(RISK_PROFILE_KEYS, PROFILE_SELECTION_WEIGHTS):
            acc += w
            if r <= acc:
                profile = key
                break
        else:  # fallback
            profile = RISK_PROFILE_KEYS[-1]
        self._driver_profile[driver_id] = profile
        return profile

    def _choose_event_type(self, profile: str) -> EventTypeSpec:
        if not self.cfg.extreme_variance:
            r = self.rng.uniform(0, TOTAL_WEIGHT)
            acc = 0.0
            for spec in EVENT_TYPE_CONFIG:
                acc += spec.probability
                if r <= acc:
                    return spec
            return EVENT_TYPE_CONFIG[-1]
        mults = PROFILE_EVENT_MULTIPLIERS[profile]
        total = 0.0
        for spec in EVENT_TYPE_CONFIG:
            total += spec.probability * mults.get(spec.name, 1.0)
        r = self.rng.uniform(0, total)
        acc = 0.0
        for spec in EVENT_TYPE_CONFIG:
            w = spec.probability * mults.get(spec.name, 1.0)
            acc += w
            if r <= acc:
                return spec
        return EVENT_TYPE_CONFIG[-1]

    def _ensure_trip(self, driver_id: str, current_minute: int) -> str:
        state = self._driver_state.setdefault(driver_id, {"trip_id": None, "trip_end_min": 0})
        trip_id = state.get("trip_id")
        if trip_id is None or current_minute >= state.get("trip_end_min", 0):
            # Start new trip with probability or if forced end
            if self.rng.random() < self.cfg.trip_start_prob or trip_id is None:
                duration = max(
                    5,
                    int(
                        self.rng.normalvariate(self.cfg.trip_avg_minutes, self.cfg.trip_std_minutes)
                    ),
                )
                trip_id = f"T-{uuid.uuid4().hex[:10]}"
                state["trip_id"] = trip_id
                state["trip_end_min"] = current_minute + duration
        return state["trip_id"]

    def events(self, start_time: Optional[datetime] = None) -> Iterator[TelemetryEvent]:
        if start_time is None:
            start_time = datetime.now(timezone.utc)
        current_minute = 0
        while True:
            for d in range(self.cfg.drivers):
                driver_id = f"D{d:04d}"
                profile = self._assign_profile(driver_id)
                trip_id = self._ensure_trip(driver_id, current_minute)
                spec = self._choose_event_type(profile)
                base_speed = self.rng.uniform(self.cfg.min_speed, self.cfg.max_speed)
                speed_factor = 1.0
                if spec.name == "ping":
                    speed_factor = 0.6
                if self.cfg.extreme_variance:
                    # profile-specific speed scaling to influence exposure denominator
                    if profile in ("ultra_safe", "safe"):
                        speed_factor *= 1.1  # slightly higher miles -> lowers per-100 event rates
                    elif profile in ("risky", "ultra_risky"):
                        speed_factor *= 0.8  # fewer miles -> higher per-100 metrics
                if spec.name == "late_night_driving":
                    # Force timestamp into late-night window for semantics
                    event_ts = (start_time + timedelta(minutes=current_minute)).replace(
                        hour=self.rng.choice([0, 1, 2, 3]), minute=self.rng.randint(0, 59)
                    )
                else:
                    event_ts = start_time + timedelta(minutes=current_minute)

                evt: TelemetryEvent = {
                    "event_id": uuid.uuid4().hex,
                    "driver_id": driver_id,
                    "trip_id": trip_id,
                    "ts": event_ts.strftime(ISO),
                    "event_type": spec.name,
                    "latitude": round(
                        self.cfg.base_lat
                        + self.rng.uniform(-self.cfg.lat_jitter_deg, self.cfg.lat_jitter_deg),
                        6,
                    ),
                    "longitude": round(
                        self.cfg.base_lon
                        + self.rng.uniform(-self.cfg.lon_jitter_deg, self.cfg.lon_jitter_deg),
                        6,
                    ),
                    "speed_mph": round(base_speed * speed_factor, 1),
                    "heading_deg": self.rng.randint(0, 359),
                    "period_minute": current_minute,
                }
                # attach static car attributes once per driver
                evt.update(self._assign_car(driver_id))
                evt.update(spec.attribute_fn(self.rng))
                if self.cfg.extreme_variance:
                    # Amplify / dampen type-specific intensities for extremes
                    if profile in ("risky", "ultra_risky"):
                        if spec.name == "speeding":
                            if "duration_sec" in evt:
                                evt["duration_sec"] = int(
                                    evt["duration_sec"] * (2.0 if profile == "risky" else 3.2)
                                )
                            if "over_speed_mph" in evt:
                                evt["over_speed_mph"] = round(
                                    float(evt["over_speed_mph"])
                                    * (1.6 if profile == "risky" else 2.2),
                                    1,
                                )
                        if spec.name == "hard_braking" and "braking_g" in evt:
                            evt["braking_g"] = round(
                                min(2.5, evt["braking_g"] * (1.4 if profile == "risky" else 1.8)), 2
                            )
                        if spec.name == "aggressive_turn" and "lateral_g" in evt:
                            evt["lateral_g"] = round(
                                min(3.0, evt["lateral_g"] * (1.4 if profile == "risky" else 1.9)), 2
                            )
                    elif profile in ("ultra_safe", "safe"):
                        if spec.name == "speeding" and "duration_sec" in evt:
                            evt["duration_sec"] = max(5, int(evt["duration_sec"] * 0.5))
                        if spec.name == "hard_braking" and "braking_g" in evt:
                            evt["braking_g"] = round(evt["braking_g"] * 0.7, 2)
                        if spec.name == "aggressive_turn" and "lateral_g" in evt:
                            evt["lateral_g"] = round(evt["lateral_g"] * 0.7, 2)
                if self.cfg.extreme_variance:
                    evt["driver_profile"] = profile
                yield evt
            current_minute += 1


# ------------------------------- Output Helpers ---------------------------------


def write_jsonl(events: Iterable[TelemetryEvent], out_path: str, limit: int) -> None:
    with open(out_path, "w", encoding="utf-8") as f:
        for i, evt in enumerate(events):
            if i >= limit:
                break
            f.write(json.dumps(evt) + "\n")


def write_csv(events: Iterable[TelemetryEvent], out_path: str, limit: int) -> None:
    first = True
    fieldnames: List[str] = []
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer: Optional[csv.DictWriter] = None
        for i, evt in enumerate(events):
            if i >= limit:
                break
            if first:
                fieldnames = sorted(evt.keys())
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                first = False
            assert writer is not None
            writer.writerow(evt)


def stream_stdout(events: Iterable[TelemetryEvent], delay: float) -> None:
    for evt in events:
        print(json.dumps(evt), flush=True)
        time.sleep(delay)


# ------------------------------- CLI Interface ----------------------------------


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate mock telematics events.")
    p.add_argument("--events", type=int, default=1000, help="Total events to emit (batch mode)")
    p.add_argument("--drivers", type=int, default=10, help="Number of synthetic drivers")
    p.add_argument("--seed", type=int, default=42, help="Random seed")
    p.add_argument(
        "--out", type=str, help="Output file path (JSONL or CSV inferred from extension)"
    )
    p.add_argument(
        "--format", choices=["jsonl", "csv"], help="Force output format (overrides extension)"
    )
    p.add_argument(
        "--stream", action="store_true", help="Continuously stream to stdout (ignore --events)"
    )
    p.add_argument(
        "--interval", type=float, default=0.5, help="Seconds between streamed events (stream mode)"
    )
    p.add_argument(
        "--extreme-variance",
        action="store_true",
        help="Enable driver risk profiles for wider metric variance",
    )
    return p.parse_args(argv)


def detect_format(out_path: str, forced: Optional[str]) -> str:
    if forced:
        return forced
    if out_path.lower().endswith(".csv"):
        return "csv"
    return "jsonl"


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    cfg = GeneratorConfig(
        drivers=args.drivers, seed=args.seed, extreme_variance=args.extreme_variance
    )
    gen = TelemetryGenerator(cfg)
    event_iter = gen.events()

    if args.stream:
        stream_stdout(event_iter, args.interval)
        return

    if not args.out:
        print(
            "--out is required in batch mode (omit or use --stream for streaming)", file=sys.stderr
        )
        sys.exit(2)

    fmt = detect_format(args.out, args.format)
    if fmt == "jsonl":
        write_jsonl(event_iter, args.out, args.events)
    else:
        write_csv(event_iter, args.out, args.events)
    print(f"Wrote {args.events} events to {args.out} ({fmt})")


if __name__ == "__main__":  # pragma: no cover
    main()
