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
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pandas as pd

try:
    from models.aws_sagemaker.xgboost_model import (  # type: ignore
        ModelArtifacts,
        FEATURE_COLUMNS,
        predict_fn,
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
BASE_MONTHLY_PREMIUM = float(os.getenv("BASE_MONTHLY_PREMIUM", "110"))
MIN_PREMIUM = float(os.getenv("MIN_PREMIUM", "50"))
MAX_PREMIUM = float(os.getenv("MAX_PREMIUM", "400"))
MIN_FACTOR = float(os.getenv("MIN_FACTOR", "0.7"))
MAX_FACTOR = float(os.getenv("MAX_FACTOR", "1.5"))

_ARTIFACTS: Any = None  # use Any for runtime flexibility when model module absent


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
            base_premium=BASE_MONTHLY_PREMIUM,
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
                    "base_premium": BASE_MONTHLY_PREMIUM,
                    "min_premium": MIN_PREMIUM,
                    "max_premium": MAX_PREMIUM,
                },
            }
        )
        output.append(r)
    return output


def lambda_handler(event: Dict[str, Any], context: Any):  # AWS-style signature
    try:
        body = event.get("body") if isinstance(event, dict) else None
        if body is None:
            raise ValueError("Missing body")
        data = json.loads(body)
        if isinstance(data, dict):
            rows = [data]
        else:
            rows = data
        priced = price_rows(rows)
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"count": len(priced), "items": priced}),
        }
    except Exception as e:  # noqa: BLE001
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json"},
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
