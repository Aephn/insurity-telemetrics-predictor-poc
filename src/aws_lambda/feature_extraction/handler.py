"""Feature Extraction Lambda

Consumes event-level telemetry (raw or enriched) from a Kinesis stream, aggregates
into driver-period feature rows consistent with the model's expected schema.

Extensible: add new calculators in `features/` and register them.
"""
from __future__ import annotations

import os
import json
import base64
import calendar
from datetime import datetime
import hashlib
from typing import Any, Dict, List, Tuple, DefaultDict
from collections import defaultdict

try:
    import boto3  # type: ignore
    from botocore.exceptions import BotoCoreError, ClientError  # type: ignore
except Exception:  # pragma: no cover
    boto3 = None  # type: ignore
    BotoCoreError = ClientError = Exception  # type: ignore

from .features.registry import load_feature_calculators

FEATURES_STREAM = os.getenv("FEATURES_STREAM_NAME")
PK_FIELD = os.getenv("FEATURES_PARTITION_KEY_FIELD", "driver_id")
PERIOD_GRANULARITY = os.getenv("PERIOD_GRANULARITY", "MONTH").upper()
MIN_EXPOSURE_MILES = float(os.getenv("MIN_EXPOSURE_MILES", "5.0"))

_kinesis_client = None


def _get_kinesis():
    global _kinesis_client  # noqa: PLW0603
    if _kinesis_client is None and boto3 is not None:
        _kinesis_client = boto3.client("kinesis")
    return _kinesis_client


def _period_key(ts: str) -> Tuple[str, str, str]:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if PERIOD_GRANULARITY == "DAY":
        key = dt.strftime("%Y-%m-%d")
        start = key
        end = key
    elif PERIOD_GRANULARITY == "HOUR":
        key = dt.strftime("%Y-%m-%dT%H")
        start = key + ":00:00"
        end = start
    else:  # MONTH
        key = dt.strftime("%Y-%m")
        last_day = calendar.monthrange(dt.year, dt.month)[1]
        start = f"{key}-01"
        end = f"{key}-{last_day:02d}"
    return key, start, end


def _decode_kinesis(event: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for rec in event.get("Records", []):
        try:
            payload = base64.b64decode(rec["kinesis"]["data"]).decode()
            obj = json.loads(payload)
            out.append(obj)
        except Exception:  # pragma: no cover
            continue
    return out


def _aggregate(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    calculators = load_feature_calculators()
    # state[(driver_id, period_key)] = { 'calc_name': state_dict , '_shared': {exposure_miles,..}, 'meta': {...}}
    state: DefaultDict[Tuple[str, str], Dict[str, Any]] = defaultdict(dict)

    for evt in events:
        driver = evt.get("driver_id")
        ts = evt.get("ts")
        if not driver or not ts:
            continue
        period_key, start, end = _period_key(ts)
        bucket_key = (driver, period_key)
        bucket = state[bucket_key]
        if not bucket:
            bucket["_shared"] = {"period_start": start, "period_end": end}
            bucket["meta"] = {}
            for calc in calculators:
                bucket[calc.name] = calc.init_state()
            # capture static car attributes on first bucket creation
            if "car_value" in evt:
                bucket["meta"]["car_value"] = evt.get("car_value")
            if "car_sportiness" in evt:
                bucket["meta"]["car_sportiness"] = evt.get("car_sportiness")
            if "car_type" in evt:
                bucket["meta"]["car_type"] = evt.get("car_type")
        # If not previously set (mid-period first appearance) set them
        else:
            if "car_value" in evt and "car_value" not in bucket["meta"]:
                bucket["meta"]["car_value"] = evt.get("car_value")
            if "car_sportiness" in evt and "car_sportiness" not in bucket["meta"]:
                bucket["meta"]["car_sportiness"] = evt.get("car_sportiness")
            if "car_type" in evt and "car_type" not in bucket["meta"]:
                bucket["meta"]["car_type"] = evt.get("car_type")
        for calc in calculators:
            try:
                calc.update(bucket[calc.name], evt)
            except Exception:  # pragma: no cover
                continue

    # finalize
    out_rows: List[Dict[str, Any]] = []
    static_cache: Dict[str, Dict[str, Any]] = {}

    def _static_for(driver_id: str) -> Dict[str, Any]:
        st = static_cache.get(driver_id)
        if st:
            return st
        dh_local = int(hashlib.sha256(driver_id.encode("utf-8")).hexdigest()[:8], 16)
        bucket_pct = dh_local % 100
        if bucket_pct < 30:
            base_val = 18_000
        elif bucket_pct < 65:
            base_val = 28_000
        elif bucket_pct < 83:
            base_val = 40_000
        elif bucket_pct < 93:
            base_val = 65_000
        elif bucket_pct < 98:
            base_val = 85_000
        else:
            base_val = 140_000
        car_value = int(base_val * (1.0 + ((dh_local >> 8) % 21 - 10) / 100.0))
        car_sportiness = round(min(1.0, max(0.0, 0.1 + ((dh_local >> 16) % 70) / 100.0)), 3)
        st = {"car_value": car_value, "car_sportiness": car_sportiness}
        static_cache[driver_id] = st
        return st

    for (driver, period_key), bucket in state.items():
        shared = bucket["_shared"]
        row: Dict[str, Any] = {
            "driver_id": driver,
            "period_key": period_key,
            "period_start": shared["period_start"],
            "period_end": shared["period_end"],
            "feature_version": 1,
        }
        feature_values: Dict[str, Any] = {}
        for calc in calculators:
            try:
                feature_values.update(calc.finalize(bucket[calc.name], shared))
            except Exception:  # pragma: no cover
                continue
        row.update(feature_values)
        meta_info = bucket.get("meta", {})
        if meta_info:
            # pass through static car attributes (non-normalized)
            if "car_value" in meta_info:
                row["car_value"] = meta_info["car_value"]
            if "car_sportiness" in meta_info:
                row["car_sportiness"] = meta_info["car_sportiness"]
            if "car_type" in meta_info:
                row["car_type"] = meta_info["car_type"]

        # ---------------- Fallback synthetic enrichment (if upstream generator lacked static attrs) ----------------
        # Deterministic per driver so training / scoring remain stable between runs.
        dh = int(hashlib.sha256(driver.encode("utf-8")).hexdigest()[:8], 16)

        if "car_value" not in row or "car_sportiness" not in row:
            static_vals = _static_for(driver)
            row.setdefault("car_value", static_vals["car_value"])
            row.setdefault("car_sportiness", static_vals["car_sportiness"])

        # Derive / recompute prior_claim_count (if missing or zero) for synthetic variation
        if "prior_claim_count" not in row or row.get("prior_claim_count", 0) == 0:
            hbr = float(row.get("hard_braking_events_per_100mi", 0.0) or 0.0)
            atr = float(row.get("aggressive_turning_events_per_100mi", 0.0) or 0.0)
            tgr = float(row.get("tailgating_time_ratio", 0.0) or 0.0) * 15.0  # scaled down
            spd = float(row.get("speeding_minutes_per_100mi", 0.0) or 0.0) * 0.5
            composite = hbr * 0.4 + atr * 0.3 + spd * 0.4 + tgr * 0.6
            # Map into 0-3 using compact thresholds
            thresholds = [1.2, 3.0, 6.0]
            bucket_idx = 0
            for t in thresholds:
                if composite >= t:
                    bucket_idx += 1
            # slight deterministic variance: bump some drivers up one tier at boundary
            if bucket_idx < 3 and (dh % 11 == 0):
                bucket_idx += 1
            row["prior_claim_count"] = int(min(3, bucket_idx))

        # Preserve raw and create normalized car value
        if "car_value" in row:
            try:
                raw_val = float(row["car_value"])
                row["car_value_raw"] = int(raw_val)
                row["car_value_norm"] = raw_val / 10000.0
            except Exception:
                row["car_value_norm"] = row.get("car_value")
        # Interaction feature
        if "car_sportiness" in row and "speeding_minutes_per_100mi" in row:
            try:
                row["car_speeding_interaction"] = float(row["car_sportiness"]) * float(row.get("speeding_minutes_per_100mi", 0.0))
            except Exception:
                pass
        # Skip low exposure
        if row.get("miles", 0.0) < MIN_EXPOSURE_MILES:
            continue
        out_rows.append(row)
    return out_rows


def _emit_features(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not FEATURES_STREAM or not rows:
        return {"enabled": False, "count": len(rows)}
    client = _get_kinesis()
    if client is None:
        return {"enabled": False, "error": "boto3_missing"}
    entries = []
    for r in rows:
        data = json.dumps(r).encode("utf-8")
        if len(data) > 1_000_000:
            continue
        pk = str(r.get(PK_FIELD) or r.get("driver_id") or "default")
        entries.append({"Data": data, "PartitionKey": pk})
    success = 0
    failed = 0
    for i in range(0, len(entries), 500):
        batch = entries[i : i + 500]
        try:
            resp = client.put_records(StreamName=FEATURES_STREAM, Records=batch)
            for rec in resp.get("Records", []):
                if rec.get("ErrorCode"):
                    failed += 1
                else:
                    success += 1
        except (BotoCoreError, ClientError):  # pragma: no cover
            failed += len(batch)
    return {"enabled": True, "success": success, "failed": failed}


def lambda_handler(event, context):  # type: ignore
    events = _decode_kinesis(event)
    feature_rows = _aggregate(events)
    meta = _emit_features(feature_rows)
    return {"status": "ok", "input_events": len(events), "feature_rows": len(feature_rows), "kinesis": meta}


if __name__ == "__main__":  # pragma: no cover
    # Simple local test with no records
    print(lambda_handler({"Records": []}, None))
