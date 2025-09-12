"""Local End-to-End Pipeline Demo
=================================

Purpose
-------
Simulate the full (simplified) data flow entirely locally without AWS:
  mock telemetry events -> validation (Pydantic) -> feature aggregation -> model scoring

This script:
 1. Generates a batch of synthetic telemetry events using `data.mock.TelemetryGenerator`.
 2. Validates them with the same logic from `src.aws_lambda.validation.handler.validate_events`.
 3. Aggregates features with the code from `src.aws_lambda.feature_extraction.handler` (re-using
    internal aggregation functions directly, bypassing Kinesis decode).
 4. Loads (or trains if missing) the XGBoost model from `models.aws_sagemaker.xgboost_model`.
 5. Produces risk score + premium multiplier predictions for the aggregated feature rows.

Run:
  python scripts/local_pipeline_demo.py --events 5000 --drivers 12 --model-artifacts artifacts/

If the model artifacts directory lacks a trained model, a quick synthetic training run is executed.

Outputs:
 - Summary stats printed to stdout
 - A sample of feature rows and predictions
 - (Optional) JSONL dumps of intermediate stages via flags

This is a diagnostic / smoke-test harness and not optimized for performance.
"""
from __future__ import annotations

import argparse
import json as _json
import os
import signal
from pathlib import Path
from typing import List, Dict, Any
import sys

# Ensure project root (one level up from scripts/) is on sys.path so 'data', 'models', 'src' are importable
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_LOG_PATH = Path("local_pipeline_demo.log")
LOG_PATH = DEFAULT_LOG_PATH

def log(msg: str) -> None:
    """Print and append to log. If stdout pipe closed, still continue file logging for rest of run."""
    global LOG_PATH
    try:
        print(msg, flush=True)
    except BrokenPipeError:
        # Do not return; continue to file so metrics persist
        pass
    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except OSError:
        pass

# --- Import project modules ---
from data.mock import TelemetryGenerator, GeneratorConfig
from src.aws_lambda.validation.handler import validate_events
from src.aws_lambda.feature_extraction.handler import _aggregate as aggregate_features  # type: ignore
from models.aws_sagemaker.xgboost_model import (
    ModelArtifacts,
    FEATURE_COLUMNS,
    predict_fn,
    train_model,
    synthesize_dataset_improved,
)
from src.aws_lambda.pricing_engine.handler import price_rows  # type: ignore
import pandas as pd


def generate_events(total: int, drivers: int, seed: int, extreme_variance: bool = False) -> List[Dict[str, Any]]:
    cfg = GeneratorConfig(drivers=drivers, seed=seed, extreme_variance=extreme_variance)
    gen = TelemetryGenerator(cfg).events()
    events: List[Dict[str, Any]] = []
    for i, evt in enumerate(gen):
        if i >= total:
            break
        events.append(evt)
    return events


def run_validation(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    # mimic API body = list
    result, valid = validate_events(events)
    if result.invalid_count:
        log(f"Validation: {result.invalid_count} invalid events (showing first 3 error sets)")
        for err in result.errors[:3]:
            log(_json.dumps(err, indent=2))
    log(f"Validation: {result.valid_count} valid / {result.invalid_count} invalid")
    return valid

def aggregate(valid_events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    feats = aggregate_features(valid_events)
    log(f"Feature aggregation: produced {len(feats)} driver-period rows")
    return feats


def ensure_model(model_dir: Path, force_retrain: bool = False) -> ModelArtifacts:
    if (model_dir / "xgb_model.json").exists() and not force_retrain:
        log(f"Loading existing model artifacts from {model_dir}")
        return ModelArtifacts.load(model_dir)
    if force_retrain:
        log("--force-retrain specified: retraining model...")
    else:
        log("Model artifacts not found. Training a quick model (synthetic data) ...")
    df = synthesize_dataset_improved(n_drivers=400, periods=4)
    artifacts, metrics = train_model(df, params=None, validation_size=0.2, early_stopping_rounds=10, num_boost_round=120)
    model_dir.mkdir(parents=True, exist_ok=True)
    artifacts.save(model_dir)
    log("Trained model metrics:" + _json.dumps(metrics, indent=2))
    return artifacts
def score(model: ModelArtifacts, feature_rows: List[Dict[str, Any]]) -> pd.DataFrame:
    if not feature_rows:
        return pd.DataFrame()
    df = pd.DataFrame(feature_rows)
    missing = [c for c in FEATURE_COLUMNS if c not in df.columns]
    for m in missing:
        df[m] = 0
    preds = predict_fn(df[FEATURE_COLUMNS].copy(), model)
    df["risk_score"] = preds["risk_score"]
    df["premium_multiplier"] = preds["premium_multiplier"]
    return df


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Local pipeline simulation")
    p.add_argument("--events", type=int, default=3000, help="Total mock events to generate")
    p.add_argument("--drivers", type=int, default=10, help="Number of synthetic drivers")
    p.add_argument("--seed", type=int, default=42, help="RNG seed")
    p.add_argument("--model-artifacts", type=str, default="artifacts", help="Directory for model artifacts")
    p.add_argument("--dump-events", type=str, help="Optional path to write validated events JSONL")
    p.add_argument("--dump-features", type=str, help="Optional path to write feature rows JSONL")
    p.add_argument("--sample", type=int, default=5, help="Sample size to print from predictions")
    p.add_argument("--inject-extremes", action="store_true", help="Add synthetic very-low and very-high risk feature rows for variance exploration")
    p.add_argument("--extreme-pairs", type=int, default=1, help="How many low/high extreme row pairs to inject (requires --inject-extremes)")
    p.add_argument("--extreme-variance", action="store_true", help="Enable generator risk profiles for wider raw event variance (data/mock.py)")
    p.add_argument("--force-retrain", action="store_true", help="Ignore existing model artifacts and retrain (useful after model sensitivity changes)")
    p.add_argument("--debug-driver-sample", type=int, default=5, help="Number of original (non-injected) driver rows to print with detailed feature + pricing breakdown")
    p.add_argument("--premium-target-spread", type=float, help="Override PREMIUM_SCALING_TARGET_SPREAD env for dynamic premium scaling (e.g. 0.45)")
    p.add_argument("--log-file", type=str, help="Custom log file path (default local_pipeline_demo.log)")
    return p.parse_args()


def maybe_dump(path: str | None, rows: List[Dict[str, Any]]) -> None:
    if not path:
        return
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(_json.dumps(r) + "\n")
    print(f"Wrote {len(rows)} rows to {path}")


def main() -> None:
    args = parse_args()
    # Configure log file override
    global LOG_PATH
    if args.log_file:
        LOG_PATH = Path(args.log_file)
    # Ignore SIGPIPE so native libs won't kill process; BrokenPipe handled in log()
    try:
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (AttributeError, ValueError):
        pass
    model_dir = Path(args.model_artifacts)
    log("[local_pipeline_demo] Starting pipeline")

    # 1. Generate
    raw_events = generate_events(args.events, args.drivers, args.seed, args.extreme_variance)
    log(f"Generated {len(raw_events)} raw events")

    # 2. Validate
    valid_events = run_validation(raw_events)
    maybe_dump(args.dump_events, valid_events)

    # 3. Aggregate features
    feature_rows = aggregate(valid_events)
    maybe_dump(args.dump_features, feature_rows)

    if not feature_rows:
        log("No feature rows (possibly low exposure); exiting.")
        return

    # 4. Model ensure
    model = ensure_model(model_dir, force_retrain=args.force_retrain)
    # If user wants to override target spread for dynamic premium scaling, set env before scoring
    if args.premium_target_spread:
        os.environ["PREMIUM_SCALING_TARGET_SPREAD"] = str(args.premium_target_spread)

    # Optional: Inject extreme low/high risk rows AFTER model is ready so we can adaptively scale
    if args.inject_extremes:
        ref_period = feature_rows[0].get("period_key", "2025-01")
        period_start = feature_rows[0].get("period_start", "2025-01-01")
        period_end = feature_rows[0].get("period_end", "2025-01-31")

        def predict_risk(rows: List[Dict[str, Any]]):
            df_tmp = pd.DataFrame(rows)
            for col in FEATURE_COLUMNS:
                if col not in df_tmp.columns:
                    df_tmp[col] = 0.0
            preds = predict_fn(df_tmp[FEATURE_COLUMNS].copy(), model)
            return preds["risk_score"]

        injected: List[Dict[str, Any]] = []
        target_delta = 0.2
        scale_schedule = [1, 2, 3, 4, 6, 8, 10, 12, 15]

        for i in range(args.extreme_pairs):
            low_template = {
                "driver_id": f"DEXTR_LOW_{i}",
                "period_key": ref_period,
                "period_start": period_start,
                "period_end": period_end,
                "feature_version": 1,
                "hard_braking_events_per_100mi": 0.05,
                "aggressive_turning_events_per_100mi": 0.05,
                "tailgating_time_ratio": 0.0,
                "speeding_minutes_per_100mi": 0.1,
                "late_night_miles_per_100mi": 0.0,
                "miles": 980.0,
                "prior_claim_count": 0,
            }
            best_low = low_template
            best_high = None
            achieved = False
            for factor in scale_schedule:
                high_candidate = {
                    "driver_id": f"DEXTR_HIGH_{i}_x{factor}",
                    "period_key": ref_period,
                    "period_start": period_start,
                    "period_end": period_end,
                    "feature_version": 1,
                    "hard_braking_events_per_100mi": 2.5 * factor + 2,  # escalate
                    "aggressive_turning_events_per_100mi": 2.0 * factor + 2,
                    "tailgating_time_ratio": min(0.05 * factor + 0.05, 0.95),
                    "speeding_minutes_per_100mi": 1.2 * factor + 1,
                    "late_night_miles_per_100mi": 0.8 * factor + 0.5,
                    "miles": 900.0 - min(200, 5 * factor),  # slight reduction
                    "prior_claim_count": min(1 + factor // 2, 12),
                }
                risks = predict_risk([best_low, high_candidate])
                delta = risks[1] - risks[0]
                if delta >= target_delta:
                    best_high = high_candidate
                    achieved = True
                    log(f"Extreme pair {i}: achieved risk delta {delta:.3f} with factor {factor}")
                    break
                best_high = high_candidate  # keep last even if not yet achieved
            if not achieved:
                # log final delta attempt
                risks = predict_risk([best_low, best_high])  # type: ignore[arg-type]
                log(f"Extreme pair {i}: max factor used; risk delta {risks[1]-risks[0]:.3f}")
            injected.extend([best_low, best_high])  # type: ignore[arg-type]
        feature_rows.extend(injected)
        log(f"Injected {len(injected)} adaptive extreme rows. New total: {len(feature_rows)}")

    # 5. Score
    scored = score(model, feature_rows)
    if scored.empty:
        log("No data to score.")
        return

    log("\nPrediction sample (model-only):")
    log(scored.head(args.sample).to_string(index=False))

    log("\nSummary stats (risk_score):")
    log(str(scored["risk_score"].describe()))

    # Explicit spread metric for quick variance inspection
    min_r, max_r = float(scored["risk_score"].min()), float(scored["risk_score"].max())
    log(f"\nRisk score spread (max - min): {max_r - min_r:.3f} (min={min_r:.3f}, max={max_r:.3f})")

    log("\nSummary stats (premium_multiplier):")
    log(str(scored["premium_multiplier"].describe()))
    # Log model distribution stats and computed scaling factor if available
    try:
        meta_path = os.path.join(model_dir, 'meta.json')
        with open(meta_path, 'r', encoding='utf-8') as f:
            meta = _json.load(f)
        dist = meta.get('dist_stats', {}) or {}
        p5 = dist.get("pred_p5"); p95 = dist.get("pred_p95")
        tgt = os.environ.get("PREMIUM_SCALING_TARGET_SPREAD", "default")
        if p5 is not None and p95 is not None:
            spread = p95 - p5
            scaling_factor = (float(tgt) if tgt != "default" else 0.35) / spread if spread and spread > 1e-6 else float('nan')
            log(f"Distribution stats: p5={p5:.4f}, p95={p95:.4f}, spread={spread:.4f}, target={tgt}, derived_scaling_factor≈{scaling_factor:.4f}")
    except (FileNotFoundError, _json.JSONDecodeError):
        pass

    # 6. Pricing Engine Integration
    # Ensure pricing engine points at same artifacts directory
    os.environ.setdefault("MODEL_ARTIFACTS_DIR", str(model_dir))
    priced_rows = price_rows(feature_rows)
    if not priced_rows:
        log("Pricing engine produced no rows.")
        return
    priced_df = pd.json_normalize(priced_rows, sep='.')

    # Merge risk/premium columns from earlier scoring if desired (priced_rows already recomputed model outputs)
    log("\nPricing sample (final monthly premiums):")
    display_cols = [
        c for c in [
            "driver_id",
            "period_key",
            "risk_score",
            "model_premium_multiplier",
            "pricing.final_multiplier",
            "pricing.final_monthly_premium",
        ] if c in priced_df.columns
    ]
    log(priced_df[display_cols].head(args.sample).to_string(index=False))

    if "pricing.final_monthly_premium" in priced_df.columns:
        log("\nSummary stats (final_monthly_premium):")
        log(str(priced_df["pricing.final_monthly_premium"].describe()))
    pmin, pmax = float(priced_df["pricing.final_monthly_premium"].min()), float(priced_df["pricing.final_monthly_premium"].max())
    log(f"\nFinal monthly premium spread: {pmax - pmin:.2f} (min={pmin:.2f}, max={pmax:.2f})")
    if "pricing.final_multiplier" in priced_df.columns:
        log("\nSummary stats (final_multiplier):")
        log(str(priced_df["pricing.final_multiplier"].describe()))

    # Detailed debug for first N original (non-injected) drivers
    if args.debug_driver_sample > 0:
        base_mask = ~priced_df['driver_id'].str.startswith('DEXTR_') if 'driver_id' in priced_df.columns else [True] * len(priced_df)
        base_sample = priced_df[base_mask].head(args.debug_driver_sample)
        if not base_sample.empty:
            cols_show = [c for c in [
                'driver_id','period_key','miles','hard_braking_events_per_100mi','aggressive_turning_events_per_100mi',
                'tailgating_time_ratio','speeding_minutes_per_100mi','late_night_miles_per_100mi','prior_claim_count',
                'car_value','car_sportiness','risk_score','model_premium_multiplier','pricing.final_multiplier','pricing.final_monthly_premium'
            ] if c in base_sample.columns]
            log("\nDebug driver sample (original drivers):")
            log(base_sample[cols_show].to_string(index=False))


if __name__ == "__main__":  # pragma: no cover
    main()
