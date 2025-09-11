"""XGBoost Model Scaffold for Telematics-based Usage-Based Insurance (SageMaker Ready)
=====================================================================================

Purpose
-------
This module provides a minimal yet extensible scaffold for training and serving an
XGBoost model that estimates a driver *risk score* or *next-period premium uplift* from
aggregated telematics features (hard braking, aggressive turning, following distance,
excessive speeding, late-night driving, mileage, etc.). It aligns with the project
objective: dynamic pricing that reflects real driving behavior.

Design Goals
------------
1. SageMaker Script Mode Compatibility (train + inference entrypoints).
2. Local developer ergonomics (run `python models/xgboost_sagemaker.py --local-train`).
3. Clear feature engineering hooks (aggregate raw event-level telematics into monthly features).
4. Simple, reproducible training (deterministic seed + early stopping).
5. Hyperparameter tuning support (expose search space helper for SageMaker HyperparameterTuner).
6. Inference logic that returns both raw prediction and derived premium adjustment.

Data Expectations
-----------------
For training, we expect a tabular CSV with (at minimum) the following columns
representing *per-driver-per-period* aggregates (monthly or rolling window):

  driver_id (str)
  period_start (date or str)
  period_end (date or str)
  hard_braking_events_per_100mi (float)
  aggressive_turning_events_per_100mi (float)
  tailgating_time_ratio (float)          # proportion 0-1
  speeding_minutes_per_100mi (float)
  late_night_miles_per_100mi (float)
  miles (float)
  prior_claim_count (int)                # optional
  target_risk (float)                    # label: e.g., claim frequency proxy or future premium factor

If no dataset is supplied, the script will synthesize one for demonstration.

SageMaker Channels / Environment Variables
------------------------------------------
The script follows the typical conventions automatically set by SageMaker:
  SM_MODEL_DIR         -> where to persist the trained model artifact
  SM_OUTPUT_DATA_DIR   -> optional eval metrics output
  SM_CHANNEL_TRAIN     -> path containing training data (CSV)
  SM_CHANNEL_VALIDATION-> path for validation data (optional)

Inference Contract
------------------
Input: JSON lines or CSV rows with the feature columns (sans target). Missing numeric
features will be imputed (median) based on training statistics saved in the model file.
Output: JSON with fields: {"risk_score": float, "premium_multiplier": float}

Premium Multiplier (prototype):
  multiplier = 1 + (risk_score - baseline) * scaling_factor
Where baseline defaults to training set mean risk_score (or 0.5 if not stored).

Example Local Usage
-------------------
Train with synthetic data and save model under ./artifacts :
  python models/xgboost_sagemaker.py --local-train --model-dir artifacts/

Run a quick inference:
  python models/xgboost_sagemaker.py --predict-sample

SageMaker Estimator (outside this script):
  from sagemaker.xgboost import XGBoost as SageMakerXGB
  estimator = SageMakerXGB(
          entry_point='models/xgboost_sagemaker.py',
          role=ROLE_ARN,
          instance_type='ml.m5.large',
          framework_version='1.7-1',  # adjust per AWS region offering
          hyperparameters={'max_depth':5, 'eta':0.1, 'objective':'reg:squarederror'}
  )
  estimator.fit({'train': train_s3_uri, 'validation': val_s3_uri})

NOTE: Alternatively, you can use the generic ScriptProcessor / Estimator with this file.

Extending:
 - Replace synthetic generation with real feature pipeline output (e.g., AWS Glue + Feature Store).
 - Add SHAP value computation for transparency (store in model metrics).
 - Implement model versioning + lineage (e.g., MLflow or SageMaker Model Registry).
 - Calibrate scores if treated as probability (Platt scaling / isotonic).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from sklearn.metrics import mean_squared_error
from sklearn.model_selection import train_test_split
from sklearn.impute import SimpleImputer
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import Pipeline
import xgboost as xgb


# Keep for reproducible metrics
SEED = 42
RNG = np.random.default_rng(SEED)

FEATURE_COLUMNS = [
    "hard_braking_events_per_100mi",
    "aggressive_turning_events_per_100mi",
    "tailgating_time_ratio",
    "speeding_minutes_per_100mi",
    "late_night_miles_per_100mi",
    "miles",
    "prior_claim_count",
]
TARGET_COLUMN = "target_risk"


@dataclass
class ModelArtifacts:
    booster: xgb.Booster
    feature_pipeline: Pipeline  # imputers / scalers
    baseline_risk: float

    def save(self, model_dir: Path) -> None:
        model_dir.mkdir(parents=True, exist_ok=True)
        # Save booster
        booster_path = model_dir / "xgb_model.json"
        self.booster.save_model(str(booster_path))
        # Save preprocessing pipeline (simple joblib via numpy serialization fallback)
        import joblib

        joblib.dump(self.feature_pipeline, model_dir / "feature_pipeline.joblib")
        meta = {"baseline_risk": float(self.baseline_risk)}
        (model_dir / "meta.json").write_text(json.dumps(meta))

    @staticmethod
    def load(model_dir: Path) -> "ModelArtifacts":
        import joblib

        booster = xgb.Booster()
        booster.load_model(str(model_dir / "xgb_model.json"))
        pipeline: Pipeline = joblib.load(model_dir / "feature_pipeline.joblib")
        meta = json.loads((model_dir / "meta.json").read_text())
        return ModelArtifacts(
            booster=booster, feature_pipeline=pipeline, baseline_risk=meta.get("baseline_risk", 0.5)
        )


def synthesize_dataset_improved(n_drivers: int = 800, periods: int = 6) -> pd.DataFrame:
    """Enhanced synthetic data with more realistic risk patterns."""

    rows: List[Dict[str, Any]] = []

    # Define driver archetypes with different risk profiles
    driver_types = {
        "safe": {"base_risk": 0.1, "weight": 0.7},
        "moderate": {"base_risk": 0.3, "weight": 0.2},
        "risky": {"base_risk": 0.7, "weight": 0.1},
    }

    for d in range(n_drivers):
        driver_id = f"D{d:05d}"

        # Assign driver type
        driver_type = RNG.choice(
            list(driver_types.keys()), p=[0.7, 0.2, 0.1]
        )  # Most drivers are safe
        base_risk = driver_types[driver_type]["base_risk"]

        prior_claims = RNG.poisson(base_risk * 2)  # Claims correlated with risk

        for p in range(periods):
            # Generate features based on driver type
            if driver_type == "safe":
                hard_braking = RNG.gamma(1, 0.8)  # Lower values
                aggressive_turns = RNG.gamma(1, 0.6)
                tailgating_ratio = np.clip(RNG.normal(0.05, 0.02), 0, 0.3)
                speeding_minutes = abs(RNG.normal(2, 1))
                late_night_miles = abs(RNG.normal(1, 0.5))
            elif driver_type == "moderate":
                hard_braking = RNG.gamma(2, 1.2)
                aggressive_turns = RNG.gamma(1.5, 1.0)
                tailgating_ratio = np.clip(RNG.normal(0.12, 0.04), 0, 0.4)
                speeding_minutes = abs(RNG.normal(5, 2))
                late_night_miles = abs(RNG.normal(3, 1.5))
            else:  # risky
                hard_braking = RNG.gamma(3, 2.0)  # Higher values
                aggressive_turns = RNG.gamma(2.5, 1.8)
                tailgating_ratio = np.clip(RNG.normal(0.25, 0.08), 0, 0.6)
                speeding_minutes = abs(RNG.normal(12, 4))
                late_night_miles = abs(RNG.normal(8, 3))

            miles = RNG.normal(850, 220)

            # More sophisticated risk calculation
            # Non-linear interactions between features
            tailgating_effect = tailgating_ratio * 2.0  # Strong effect
            speed_braking_interaction = (speeding_minutes / 10) * (hard_braking / 5) * 0.5

            linear_risk = (
                tailgating_effect
                + 0.08 * hard_braking
                + 0.06 * aggressive_turns
                + 0.04 * speeding_minutes
                + 0.03 * late_night_miles
                + 0.02 * prior_claims
                + speed_braking_interaction  # Interaction term
            )

            # Add temporal effects (seasonal patterns)
            seasonal_factor = 1 + 0.1 * np.sin(2 * np.pi * p / 12)  # Winter risk increase

            noise = RNG.normal(0, 0.08)
            risk = base_risk + (1 / (1 + np.exp(-(linear_risk + noise - 1.0)))) * seasonal_factor
            risk = np.clip(risk, 0.01, 0.99)  # Keep in reasonable bounds

            rows.append(
                {
                    "driver_id": driver_id,
                    "period_start": f"2024-{p+1:02d}-01",
                    "period_end": f"2024-{p+1:02d}-28",
                    "hard_braking_events_per_100mi": hard_braking,
                    "aggressive_turning_events_per_100mi": aggressive_turns,
                    "tailgating_time_ratio": tailgating_ratio,
                    "speeding_minutes_per_100mi": speeding_minutes,
                    "late_night_miles_per_100mi": late_night_miles,
                    "miles": miles,
                    "prior_claim_count": prior_claims,
                    "driver_type": driver_type,  # Keep for validation
                    TARGET_COLUMN: risk,
                }
            )

    return pd.DataFrame(rows)


def load_dataset(train_channel: Optional[str]) -> pd.DataFrame:
    if train_channel and Path(train_channel).exists():
        # Expect exactly one CSV or combine multiple
        csvs = list(Path(train_channel).glob("*.csv"))
        if not csvs:
            print(f"No CSV files found in {train_channel}, generating synthetic data instead.")
            return synthesize_dataset_improved()
        frames = [pd.read_csv(p) for p in csvs]
        df = pd.concat(frames, ignore_index=True)
        print(f"Loaded {len(df)} rows from {len(csvs)} file(s).")
        return df
    print("Training channel not provided or missing; generating synthetic data.")
    return synthesize_dataset_improved()


def build_feature_pipeline() -> Pipeline:
    return Pipeline(
        steps=[
            ("imputer", SimpleImputer(strategy="median")),
            ("scaler", StandardScaler()),
        ]
    )


def train_model(
    df: pd.DataFrame,
    params: Optional[Dict[str, Any]] = None,
    validation_size: float = 0.2,
    early_stopping_rounds: int = 25,
    num_boost_round: int = 400,
) -> Tuple[ModelArtifacts, Dict[str, Any]]:
    if params is None:
        params = {
            "objective": "reg:squarederror",
            "max_depth": 5,
            "eta": 0.08,
            "subsample": 0.85,
            "colsample_bytree": 0.9,
            "lambda": 1.2,
            "alpha": 0.2,
            "seed": SEED,
            "eval_metric": "rmse",
        }

    df = df.copy()
    # Basic sanity: drop rows missing target
    df = df.dropna(subset=[TARGET_COLUMN])
    X = df[FEATURE_COLUMNS]
    y = df[TARGET_COLUMN]

    X_train, X_val, y_train, y_val = train_test_split(
        X, y, test_size=validation_size, random_state=SEED
    )

    pipeline = build_feature_pipeline()
    X_train_proc = pipeline.fit_transform(X_train)
    X_val_proc = pipeline.transform(X_val)

    dtrain = xgb.DMatrix(X_train_proc, label=y_train.values, feature_names=FEATURE_COLUMNS)
    dval = xgb.DMatrix(X_val_proc, label=y_val.values, feature_names=FEATURE_COLUMNS)
    evals = [(dtrain, "train"), (dval, "validation")]

    booster = xgb.train(
        params,
        dtrain,
        num_boost_round=num_boost_round,
        evals=evals,
        early_stopping_rounds=early_stopping_rounds,
        verbose_eval=25,
    )

    y_val_pred = booster.predict(dval)
    # Compute RMSE manually to remain compatible with older scikit-learn versions
    mse = mean_squared_error(y_val, y_val_pred)
    rmse = float(mse**0.5)
    metrics = {"validation_rmse": rmse, "best_iteration": booster.best_iteration}
    baseline_risk = float(y_train.mean())
    artifacts = ModelArtifacts(
        booster=booster, feature_pipeline=pipeline, baseline_risk=baseline_risk
    )
    return artifacts, metrics


def get_hyperparameter_search_space() -> Dict[str, Any]:
    """Return a search space mapping for SageMaker HyperparameterTuner.

    Example usage:
            from sagemaker.tuner import HyperparameterTuner, ContinuousParameter, IntegerParameter
            space = get_hyperparameter_search_space()
            tuner = HyperparameterTuner(
                    estimator, objective_metric_name='validation:rmse',
                    hyperparameter_ranges={
                            'eta': ContinuousParameter(*space['eta']),
                            'max_depth': IntegerParameter(*space['max_depth']),
                            'subsample': ContinuousParameter(*space['subsample'])
                    },
                    max_jobs=20, max_parallel_jobs=3
            )
    """
    return {
        "eta": (0.01, 0.3),
        "max_depth": (3, 9),
        "subsample": (0.5, 1.0),
        "colsample_bytree": (0.5, 1.0),
        "lambda": (0.5, 5.0),
        "alpha": (0.0, 2.0),
    }


# ------------- Inference Helpers (SageMaker Serving) -----------------


def model_fn(model_dir: str) -> ModelArtifacts:  # SageMaker naming convention
    return ModelArtifacts.load(Path(model_dir))


def input_fn(serialized_input_data: str, content_type: str) -> pd.DataFrame:
    if content_type == "application/json":
        data = json.loads(serialized_input_data)
        if isinstance(data, dict):
            data = [data]
        return pd.DataFrame(data)
    if content_type == "text/csv":
        from io import StringIO

        return pd.read_csv(StringIO(serialized_input_data))
    raise ValueError(f"Unsupported content type: {content_type}")


def predict_fn(input_data: pd.DataFrame, model: ModelArtifacts):  # type: ignore
    missing = [c for c in FEATURE_COLUMNS if c not in input_data.columns]
    for c in missing:
        input_data[c] = np.nan

    X_data = input_data[FEATURE_COLUMNS]
    X_proc = model.feature_pipeline.transform(X_data)
    dmat = xgb.DMatrix(X_proc, feature_names=FEATURE_COLUMNS)
    preds = model.booster.predict(dmat)
    # premium multiplier (toy scaling): amplify variance
    scaling_factor = 0.25
    premium_multiplier = 1 + (preds - model.baseline_risk) * scaling_factor
    return {
        "risk_score": preds.tolist(),
        "premium_multiplier": premium_multiplier.tolist(),
    }


def output_fn(prediction: Dict[str, Any], accept: str) -> str:
    if accept == "application/json":
        return json.dumps(prediction)
    raise ValueError(f"Unsupported accept type: {accept}")


# ------------- CLI / Script Entrypoint --------------------------------


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train or serve XGBoost UBI model.")
    parser.add_argument("--model-dir", type=str, default=os.environ.get("SM_MODEL_DIR", "model"))
    parser.add_argument("--train-channel", type=str, default=os.environ.get("SM_CHANNEL_TRAIN"))
    parser.add_argument(
        "--validation-channel", type=str, default=os.environ.get("SM_CHANNEL_VALIDATION")
    )
    parser.add_argument("--local-train", action="store_true", help="Run a local training job")
    parser.add_argument(
        "--predict-sample", action="store_true", help="Run a sample inference after training"
    )
    parser.add_argument("--learning-rate", type=float, default=0.08)
    parser.add_argument("--max-depth", type=int, default=5)
    parser.add_argument("--subsample", type=float, default=0.85)
    parser.add_argument("--colsample-bytree", type=float, default=0.9)
    parser.add_argument("--num-round", type=int, default=400)
    parser.add_argument("--early-stopping", type=int, default=25)
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)

    if not (args.local_train or os.environ.get("SM_CHANNEL_TRAIN")):
        print("No training requested (use --local-train) and not in SageMaker training context.")
        return

    df = load_dataset(args.train_channel)

    params = {
        "objective": "reg:squarederror",
        "eta": args.learning_rate,
        "max_depth": args.max_depth,
        "subsample": args.subsample,
        "colsample_bytree": args.colsample_bytree,
        "lambda": 1.2,
        "alpha": 0.2,
        "seed": SEED,
        "eval_metric": "rmse",
    }
    artifacts, metrics = train_model(
        df,
        params=params,
        validation_size=0.2,
        num_boost_round=args.num_round,
        early_stopping_rounds=args.early_stopping,
    )
    model_dir = Path(args.model_dir)
    artifacts.save(model_dir)
    print(f"Model saved to {model_dir.resolve()}")
    print("Metrics:", json.dumps(metrics, indent=2))

    # Persist metrics for SageMaker (if output dir exists)
    sm_output = os.environ.get("SM_OUTPUT_DATA_DIR")
    if sm_output:
        (Path(sm_output) / "metrics.json").write_text(json.dumps(metrics))

    if args.predict_sample:
        sample = df.sample(3, random_state=SEED)[FEATURE_COLUMNS]
        loaded = ModelArtifacts.load(model_dir)
        result = predict_fn(sample, loaded)
        print("Sample inference:", json.dumps(result, indent=2))


if __name__ == "__main__":  # pragma: no cover
    main(sys.argv[1:])
