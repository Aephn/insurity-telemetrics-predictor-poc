# Local Pipeline Demo

This document explains the end-to-end local simulation script located at `scripts/local_pipeline_demo.py`.
It stitches together the existing components without AWS infrastructure:

1. Synthetic event generation (`data/mock.py`)
2. Pydantic validation (`src/aws_lambda/validation/handler.validate_events`)
3. Feature aggregation (`src/aws_lambda/feature_extraction/handler._aggregate`)
4. Model loading / (on-demand) training + scoring (`models/aws_sagemaker/xgboost_model.py`)
5. Premium mapping (dynamic scaling) & pricing engine adjustments

## Why This Exists
Acts as an integration smoke test and a reproducible way to sanity‑check feature / model drift during development without provisioning streams or Lambdas.

## Prerequisites
- Python environment with project dependencies installed (see `pyproject.toml`).
- xgboost, pandas, scikit-learn, pydantic available (installed via standard dependency install).

## Run It
From the project root (ensure the root is on `PYTHONPATH` so packages resolve):

If running evals including stronger variance in test data:

```bash
 PYTHONPATH=. python scripts/local_pipeline_demo.py \
 --events 1200 \
 --drivers 12 \
 --model-artifacts artifacts \
 --force-retrain \
 --extreme-variance \
 --inject-extremes \
 --extreme-pairs 2 > run_output.log 2>&1; tail -n 80 run_output.log
```

Running Evals:
```bash
python scripts/ubi_report.py \
--model-dir artifacts \
--drivers 300 \
--periods 6 > artifacts/ubi_run.log 2>&1; \
grep -i 'ubi_report' artifacts/ubi_run.log || tail -n 60 artifacts/ubi_run.log
```


### Arguments
| Flag | Default | Description |
|------|---------|-------------|
| `--events` | 3000 | Total mock telemetry events to generate |
| `--drivers` | 10 | Number of synthetic drivers |
| `--seed` | 42 | RNG seed for reproducibility |
| `--model-artifacts` | `artifacts` | Directory containing (or to store) model files |
| `--dump-events` | None | If set, writes validated events (JSONL) |
| `--dump-features` | None | If set, writes aggregated feature rows (JSONL) |
| `--sample` | 5 | Number of scored feature rows to print |
| `--inject-extremes` | off | Post-aggregation injection of adaptive low/high synthetic rows (guarantee a target spread) |
| `--extreme-pairs` | 1 | Number of injected low/high pairs when `--inject-extremes` is set |
| `--extreme-variance` | off | Enable generator driver risk profiles + amplified intensities for organic wider variance |
| `--debug-driver-sample` | 0 | If >0 prints a filtered subset of non-injected driver rows for inspection |
| `--premium-target-spread` | None | Override dynamic pricing spread (TARGET_SPREAD) at runtime |
| `--log-file` | None | Write full log (stdout may be truncated if piped) |

## What It Outputs
- Console / log file `local_pipeline_demo.log` lines describing each stage.
- If no existing model artifacts are present, a quick synthetic training run occurs (re-uses `synthesize_dataset_improved`).
- Printed sample predictions include `risk_score` and `premium_multiplier`.
- Summary statistics for both prediction arrays.

### Interpreting `risk_score` & `premium_multiplier`
| Field | Meaning (Prototype) | Typical Range | Notes |
|-------|---------------------|---------------|-------|
| `risk_score` | Relative latent risk (synthetic, 0–1 clipped) | ~0.18–0.75 center, tails 0.01–0.99 | Not calibrated probability yet |
| `premium_multiplier` | Dynamic scaling around baseline mean risk using training distribution percentiles | Spread target default 0.35 | Adaptive band preserves differentiation after generator tuning |

Dynamic multiplier formula:
```
scaling_factor = TARGET_SPREAD / (p95 - p5)
premium_multiplier = 1 + (risk_score - baseline_risk) * scaling_factor
```
`TARGET_SPREAD` defaults to 0.35 or can be set with `--premium-target-spread` (or env var `PREMIUM_SCALING_TARGET_SPREAD`).

Lower `risk_score` → multiplier below 1 (discount); higher → above 1 (surcharge). Treat current bands as heuristic until calibrated on real claims data.

The script now also prints an explicit spread line:

```
Risk score spread (max - min): 0.237 (min=0.112, max=0.349)
```

This is useful when tuning variance mechanisms.

### Pricing Output

After model scoring, the pricing engine (`src/aws_lambda/pricing_engine`) is invoked to compute:
- `pricing.final_multiplier`
- `pricing.final_monthly_premium`

It also prints a premium spread line similar to risk spread.

## Variance Exploration Modes

Two complementary approaches exist to widen distribution:

1. Generator Profiles (`--extreme-variance`):
  - Assigns each driver a risk profile (ultra_safe → ultra_risky)
  - Adjusts both occurrence probabilities and intensity (e.g., braking_g, speeding duration)
  - Impacts feature denominators (miles) to shift per‑100mi normalization organically.

2. Adaptive Post-Aggregation Injection (`--inject-extremes`):
  - After aggregating real drivers, synthesizes low/high rows.
  - Iteratively scales high-risk feature magnitudes until a target delta (≥0.2) between model predictions is achieved (or max scale reached).

Use them together for maximum spread during experimentation. For a more realistic evaluation, prefer relying on the generator mode and disable injection once natural variance is sufficient.

## Feature Columns Used For Scoring
Current `FEATURE_COLUMNS` (behavior + static + interaction):
- hard_braking_events_per_100mi
- aggressive_turning_events_per_100mi
- tailgating_time_ratio
- speeding_minutes_per_100mi
- late_night_miles_per_100mi
- miles
- prior_claim_count (or fallback heuristic tier)
- car_value (normalized; if raw >500 scaled /10000)
- car_sportiness
- car_speeding_interaction = (speeding_minutes_per_100mi/10)*(hard_braking_events_per_100mi/5)

Missing new columns in legacy feature dumps are auto-added with zeros prior to the model pipeline transformation.

Missing columns (if any future features are added) are imputed to 0 before passing through the model's preprocessing pipeline (which will apply median imputation + scaling).

## Model Artifacts
If `artifacts/xgb_model.json` is absent:
- Trains a small model (reduced boosting rounds) for fast startup.
- Saves: `xgb_model.json`, `feature_pipeline.joblib`, `meta.json`.

## Extending The Demo
| Goal | Change |
|------|--------|
| Add enrichment-based features | Extend feature calculators and regenerate feature rows |
| Simulate multiple periods | Increase `--events` so per-driver monthly buckets accumulate |
| Test model retraining on real features | Replace synthetic training set with exported feature_rows JSONL converted to CSV with target column appended |
| Benchmark performance | Wrap generate/aggregate/score sections with timers |

## Troubleshooting
| Symptom | Cause | Fix |
|---------|-------|-----|
| No feature rows | Exposure miles below `MIN_EXPOSURE_MILES` (default 5) | Lower env var or increase events |
| ImportError for modules | `PYTHONPATH` not set to project root | Run with `PYTHONPATH=.` prefix |
| All premium multipliers ~1.0 | Model baseline close to predictions (untrained or low variance) | Enable variance flags, retrain, or adjust `--premium-target-spread` upward |
| Car attributes missing | Events lacked static fields & fallback disabled | Ensure generator includes attributes; fallback now auto-populates deterministically |

## Exit Criteria For This Demo
The script is considered successful if:
1. Generates >=1 feature row for provided input size.
2. Loads or trains model without exception.
3. Produces arrays of equal length for `risk_score` and `premium_multiplier`.

---
Update this doc whenever new features or model input columns are introduced.
