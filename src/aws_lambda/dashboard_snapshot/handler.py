"""Dashboard Snapshot Lambda
Generates a single synthetic driver's latest month dashboard payload conforming to frontend DashboardData type.
"""

from __future__ import annotations
from pathlib import Path
import json, os, random, time
from typing import Any, Dict, List
import pandas as pd

from models.aws_sagemaker.xgboost_model import (
    ModelArtifacts,
    FEATURE_COLUMNS,
    predict_fn,
    synthesize_dataset_improved,
)
from src.aws_lambda.pricing_engine.handler import price_rows  # type: ignore

MODEL_DIR = Path(os.getenv("MODEL_ARTIFACTS_DIR", "artifacts"))
BASE_PREMIUM = float(os.getenv("BASE_PREMIUM", os.getenv("BASE_MONTHLY_PREMIUM", "190")))

_ARTIFACTS: ModelArtifacts | None = None


def _load_model() -> ModelArtifacts:
    global _ARTIFACTS
    if _ARTIFACTS is None:
        if not (MODEL_DIR / "xgb_model.json").exists():
            # Train quick if absent using small synthetic set
            df = synthesize_dataset_improved(n_drivers=200, periods=4)
            from models.aws_sagemaker.xgboost_model import train_model

            artifacts, _ = train_model(df)
            MODEL_DIR.mkdir(parents=True, exist_ok=True)
            artifacts.save(MODEL_DIR)
        _ARTIFACTS = ModelArtifacts.load(MODEL_DIR)
    return _ARTIFACTS


def _safety_score(row: Dict[str, Any]) -> int:
    # Invert some metrics into a 0-100 style score heuristic
    hb = float(row.get("hard_braking_events_per_100mi", 0))
    sp = float(row.get("speeding_minutes_per_100mi", 0))
    tg = float(row.get("tailgating_time_ratio", 0)) * 100
    ln = float(row.get("late_night_miles_per_100mi", 0))
    turns = float(row.get("aggressive_turning_events_per_100mi", 0))
    raw = 100 - (hb * 2 + turns * 1.5 + sp * 1.2 + tg * 0.6 + ln * 1.8)
    return int(max(0, min(100, raw)))


def generate_snapshot() -> Dict[str, Any]:
    artifacts = _load_model()
    # Use synthetic dataset for one driver history
    hist_df = synthesize_dataset_improved(n_drivers=1, periods=6)
    driver_id = hist_df["driver_id"].iloc[0]

    # Score & price all periods
    feature_rows: List[Dict[str, Any]] = hist_df.to_dict(orient="records")
    # Ensure required columns subset for predict
    df_feats = pd.DataFrame(feature_rows)
    preds = predict_fn(df_feats[FEATURE_COLUMNS].copy(), artifacts)
    df_feats["risk_score"] = preds["risk_score"]
    df_feats["model_multiplier"] = preds["premium_multiplier"]
    priced = price_rows(feature_rows)
    priced_map = {(r["driver_id"], r.get("period_start")): r for r in priced}

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

    # Recent events mock (sampled from last row factors)
    last = feature_rows[-1]
    now = int(time.time())
    events = []

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
    profile = {
        "id": driver_id,
        "name": "Harrison Lin",
        "policyNumber": "POLICY-" + driver_id[-4:],
        "basePremium": base_prem_env,
        "currentMonth": monthly_scores[-1]["month"],
    }

    # Simple projection: use last premium and risk deltas
    projections = []
    if len(monthly_scores) >= 2:
        last_p = monthly_scores[-1]["premium"]
        prev_p = monthly_scores[-2]["premium"]
        trend = last_p - prev_p
        for i in range(1, 4):
            projections.append(
                {
                    "date": f"{int(profile['currentMonth'][:4])}-{int(profile['currentMonth'][5:7])+i:02d}-01",
                    "projectedPremium": round(max(50, min(400, last_p + trend * i * 0.6)), 2),
                }
            )

    # Convert last ~6 monthly period factor rates (per-100mi style metrics) into approximate event counts
    # User request: show whole-number counts over past ~14 weeks. We approximate by summing the last 3 periods (~3 months ≈ 13 weeks).
    recent_periods = monthly_scores[-3:] if len(monthly_scores) >= 3 else monthly_scores
    agg = {
        "hardBraking": 0,
        "aggressiveTurning": 0,
        "followingDistance": 0,
        "excessiveSpeeding": 0,
        "lateNightDriving": 0,
    }
    for m in recent_periods:
        f = m["factors"]
        for k in agg:
            # followingDistance is ratio; scale to pseudo events (out of 100) for a rough whole number meaning
            if k == "followingDistance":
                agg[k] += int(round(f[k] * 100))
            else:
                agg[k] += int(round(f[k]))
    current_factors = agg
    return {
        "profile": profile,
        "history": monthly_scores,
        "recentEvents": events[:20],
        "projections": projections,
        "currentFactors": current_factors,
    }


def lambda_handler(event, context):  # AWS style
    try:
        snapshot = generate_snapshot()
        return {
            "statusCode": 200,
            "headers": {
                "Content-Type": "application/json",
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET,OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type",
            },
            "body": json.dumps(snapshot),
        }
    except Exception as e:  # noqa: BLE001, broad for Lambda safety
        return {
            "statusCode": 500,
            "headers": {
                "Content-Type": "application/json",
                "Access-Control-Allow-Origin": "*",
            },
            "body": json.dumps({"error": str(e)}),
        }


if __name__ == "__main__":  # local debug
    print(json.dumps(generate_snapshot(), indent=2))
