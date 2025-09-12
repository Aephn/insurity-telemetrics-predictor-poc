# Local Pipeline Demo

This document explains the end-to-end local simulation script located at `scripts/local_pipeline_demo.py`.
It stitches together the existing components without AWS infrastructure:

1. Synthetic event generation (`data/mock.py`)
2. Pydantic validation (`src/aws_lambda/validation/handler.validate_events`)
3. Feature aggregation (`src/aws_lambda/feature_extraction/handler._aggregate`)
4. Model loading / (on-demand) training + scoring (`models/aws_sagemaker/xgboost_model.py`)

## Why This Exists
Acts as an integration smoke test and a reproducible way to sanity‑check feature / model drift during development without provisioning streams or Lambdas.

## Prerequisites
- Python environment with project dependencies installed (see `pyproject.toml`).
- xgboost, pandas, scikit-learn, pydantic available (installed via standard dependency install).

## Run It
From the project root (ensure the root is on `PYTHONPATH` so packages resolve):

```bash
PYTHONPATH=. python scripts/local_pipeline_demo.py \
  --events 3000 \
  --drivers 10 \
  --model-artifacts artifacts \
  --dump-events validated_events.jsonl \
  --dump-features feature_rows.jsonl
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

## What It Outputs
- Console / log file `local_pipeline_demo.log` lines describing each stage.
- If no existing model artifacts are present, a quick synthetic training run occurs (re-uses `synthesize_dataset_improved`).
- Printed sample predictions include `risk_score` and `premium_multiplier`.
- Summary statistics for both prediction arrays.

### Interpreting `risk_score` & `premium_multiplier`
| Field | Meaning (Prototype) | Typical Range | Notes |
|-------|---------------------|---------------|-------|
| `risk_score` | Relative latent risk (synthetic, 0–1 clipped) | ~0.18–0.75 center, tails 0.01–0.99 | Not calibrated probability yet |
| `premium_multiplier` | Linear scaling around baseline mean risk using k=0.25 | ~0.94–1.09 (example) | Adjust base premium: `final = base * multiplier` |

Multiplier formula:
```
premium_multiplier = 1 + (risk_score - baseline_risk) * 0.25
```
Where `baseline_risk` = training set mean stored in `artifacts/meta.json`.

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
Matches `FEATURE_COLUMNS` in the model module:
- hard_braking_events_per_100mi
- aggressive_turning_events_per_100mi
- tailgating_time_ratio
- speeding_minutes_per_100mi
- late_night_miles_per_100mi
- miles
- prior_claim_count

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
| All premium multipliers ~1.0 | Model baseline close to predictions (untrained or low variance) | Train a fuller model (increase events/periods) or enable `--extreme-variance` / `--inject-extremes` |

## Exit Criteria For This Demo
The script is considered successful if:
1. Generates >=1 feature row for provided input size.
2. Loads or trains model without exception.
3. Produces arrays of equal length for `risk_score` and `premium_multiplier`.

---
Update this doc whenever new features or model input columns are introduced.
