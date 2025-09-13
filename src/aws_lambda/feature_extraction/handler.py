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

try:
    # When deployed, the code might be placed at the root of the Lambda zip (no package parent),
    # so relative import `.features` can fail. Prefer absolute import fallback.
    from features.registry import load_feature_calculators  # type: ignore
except Exception:  # pragma: no cover
    from .features.registry import load_feature_calculators  # type: ignore

FEATURES_STREAM = os.getenv("FEATURES_STREAM_NAME")
PK_FIELD = os.getenv("FEATURES_PARTITION_KEY_FIELD", "driver_id")
PERIOD_GRANULARITY = os.getenv("PERIOD_GRANULARITY", "MONTH").upper()
MIN_EXPOSURE_MILES = float(os.getenv("MIN_EXPOSURE_MILES", "5.0"))
SAGEMAKER_ENDPOINT = os.getenv("SAGEMAKER_ENDPOINT_NAME", "").strip()
TELEMETRY_TABLE_NAME = os.getenv("TELEMETRY_TABLE_NAME", "").strip()
PRICING_LAMBDA = os.getenv("PRICING_LAMBDA_NAME", "").strip()

_kinesis_client = None
_sagemaker_runtime = None
_ddb_client = None
_lambda_client = None
_lambda_client = None


def _get_kinesis():
    global _kinesis_client  # noqa: PLW0603
    if _kinesis_client is None and boto3 is not None:
        _kinesis_client = boto3.client("kinesis")
    return _kinesis_client


def _get_smr():
    global _sagemaker_runtime  # noqa: PLW0603
    if _sagemaker_runtime is None and boto3 is not None and SAGEMAKER_ENDPOINT:
        _sagemaker_runtime = boto3.client("sagemaker-runtime")
    return _sagemaker_runtime


def _get_ddb():
    global _ddb_client  # noqa: PLW0603
    if _ddb_client is None and boto3 is not None and TELEMETRY_TABLE_NAME:
        _ddb_client = boto3.client("dynamodb")
    return _ddb_client


def _get_lambda():
    global _lambda_client  # noqa: PLW0603
    if _lambda_client is None and boto3 is not None and PRICING_LAMBDA:
        _lambda_client = boto3.client("lambda")
    return _lambda_client


def _get_lambda():
    global _lambda_client  # noqa: PLW0603
    if _lambda_client is None and boto3 is not None:
        _lambda_client = boto3.client("lambda")
    return _lambda_client


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

    predictions: List[Dict[str, Any]] = []
    # Index feature rows for later pricing enrichment
    row_index = {(r.get("driver_id"), r.get("period_key")): r for r in feature_rows}
    pricing_name = os.getenv("PRICING_LAMBDA_NAME", "").strip()
    if SAGEMAKER_ENDPOINT and feature_rows:
        smr = _get_smr()
        if smr is not None:
            for row in feature_rows:
                payload_obj = {k: v for k, v in row.items() if not isinstance(v, (dict, list))}
                try:
                    body = json.dumps(payload_obj)
                    resp = smr.invoke_endpoint(
                        EndpointName=SAGEMAKER_ENDPOINT,
                        ContentType="application/json",
                        Body=body,
                        Accept="application/json",
                    )
                    raw = resp.get("Body").read().decode("utf-8")
                    pred_json = json.loads(raw)
                    # Flatten if prediction returns nested object list etc.
                    pricing_payload = {
                        **{k: v for k, v in row.items() if not isinstance(v, (dict, list))},
                    }
                    # If SageMaker returned risk & multiplier attach them
                    if isinstance(pred_json, dict):
                        for k in ("risk_score", "premium_multiplier"):
                            if k in pred_json:
                                # map premium_multiplier to model_premium_multiplier expected by pricing lambda
                                if k == "premium_multiplier":
                                    pricing_payload["model_premium_multiplier"] = pred_json[k]
                                else:
                                    pricing_payload[k] = pred_json[k]
                    priced_result = None
                    if pricing_name:
                        try:
                            lmb = _get_lambda()
                            if lmb is not None:
                                resp_lambda = lmb.invoke(
                                    FunctionName=pricing_name,
                                    InvocationType="RequestResponse",
                                    Payload=json.dumps({"body": json.dumps(pricing_payload)}).encode("utf-8"),
                                )
                                raw_body = resp_lambda.get("Payload").read().decode("utf-8")  # type: ignore
                                body_json = json.loads(raw_body)
                                if isinstance(body_json, dict) and body_json.get("statusCode") == 200:
                                    priced_obj = json.loads(body_json.get("body", "{}"))
                                    items = priced_obj.get("items") if isinstance(priced_obj, dict) else None
                                    if items:
                                        priced_result = items[0]
                        except Exception:  # pragma: no cover
                            priced_result = None
                    predictions.append({
                        "driver_id": row.get("driver_id"),
                        "period_key": row.get("period_key"),
                        "prediction": priced_result or pred_json,
                    })
                except Exception:  # pragma: no cover
                    continue

    # ---------------- Optional Pricing Engine Enrichment ----------------
    priced_items: List[Dict[str, Any]] = []
    if PRICING_LAMBDA and predictions:
        lclient = _get_lambda()
        if lclient is not None:
            for pred in predictions:
                try:
                    driver_id = pred.get("driver_id")
                    period_key = pred.get("period_key")
                    base_row = dict(row_index.get((driver_id, period_key), {}))
                    pred_json = pred.get("prediction", {})
                    # Extract single values if lists
                    risk = pred_json.get("risk_score")
                    mult = pred_json.get("premium_multiplier")
                    if isinstance(risk, list):
                        risk = risk[0] if risk else None
                    if isinstance(mult, list):
                        mult = mult[0] if mult else None
                    if risk is not None:
                        base_row["risk_score"] = risk
                    if mult is not None:
                        base_row["model_premium_multiplier"] = mult
                    invoke_payload = {"body": json.dumps(base_row)}
                    resp = lclient.invoke(
                        FunctionName=PRICING_LAMBDA,
                        InvocationType="RequestResponse",
                        Payload=json.dumps(invoke_payload).encode("utf-8"),
                    )
                    raw_body = resp.get("Payload").read().decode("utf-8")  # type: ignore
                    parsed = json.loads(raw_body)
                    if parsed.get("statusCode") == 200:
                        body_obj = json.loads(parsed.get("body", "{}"))
                        items = body_obj.get("items") or []
                        if items:
                            priced_items.append({
                                "driver_id": driver_id,
                                "period_key": period_key,
                                "priced": items[0],
                            })
                except Exception:  # pragma: no cover
                    continue

    # ---------------- Persistence into single-table DynamoDB ----------------
    # Prefer priced items; fall back to raw predictions
    persistence_items = priced_items if priced_items else predictions
    if persistence_items and TELEMETRY_TABLE_NAME:
        ddbc = _get_ddb()
        if ddbc is not None:
            for item in persistence_items:
                try:
                    driver_id = str(item.get("driver_id"))
                    period_key = str(item.get("period_key"))
                    ts_epoch = int(datetime.utcnow().timestamp())
                    # PK/SK pattern: driver partition, prediction sort key with period granularity
                    pk = f"DRIVER#{driver_id}"
                    sk = f"PREDICTION#{period_key}"
                    prediction_obj = item.get("prediction") if "prediction" in item else None
                    priced_obj = item.get("priced") if "priced" in item else None
                    ddb_item = {
                        "PK": {"S": pk},
                        "SK": {"S": sk},
                        "GSI1PK": {"S": pk},
                        "GSI1SK": {"S": f"PRED#{period_key}"},
                        "GSI2PK": {"S": pk},
                        "GSI2SK": {"S": f"PRED#{period_key}"},
                        "entity_type": {"S": "prediction"},
                        "driver_id": {"S": driver_id},
                        "period_key": {"S": period_key},
                        "ts": {"N": str(ts_epoch)},
                    }
                    if prediction_obj is not None:
                        try:
                            ddb_item["prediction_json"] = {"S": json.dumps(prediction_obj)}
                        except Exception:  # pragma: no cover
                            pass
                    if priced_obj is not None:
                        try:
                            ddb_item["pricing_json"] = {"S": json.dumps(priced_obj)}
                        except Exception:  # pragma: no cover
                            pass
                    ddbc.put_item(TableName=TELEMETRY_TABLE_NAME, Item=ddb_item)
                except Exception:  # pragma: no cover
                    continue

            # Create period aggregate items (schema-aligned) for each priced or predicted record
            for item in persistence_items:
                try:
                    driver_id = str(item.get("driver_id"))
                    period_key = str(item.get("period_key"))
                    ts_epoch = int(datetime.utcnow().timestamp())
                    period_pk = f"USER#{driver_id}"
                    period_sk = f"PERIOD#{period_key}"
                    prediction_obj = item.get("prediction") if "prediction" in item else None
                    priced_obj = item.get("priced") if "priced" in item else None
                    risk_score = None
                    final_premium = None
                    model_mult = None
                    base_premium = None
                    if priced_obj:
                        risk_score = priced_obj.get("risk_score")
                        pricing_block = priced_obj.get("pricing") or {}
                        final_premium = pricing_block.get("final_monthly_premium")
                        model_mult = pricing_block.get("model_multiplier") or priced_obj.get("model_premium_multiplier")
                        base_premium = pricing_block.get("base_premium")
                    elif prediction_obj:
                        rlist = prediction_obj.get("risk_score") if isinstance(prediction_obj, dict) else None
                        if isinstance(rlist, list) and rlist:
                            risk_score = rlist[0]
                        mlist = prediction_obj.get("premium_multiplier") if isinstance(prediction_obj, dict) else None
                        if isinstance(mlist, list) and mlist:
                            model_mult = mlist[0]
                    period_item = {
                        "PK": {"S": period_pk},
                        "SK": {"S": period_sk},
                        "GSI2PK": {"S": f"PERIOD#{period_key}"},
                        "GSI2SK": {"S": driver_id},
                        "entity_type": {"S": "period"},
                        "driver_id": {"S": driver_id},
                        "period_key": {"S": period_key},
                        "ts": {"N": str(ts_epoch)},
                    }
                    if risk_score is not None:
                        try: period_item["risk_score"] = {"N": str(float(risk_score))}
                        except Exception:  # pragma: no cover
                            pass
                    if final_premium is not None:
                        try: period_item["final_monthly_premium"] = {"N": str(float(final_premium))}
                        except Exception:  # pragma: no cover
                            pass
                    if model_mult is not None:
                        try: period_item["model_multiplier"] = {"N": str(float(model_mult))}
                        except Exception:  # pragma: no cover
                            pass
                    if base_premium is not None:
                        try: period_item["base_premium"] = {"N": str(float(base_premium))}
                        except Exception:  # pragma: no cover
                            pass
                    try:
                        ddbc.put_item(TableName=TELEMETRY_TABLE_NAME, Item=period_item)
                    except Exception:  # pragma: no cover
                        pass
                except Exception:  # pragma: no cover
                    continue

    # Replace placeholders in last put_item call by terraform-time static code? Not applicable. We need dynamic attribute assembly above.

    return {
        "status": "ok",
        "input_events": len(events),
        "feature_rows": len(feature_rows),
        "kinesis": meta,
        "predictions": len(predictions),
        "sagemaker_enabled": bool(SAGEMAKER_ENDPOINT),
    }


if __name__ == "__main__":  # pragma: no cover
    print(lambda_handler({"Records": []}, None))
