"""Unified UBI Report
======================
Generates a single consolidated report covering:
 1. Synthetic training label distribution
 2. Model prediction & premium scaling distribution
 3. Feature importance proxy (correlations) vs label & predictions
 4. Pricing engine outputs (if pipeline run executed)
 5. Prior claims & car value band impacts

Outputs:
 - artifacts/ubi_report.txt (human readable)
 - artifacts/ubi_report_summary.json (machine parsable key metrics)

Optional flags let you retrain, adjust synthetic sizes, and skip pricing.
"""

from __future__ import annotations
from pathlib import Path
import argparse, json, sys, os
import numpy as np
import pandas as pd

# Ensure project root for imports
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from models.aws_sagemaker.xgboost_model import (  # type: ignore
    synthesize_dataset_improved,
    ModelArtifacts,
    FEATURE_COLUMNS,
    predict_fn,
    train_model,
    TARGET_COLUMN,
)
from src.aws_lambda.pricing_engine.handler import price_rows  # type: ignore


def parse_args():
    p = argparse.ArgumentParser(description="Unified UBI reporting")
    p.add_argument("--model-dir", type=str, default="artifacts")
    p.add_argument("--drivers", type=int, default=500)
    p.add_argument("--periods", type=int, default=6)
    p.add_argument("--force-retrain", action="store_true")
    p.add_argument("--skip-pricing", action="store_true")
    return p.parse_args()


def ensure_model(model_dir: Path, force: bool) -> ModelArtifacts:
    if (model_dir / "xgb_model.json").exists() and not force:
        return ModelArtifacts.load(model_dir)
    df = synthesize_dataset_improved(n_drivers=400, periods=4)
    artifacts, _ = train_model(df)
    model_dir.mkdir(parents=True, exist_ok=True)
    artifacts.save(model_dir)
    return artifacts


def build_report(df: pd.DataFrame, artifacts: ModelArtifacts, include_pricing: bool):
    pred_df = df[FEATURE_COLUMNS].copy()
    result = predict_fn(pred_df, artifacts)
    preds = np.array(result["risk_score"])
    mults = np.array(result["premium_multiplier"])

    # Correlations
    corr_df = df[FEATURE_COLUMNS + [TARGET_COLUMN]].copy()
    corr_df["model_pred"] = preds
    corr = corr_df.corr(numeric_only=True)
    target_corr = corr[TARGET_COLUMN].drop(labels=[TARGET_COLUMN]).sort_values(ascending=False)
    pred_corr = (
        corr["model_pred"].drop(labels=["model_pred", TARGET_COLUMN]).sort_values(ascending=False)
    )

    # Buckets
    df_b = df.copy()
    df_b["model_pred"] = preds
    df_b["premium_multiplier"] = mults
    df_b["car_value_band"] = pd.cut(
        df_b["car_value_raw"],
        bins=[0, 20000, 35000, 50000, 70000, 1e9],
        labels=["<20k", "20-35k", "35-50k", "50-70k", "70k+"],
    )

    lines = []
    add = lines.append

    add("=== SECTION 1: Synthetic Target Risk Distribution ===")
    add(df[TARGET_COLUMN].describe().to_string())
    add(f"Mean target_risk: {df[TARGET_COLUMN].mean():.4f}")
    add("")

    add("=== SECTION 2: Model Prediction & Premium Distribution ===")
    add(pd.Series(preds, name="risk_score").describe().to_string())
    add(f"Baseline stored: {artifacts.baseline_risk:.4f}")
    if artifacts.dist_stats:
        add("Stored dist stats: " + json.dumps(artifacts.dist_stats))
    add(pd.Series(mults, name="premium_multiplier").describe().to_string())
    add("")

    add("=== SECTION 3: Correlations (Top 10) ===")
    add("With target:")
    add(target_corr.head(10).to_string())
    add("With model prediction:")
    add(pred_corr.head(10).to_string())
    add("")

    add("=== SECTION 4: Claim & Car Value Impact (Mean model_pred) ===")
    add("By prior_claim_count:")
    add(df_b.groupby("prior_claim_count")["model_pred"].mean().to_string())
    add("By car value band:")
    add(df_b.groupby("car_value_band")["model_pred"].mean().to_string())
    add("")

    priced_df = None
    pricing_summary = {}
    if include_pricing:
        os.environ.setdefault("MODEL_ARTIFACTS_DIR", str(Path.cwd() / "artifacts"))
        priced_rows = price_rows(df_b.to_dict(orient="records"))
        if priced_rows:
            priced_df = pd.json_normalize(priced_rows, sep=".")
            add("=== SECTION 5: Pricing Outputs ===")
            if "pricing.final_monthly_premium" in priced_df.columns:
                add("Final Monthly Premium Distribution:")
                add(priced_df["pricing.final_monthly_premium"].describe().to_string())
            if "pricing.final_multiplier" in priced_df.columns:
                add("\nFinal Multiplier Distribution:")
                add(priced_df["pricing.final_multiplier"].describe().to_string())
            # Sample rows (top 5 by risk)
            if "risk_score" in priced_df.columns:
                top5 = priced_df.sort_values("risk_score", ascending=False).head(5)
                cols = [
                    c
                    for c in [
                        "driver_id",
                        "risk_score",
                        "model_premium_multiplier",
                        "pricing.final_multiplier",
                        "pricing.final_monthly_premium",
                        "prior_claim_count",
                        "car_value_raw",
                        "speeding_minutes_per_100mi",
                    ]
                    if c in priced_df.columns
                ]
                add("\nTop 5 High-Risk Pricing Rows:")
                add(top5[cols].to_string(index=False))
            # Adjustment contribution summary
            if "behavior_adjustments" in priced_df.columns:
                # behavior_adjustments is nested; skip raw expansion for brevity
                pass
            # Premium by claims & car value band
            if (
                "prior_claim_count" in priced_df.columns
                and "pricing.final_monthly_premium" in priced_df.columns
            ):
                add("\nMean Final Premium by Prior Claim Count:")
                add(
                    priced_df.groupby("prior_claim_count")["pricing.final_monthly_premium"]
                    .mean()
                    .to_string()
                )
            if (
                "car_value_raw" in priced_df.columns
                and "pricing.final_monthly_premium" in priced_df.columns
            ):
                priced_df["car_value_band"] = pd.cut(
                    priced_df["car_value_raw"],
                    bins=[0, 20000, 35000, 50000, 70000, 1e9],
                    labels=["<20k", "20-35k", "35-50k", "50-70k", "70k+"],
                )
                add("\nMean Final Premium by Car Value Band:")
                add(
                    priced_df.groupby("car_value_band")["pricing.final_monthly_premium"]
                    .mean()
                    .to_string()
                )
            add("")
            pricing_summary = {
                "mean_final_premium": (
                    float(priced_df["pricing.final_monthly_premium"].mean())
                    if "pricing.final_monthly_premium" in priced_df
                    else None
                ),
                "mean_final_multiplier": (
                    float(priced_df["pricing.final_multiplier"].mean())
                    if "pricing.final_multiplier" in priced_df
                    else None
                ),
                "p99_final_premium": (
                    float(priced_df["pricing.final_monthly_premium"].quantile(0.99))
                    if "pricing.final_monthly_premium" in priced_df
                    else None
                ),
            }

    text = "\n".join(lines)
    base_summary = {
        "mean_target_risk": float(df[TARGET_COLUMN].mean()),
        "mean_prediction": float(preds.mean()),
        "mean_premium_multiplier": float(mults.mean()),
        "rows": int(len(df)),
        "top_target_corr": target_corr.head(5).to_dict(),
        "top_pred_corr": pred_corr.head(5).to_dict(),
    }
    base_summary.update(pricing_summary)
    return text, base_summary


def main():
    args = parse_args()
    model_dir = Path(args.model_dir)
    artifacts = ensure_model(model_dir, args.force_retrain)
    df = synthesize_dataset_improved(n_drivers=args.drivers, periods=args.periods)
    report_text, summary = build_report(df, artifacts, not args.skip_pricing)
    out_txt = model_dir / "ubi_report.txt"
    out_json = model_dir / "ubi_report_summary.json"
    out_txt.write_text(report_text)
    out_json.write_text(json.dumps(summary, indent=2))
    print(
        json.dumps(
            {"ubi_report": {"text_file": str(out_txt), "json_file": str(out_json), **summary}},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
