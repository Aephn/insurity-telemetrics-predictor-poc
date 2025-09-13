"""Pricing Engine Lambda (Local-Friendly)

Accepts driver-period feature rows, invokes the trained XGBoost model to obtain
risk-based multipliers, layers business rule adjustments, and returns a final
monthly premium suitable for frontend display.

Input (event): JSON body of one feature row or list of rows. Example row:
{
  "driver_id": "D0007",
  "period_key": "2025-01",
  "miles": 843.27,
  "hard_braking_events_per_100mi": 3.18,
  ... feature columns ...
}

Environment Variables:
  MODEL_ARTIFACTS_DIR (default 'artifacts')
  BASE_MONTHLY_PREMIUM (default 110)  -> nominal base before telematics factor
  MIN_PREMIUM (default 50)
  MAX_PREMIUM (default 400)
  MIN_FACTOR (default 0.7)
  MAX_FACTOR (default 1.5)

Output: JSON with enriched pricing breakdown per input row.

No AWS services required (Kinesis/S3/etc.). This can be run locally:
  PYTHONPATH=. python src/aws_lambda/pricing_engine/handler.py sample_features.json
"""

from __future__ import annotations

import json
import os
import sys
import time
import random
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pandas as pd

try:
    from models.aws_sagemaker.xgboost_model import (  # type: ignore
        ModelArtifacts,
        FEATURE_COLUMNS,
        predict_fn,
        synthesize_dataset_improved,
    )
except Exception:  # pragma: no cover
    # Allow running in a minimal inference context if the training module isn't packaged.
    from typing import Any as _Any  # fallback
    ModelArtifacts = _Any  # type: ignore
    FEATURE_COLUMNS = []  # type: ignore
    def predict_fn(df, model):  # type: ignore
        raise RuntimeError("predict_fn unavailable; model artifacts module not packaged")
try:
    # When deployed, the module may be at the root (no package context), so relative import can fail.
    from formulas import compute_behavior_adjustments, finalize_multiplier, compute_price  # type: ignore
except Exception:  # pragma: no cover
    from .formulas import compute_behavior_adjustments, finalize_multiplier, compute_price  # type: ignore

MODEL_DIR = Path(os.getenv("MODEL_ARTIFACTS_DIR", "artifacts"))
# Canonical base premium for display & pricing seed. Prefer BASE_PREMIUM; fall back once to legacy BASE_MONTHLY_PREMIUM.
BASE_PREMIUM = float(os.getenv("BASE_PREMIUM", os.getenv("BASE_MONTHLY_PREMIUM", "190")))
MIN_PREMIUM = float(os.getenv("MIN_PREMIUM", "50"))
MAX_PREMIUM = float(os.getenv("MAX_PREMIUM", "400"))
MIN_FACTOR = float(os.getenv("MIN_FACTOR", "0.7"))
MAX_FACTOR = float(os.getenv("MAX_FACTOR", "1.5"))

_ARTIFACTS: Any = None  # use Any for runtime flexibility when model module absent
TELEMETRY_TABLE = os.getenv("TELEMETRY_TABLE", "").strip()
USE_SYNTHETIC_FALLBACK = os.getenv("USE_SYNTHETIC_FALLBACK", "0") == "1"

try:  # boto3 present in AWS runtime
    import boto3  # type: ignore
    from botocore.exceptions import BotoCoreError, ClientError  # type: ignore
except Exception:  # pragma: no cover - local minimal env
    boto3 = None  # type: ignore
    BotoCoreError = ClientError = Exception  # type: ignore

_ddb_client = None

def _get_ddb():  # lazy
    global _ddb_client
    if _ddb_client is None and boto3 is not None and TELEMETRY_TABLE:
        try:
            _ddb_client = boto3.client("dynamodb")
        except Exception:  # pragma: no cover
            _ddb_client = None
    return _ddb_client

# ------------------------------------------------------------------------------------
# Internal helpers for pricing
# ------------------------------------------------------------------------------------


def _load_model():  # return underlying model artifacts object
    global _ARTIFACTS  # noqa: PLW0603
    if _ARTIFACTS is None:
        if not (MODEL_DIR / "xgb_model.json").exists():
            raise RuntimeError(f"Model artifacts not found in {MODEL_DIR}. Train model first.")
        _ARTIFACTS = ModelArtifacts.load(MODEL_DIR)
    return _ARTIFACTS


def _ensure_dataframe(rows: List[Dict[str, Any]]):
    df = pd.DataFrame(rows)
    # Ensure required feature columns exist
    for c in FEATURE_COLUMNS:
        if c not in df.columns:
            df[c] = np.nan
    return df


def price_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not rows:
        return []
    need_model = any("risk_score" not in r or "model_premium_multiplier" not in r for r in rows)
    if need_model:
        model = _load_model()
        df = _ensure_dataframe(rows)
        preds = predict_fn(df[FEATURE_COLUMNS].copy(), model)
        risk_scores = preds["risk_score"]
        model_multipliers = preds["premium_multiplier"]
    else:
        # Use provided values directly
        risk_scores = [r.get("risk_score") for r in rows]
        model_multipliers = [r.get("model_premium_multiplier") for r in rows]

    output: List[Dict[str, Any]] = []
    for i, row in enumerate(rows):
        r = dict(row)  # shallow copy
        r.setdefault("risk_score", risk_scores[i])
        r.setdefault("model_premium_multiplier", model_multipliers[i])

        adjustments = compute_behavior_adjustments(row)
        mult_info = finalize_multiplier(
            model_multiplier=model_multipliers[i],
            adjustments=adjustments,
            min_factor=MIN_FACTOR,
            max_factor=MAX_FACTOR,
        )
        price_info = compute_price(
            base_premium=BASE_PREMIUM,
            final_multiplier=mult_info["final_multiplier"],
            min_premium=MIN_PREMIUM,
            max_premium=MAX_PREMIUM,
        )

        r.update(
            {
                "behavior_adjustments": adjustments,
                "pricing": {
                    **mult_info,
                    **price_info,
                    "base_premium": BASE_PREMIUM,
                    "min_premium": MIN_PREMIUM,
                    "max_premium": MAX_PREMIUM,
                },
            }
        )
        output.append(r)
    return output


# ------------------------------------------------------------------------------------
# Dashboard snapshot (synthetic) generation logic (inlined here so the shared bundle
# can satisfy both the pricing POST and dashboard GET endpoints).
# ------------------------------------------------------------------------------------

def _safety_score(row: Dict[str, Any]) -> int:
    hb = float(row.get("hard_braking_events_per_100mi", 0))
    sp = float(row.get("speeding_minutes_per_100mi", 0))
    tg = float(row.get("tailgating_time_ratio", 0)) * 100
    ln = float(row.get("late_night_miles_per_100mi", 0))
    turns = float(row.get("aggressive_turning_events_per_100mi", 0))
    raw = 100 - (hb * 2 + turns * 1.5 + sp * 1.2 + tg * 0.6 + ln * 1.8)
    return int(max(0, min(100, raw)))


def generate_dashboard_snapshot() -> Dict[str, Any]:  # mirrors original dashboard handler
    # If no table configured, fallback to synthetic
    if not TELEMETRY_TABLE:
        return _synthetic_snapshot()
    ddb = _get_ddb()
    if ddb is None:
        if USE_SYNTHETIC_FALLBACK:
            return _synthetic_snapshot()
        raise RuntimeError("dynamodb_unavailable")

    # 1. Discover a driver (query any PERIOD item). Use a small Scan with filter on begins_with(SK,'PERIOD#').
    try:
        scan = ddb.scan(
            TableName=TELEMETRY_TABLE,
            Limit=20,
            FilterExpression="begins_with(#sk, :p)",
            ExpressionAttributeNames={"#sk": "SK"},
            ExpressionAttributeValues={":p": {"S": "PERIOD#"}},
        )
        items = scan.get("Items") or []
        if not items:
            if USE_SYNTHETIC_FALLBACK:
                return _synthetic_snapshot()
            raise FileNotFoundError("no_data")
    except (BotoCoreError, ClientError):  # pragma: no cover
        if USE_SYNTHETIC_FALLBACK:
            return _synthetic_snapshot()
        raise

    # Pick first item
    driver_id = items[0].get("driver_id", {}).get("S") or _extract_driver_from_pk(items[0].get("PK", {}).get("S", ""))
    if not driver_id:
        if USE_SYNTHETIC_FALLBACK:
            return _synthetic_snapshot()
        raise RuntimeError("driver_not_found")

    # 2. Query period history for driver
    pk = f"USER#{driver_id}"
    try:
        q = ddb.query(
            TableName=TELEMETRY_TABLE,
            KeyConditionExpression="#pk = :pk AND begins_with(#sk, :per)",
            ExpressionAttributeNames={"#pk": "PK", "#sk": "SK"},
            ExpressionAttributeValues={":pk": {"S": pk}, ":per": {"S": "PERIOD#"}},
            Limit=12,
        )
        period_items = q.get("Items") or []
    except (BotoCoreError, ClientError):  # pragma: no cover
        period_items = []

    if not period_items:
        if USE_SYNTHETIC_FALLBACK:
            return _synthetic_snapshot()
        raise FileNotFoundError("no_period_history")

    monthly_scores: List[Dict[str, Any]] = []
    for it in sorted(period_items, key=lambda x: x.get("SK", {}).get("S", ""))[-6:]:
        sk = it.get("SK", {}).get("S", "")
        month = sk.split("#", 1)[-1]
        risk = _num(it.get("risk_score"))
        premium = _num(it.get("final_monthly_premium")) or _num(it.get("base_premium")) or 110.0
        model_mult = _num(it.get("model_multiplier")) or 1.0
        # Placeholder factor extraction: not stored yet; set zeros
        monthly_scores.append(
            {
                "month": month,
                "safetyScore": 100 - int((risk or 0) * 10) if risk is not None else 70,
                "premium": float(premium),
                "miles": 0.0,
                "riskScore": float(risk) if risk is not None else 0.5,
                "modelMultiplier": float(model_mult),
                "factors": {
                    "hardBraking": 0,
                    "aggressiveTurning": 0,
                    "followingDistance": 0,
                    "excessiveSpeeding": 0,
                    "lateNightDriving": 0,
                },
            }
        )

    # 3. Recent events (GSI1)
    events: List[Dict[str, Any]] = []
    try:
        ev = ddb.query(
            TableName=TELEMETRY_TABLE,
            IndexName="GSI1_EventsByUser",
            KeyConditionExpression="#gpk = :gpk",
            ExpressionAttributeNames={"#gpk": "GSI1PK"},
            ExpressionAttributeValues={":gpk": {"S": f"EVENTS#{driver_id}"}},
            Limit=20,
            ScanIndexForward=False,
        )
        for e in ev.get("Items", []):
            events.append(
                {
                    "id": e.get("SK", {}).get("S", "evt"),
                    "timestamp": e.get("timestamp", {}).get("S", ""),
                    "type": e.get("event_type", {}).get("S", "unknown"),
                    "severity": e.get("severity", {}).get("S", ""),
                    "value": _num(e.get("value")) or 0,
                    "speedMph": _num(e.get("speedMph")) or 0,
                }
            )
    except (BotoCoreError, ClientError):  # pragma: no cover
        pass

    base_prem_env = BASE_PREMIUM
    profile = {
        "id": driver_id,
        "name": f"Driver {driver_id[-4:]}" if len(driver_id) >= 4 else driver_id,
        "policyNumber": "POLICY-" + driver_id[-4:],
        "basePremium": base_prem_env,
        "currentMonth": monthly_scores[-1]["month"],
    }

    projections: List[Dict[str, Any]] = []
    if len(monthly_scores) >= 2:
        last_p = monthly_scores[-1]["premium"]
        prev_p = monthly_scores[-2]["premium"]
        trend = last_p - prev_p
        for i in range(1, 4):
            projections.append(
                {
                    "date": _project_month(monthly_scores[-1]["month"], i),
                    "projectedPremium": round(max(50, min(400, last_p + trend * i * 0.6)), 2),
                }
            )

    # Factors aggregated (placeholder zeros until stored)
    agg = {k: 0 for k in ("hardBraking", "aggressiveTurning", "followingDistance", "excessiveSpeeding", "lateNightDriving")}

    return {
        "profile": profile,
        "history": monthly_scores,
        "recentEvents": events,
        "projections": projections,
        "currentFactors": agg,
    }


def _synthetic_snapshot() -> Dict[str, Any]:  # original synthetic logic extracted
    hist_df = synthesize_dataset_improved(n_drivers=1, periods=6)
    driver_id = hist_df["driver_id"].iloc[0]
    feature_rows: List[Dict[str, Any]] = hist_df.to_dict(orient="records")
    df_feats = pd.DataFrame(feature_rows)
    model = _load_model()
    preds = predict_fn(df_feats[FEATURE_COLUMNS].copy(), model)
    df_feats["risk_score"] = preds["risk_score"]
    df_feats["model_multiplier"] = preds["premium_multiplier"]
    priced_rows = price_rows(feature_rows)
    priced_map = {(r["driver_id"], r.get("period_start")): r for r in priced_rows}
    monthly_scores = []
    for r in feature_rows:
        key = (r["driver_id"], r.get("period_start"))
        priced_row = priced_map.get(key, {})
        month = str(r.get("period_start", "2025-01-01"))[:7]
        premium = priced_row.get("pricing", {}).get("final_monthly_premium", 110)
        monthly_scores.append(
            {
                "month": month,
                "safetyScore": _safety_score(r),
                "premium": float(premium),
                "miles": float(r.get("miles", 0)),
                "riskScore": float(r.get("risk_score", 0.5)),
                "modelMultiplier": float(r.get("model_multiplier", 1.0)),
                "factors": {
                    "hardBraking": r.get("hard_braking_events_per_100mi", 0),
                    "aggressiveTurning": r.get("aggressive_turning_events_per_100mi", 0),
                    "followingDistance": r.get("tailgating_time_ratio", 0),
                    "excessiveSpeeding": r.get("speeding_minutes_per_100mi", 0),
                    "lateNightDriving": r.get("late_night_miles_per_100mi", 0),
                },
            }
        )
    now = int(time.time())
    events: List[Dict[str, Any]] = []
    last = feature_rows[-1]
    def add_events(kind: str, value: float):
        for i in range(int(min(5, max(0, round(value))))):
            sev = "high" if value > 8 else "moderate" if value > 3 else "low"
            events.append(
                {
                    "id": f"evt_{kind}_{i}",
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - i * 360)),
                    "type": kind,
                    "severity": sev,
                    "value": round(value / max(1, i + 1), 2),
                    "speedMph": round(random.uniform(25, 75), 1),
                }
            )
    add_events("hardBraking", last.get("hard_braking_events_per_100mi", 0))
    add_events("aggressiveTurning", last.get("aggressive_turning_events_per_100mi", 0))
    add_events("followingDistance", last.get("tailgating_time_ratio", 0) * 10)
    add_events("excessiveSpeeding", last.get("speeding_minutes_per_100mi", 0))
    add_events("lateNightDriving", last.get("late_night_miles_per_100mi", 0))
    base_prem_env = BASE_PREMIUM
    profile = {"id": driver_id, "name": "Dashboard Driver", "policyNumber": "POLICY-" + driver_id[-4:], "basePremium": base_prem_env, "currentMonth": monthly_scores[-1]["month"]}
    projections: List[Dict[str, Any]] = []
    if len(monthly_scores) >= 2:
        last_p = monthly_scores[-1]["premium"]; prev_p = monthly_scores[-2]["premium"]; trend = last_p - prev_p
        for i in range(1, 4):
            projections.append({"date": f"{int(profile['currentMonth'][:4])}-{int(profile['currentMonth'][5:7])+i:02d}-01", "projectedPremium": round(max(50, min(400, last_p + trend * i * 0.6)), 2)})
    agg = {k: 0 for k in ("hardBraking", "aggressiveTurning", "followingDistance", "excessiveSpeeding", "lateNightDriving")}
    return {"profile": profile, "history": monthly_scores, "recentEvents": events[:20], "projections": projections, "currentFactors": agg}


def _extract_driver_from_pk(pk: str) -> str:
    if pk.startswith("USER#"):
        return pk.split("#", 1)[-1]
    if pk.startswith("DRIVER#"):
        return pk.split("#", 1)[-1]
    return ""


def _num(val):
    if not isinstance(val, dict):
        return None
    if "N" in val:
        try:
            return float(val["N"])
        except Exception:
            return None
    return None


def _project_month(month: str, offset: int) -> str:
    try:
        y, m = month.split("-")
        y = int(y); m = int(m)
        m += offset
        y += (m - 1) // 12
        m = ((m - 1) % 12) + 1
        return f"{y}-{m:02d}-01"
    except Exception:
        return month + "-01"


def _cors_headers(extra: Dict[str, Any] | None = None) -> Dict[str, Any]:
    base = {
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }
    if extra:
        base.update(extra)
    return base


def lambda_handler(event: Dict[str, Any], context: Any):  # AWS-style signature + dashboard GET routing
    try:
        if isinstance(event, dict):
            method = event.get("httpMethod")
            path = (event.get("path") or event.get("resource") or "").lower()
            # Health check
            if method == "GET" and "health" in path:
                # health: check ddb availability & at least one period item
                status = "ok"
                details: Dict[str, Any] = {}
                code = 200
                if TELEMETRY_TABLE:
                    ddb = _get_ddb()
                    if ddb is None:
                        status = "degraded"; code = 500; details["ddb"] = "unavailable"
                    else:
                        try:
                            ddb.describe_table(TableName=TELEMETRY_TABLE)
                            scan = ddb.scan(TableName=TELEMETRY_TABLE, Limit=1)
                            if not scan.get("Items"):
                                details["data"] = "empty"
                        except Exception as ex:  # noqa: BLE001
                            status = "error"; code = 500; details["error"] = str(ex)
                else:
                    details["ddb"] = "table_env_missing"
                return {"statusCode": code, "headers": _cors_headers(), "body": json.dumps({"status": status, **details})}
            # Dashboard snapshot (DB backed)
            if method == "GET":
                try:
                    snapshot = generate_dashboard_snapshot()
                    return {"statusCode": 200, "headers": _cors_headers(), "body": json.dumps(snapshot)}
                except FileNotFoundError:
                    return {"statusCode": 404, "headers": _cors_headers(), "body": json.dumps({"message": "No data"})}
                except Exception as ex:  # noqa: BLE001
                    return {"statusCode": 500, "headers": _cors_headers(), "body": json.dumps({"message": "dashboard_error", "detail": str(ex)})}

        # Pricing (expects JSON body)
        body = event.get("body") if isinstance(event, dict) else None
        if body is None:
            return {
                "statusCode": 400,
                "headers": _cors_headers(),
                "body": json.dumps({"message": "Missing body"}),
            }
        data = json.loads(body)
        rows = [data] if isinstance(data, dict) else data
        priced = price_rows(rows)
        return {
            "statusCode": 200,
            "headers": _cors_headers(),
            "body": json.dumps({"count": len(priced), "items": priced}),
        }
    except Exception as e:  # noqa: BLE001
        return {
            "statusCode": 500,
            "headers": _cors_headers(),
            "body": json.dumps({"message": "Pricing error", "detail": str(e)}),
        }


def _cli(path: str):  # simple local helper
    rows = json.loads(Path(path).read_text())
    if isinstance(rows, dict):
        rows = [rows]
    priced = price_rows(rows)
    print(json.dumps(priced, indent=2))


if __name__ == "__main__":  # pragma: no cover
    if len(sys.argv) < 2:
        print(
            "Usage: PYTHONPATH=. python src/aws_lambda/pricing_engine/handler.py feature_rows.json"
        )
        sys.exit(2)
    _cli(sys.argv[1])
