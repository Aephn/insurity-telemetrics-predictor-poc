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
        for calc in calculators:
            try:
                calc.update(bucket[calc.name], evt)
            except Exception:  # pragma: no cover
                continue

    # finalize
    out_rows: List[Dict[str, Any]] = []
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
