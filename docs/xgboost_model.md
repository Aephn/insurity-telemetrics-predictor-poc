# XGBoost Telematics Risk Model Documentation

## Purpose
This document explains the structure, assumptions, parameters, and transformation formulas of the prototype XGBoost model implemented in `models/xgboost_sagemaker.py`. It supports a usage-based (UBI) auto insurance pricing workflow by mapping aggregated telematics behavior metrics to a continuous risk estimate and downstream premium & safety score transformations.

---
## Conceptual Layers
1. **Raw Telematics Events** (trip-level): braking events, speed samples, timestamps, GPS, etc.
2. **Aggregated Period Features** (driver-period, e.g. month): engineered metrics per 100 miles or as ratios.
3. **Risk Model (XGBoost)**: predicts a latent continuous `risk_score` (proxy for expected relative loss or frequency-severity blend).
4. **Business Mappings**: convert `risk_score` to premium multiplier and driver-facing safety score.
5. **Engagement Layer**: gamification badges, factor insights, improvement tracking.

---
## Feature Set (Current Prototype)
| Feature | Description | Type | Notes |
|---------|-------------|------|-------|
| `hard_braking_events_per_100mi` | Harsh decelerations normalized by exposure | float | Safety behavior proxy |
| `aggressive_turning_events_per_100mi` | High lateral acceleration turns | float | Cornering risk |
| `tailgating_time_ratio` | Proportion of time following too closely (0-1) | float | Collision precursor |
| `speeding_minutes_per_100mi` | Minutes over speed threshold per 100 miles | float | Severity / frequency risk |
| `late_night_miles_per_100mi` | Night driving (00:00–04:00) miles per 100 miles | float | Elevated risk window |
| `miles` | Exposure (total period miles) | float | Controls for scale |
| `prior_claim_count` | Historical claim count | int | Baseline risk anchor |
| `target_risk` | LABEL: latent synthetic risk (0-1) | float | To be replaced by real target |

---
## Synthetic Data Generation (Current Implementation v2)
The original simple linear + sigmoid generator has been replaced by a richer process (`synthesize_dataset_improved`) introducing population heterogeneity, non-linear interactions, and weak seasonality. This produces more varied learning signals and a wider, smoother risk spectrum.

### 1. Driver Archetypes
Each synthetic driver is assigned one of three archetypes with probabilities (safe 70%, moderate 20%, risky 10%). Each archetype sets a base risk anchor and governs feature distributions.

| Archetype | Weight | Base Risk | Qualitative Pattern |
|-----------|--------|-----------|---------------------|
| safe      | 0.70   | 0.10      | Low harsh events, short speeding time, minimal late-night miles |
| moderate  | 0.20   | 0.30      | Mid-level events, moderate speeding & night miles |
| risky     | 0.10   | 0.70      | High harsh/turn events, higher tailgating & speeding, more night miles |

### 2. Feature Sampling by Archetype
Gamma / normal draws differ per archetype to shift means and dispersion (e.g., risky drivers have higher shape & scale for harsh braking). Tailgating ratio and night miles are clipped to realistic bounds.

### 3. Interaction & Non-linear Effects
A latent interaction augments the linear risk driver:
```
tailgating_effect           = tailgating_time_ratio * 2.0
speed_braking_interaction   = (speeding_minutes_per_100mi / 10) * (hard_braking_events_per_100mi / 5) * 0.5

linear_risk = (tailgating_effect
               + 0.08 * hard_braking_events_per_100mi
               + 0.06 * aggressive_turning_events_per_100mi
               + 0.04 * speeding_minutes_per_100mi
               + 0.03 * late_night_miles_per_100mi
               + 0.02 * prior_claim_count
               + speed_braking_interaction)
```

### 4. Seasonality
A mild seasonal factor (sine wave over period index) inflates or deflates risk:
```
seasonal_factor = 1 + 0.1 * sin(2π * period_index / 12)
```

### 5. Noise & Transformation
Gaussian noise (σ≈0.08) is applied before a logistic squashing step with a shifted center; then combined with the archetype base risk and seasonality:
```
raw_sigmoid = 1 / (1 + exp(-(linear_risk + noise - 1.0)))
risk = base_risk + raw_sigmoid * seasonal_factor
risk = clip(risk, 0.01, 0.99)
```

### 6. Prior Claims Coupling
`prior_claim_count` is sampled from a Poisson whose rate scales with `base_risk`, creating natural correlation between historical claims and current generated risk (emulating real-world signal leakage you’d later control for via temporal separation in a production pipeline).

### 7. Extra Field: `driver_type`
The synthesized dataset now includes a `driver_type` column (the archetype label) retained for validation, fairness audits, and potential monitoring—but it is deliberately **excluded** from `FEATURE_COLUMNS` so the model learns from behaviors not a synthetic identity class.

### Rationale for Changes
| Enhancement | Benefit |
|-------------|---------|
| Archetypes | Introduces multi-modal target distribution & variance structure |
| Interactions | Encourages trees to learn combined effects beyond additive linearity |
| Seasonality | Gives mild temporal drift pattern to avoid stationary oversimplification |
| Correlated Prior Claims | Mimics partial information leakage & real insurance context |
| Clipping & Bounds | Keeps risk in a realistic (0,1) open interval, avoiding degenerate 0/1 |

In production replace this generator with actual aggregated features & targets (frequency, severity, or composite expected loss). The archetype field can inspire segmentation / fairness evaluation but would not exist as a direct feature.

---
## Model Training Flow
1. **Load / synthesize dataset** (CSV channel or synthetic generator).
2. **Split** into train / validation (default 80/20).
3. **Preprocess** using a scikit-learn `Pipeline`:
   - `SimpleImputer(strategy="median")`
   - `StandardScaler()`
4. **Train XGBoost** with regression objective (`reg:squarederror`).
5. **Early stopping** on validation RMSE.
6. **Persist** artifacts:
   - `xgb_model.json` (booster)
   - `feature_pipeline.joblib` (imputer + scaler)
   - `meta.json` (baseline mean risk)

---
## Core XGBoost Parameters (Default Set)
| Parameter | Default | Role | Guidance |
|-----------|---------|------|----------|
| `objective` | `reg:squarederror` | Regression loss (MSE) | Use `binary:logistic` or `reg:gamma` for other targets |
| `eta` | 0.08 | Learning rate | Lower → stabler, slower; tune with rounds |
| `max_depth` | 5 | Tree depth | Controls interaction complexity; risk of overfit if high |
| `subsample` | 0.85 | Row sampling per tree | <1 adds stochastic regularization |
| `colsample_bytree` | 0.9 | Feature sampling per tree | Helps reduce correlation & overfit |
| `lambda` | 1.2 | L2 regularization | Increase for smoother weights |
| `alpha` | 0.2 | L1 regularization | Encourages sparsity in splits |
| `seed` | 42 | Determinism | Fix for reproducible experiments |
| `eval_metric` | `rmse` | Validation metric | For classification: `auc`, `logloss`, etc. |
| `num_boost_round` | 400 (CLI) | Upper bound boosting steps | Combine with early stopping |
| `early_stopping_rounds` | 25 | Stops when no improvement | Prevents over-training |

### When Tuning
- Increase `max_depth` only if validation error plateaus early.
- Coordinate `eta` & `num_boost_round`: lower `eta` often needs more rounds.
- If overfitting: reduce `max_depth`, increase `lambda` / `alpha`, lower `subsample` slightly.
- Use SageMaker Hyperparameter Tuner referencing `validation:rmse`.

---
## Risk → Premium Mapping (Prototype)
After inference, each row yields a raw `risk_score` (model prediction). Convert to a premium adjustment multiplier:
```
premium_multiplier = 1 + (risk_score - baseline_risk) * k
```
Where:
- `baseline_risk` = mean training target (stored in `meta.json`)
- `k` = scaling factor (prototype uses 0.25)

Then:
```
adjusted_premium = base_premium * premium_multiplier
```
In production this would integrate with rating components:
```
final_premium = base_rate * territory_factor * vehicle_factor * telematics_factor(risk_score) * taxes_fees
```
`telematics_factor()` can be a calibrated monotonic function or a relativities table.

---
## Risk → Safety Score Mapping (Suggested Extension)
A driver-facing “Safety Score” should be stable, interpretable, and reward improvement.
Example mapping (not yet in code):
```
# Assume risk_score in [0,1], lower is better.
# Smooth with prior (Bayesian shrinkage) if data sparse.
raw_safety = 100 - 70 * (risk_score / (baseline_risk + 1e-6))
safety_score = clip(raw_safety, 0, 100)
```
Optional smoothing across periods (to dampen volatility):
```
smoothed_t = 0.6 * previous_safety + 0.4 * safety_score_current
```
Explainability (driver coaching): derive top contributing behaviors via SHAP; highlight 2–3 most negative factors.

---
## Inference Contract
**Input (JSON or CSV row)** must include the feature columns; missing columns are added as NaN and imputed.
**Output JSON** (current):
```json
{
  "risk_score": [0.3123, 0.4411, ...],
  "premium_multiplier": [0.94, 1.02, ...]
}
```
Recommended extension:
```json
{
  "risk_score": [...],
  "safety_score": [...],
  "premium_multiplier": [...],
  "feature_contributions": [{"hard_braking_events_per_100mi": +0.012, ...}, ...]
}
```

---
## Hyperparameter Search Space Helper
`get_hyperparameter_search_space()` returns numeric ranges to feed into SageMaker’s `HyperparameterTuner`. Example mapping:
```
eta: (0.01, 0.3)
max_depth: (3, 9)
subsample: (0.5, 1.0)
colsample_bytree: (0.5, 1.0)
lambda: (0.5, 5.0)
alpha: (0.0, 2.0)
```
Tune against `validation:rmse` or an aligned objective (e.g., Gini, AUC, Poisson deviance—depending on target).

---
## Suggested Production Enhancements
| Area | Enhancement |
|------|-------------|
| Targets | Separate frequency & severity models; combine expected loss = freq * severity |
| Calibration | Isotonic or Platt scaling for probability interpretability |
| Explainability | SHAP summary + per-inference attributions stored with predictions |
| Drift Monitoring | Population Stability Index (PSI) over features & risk distribution |
| Governance | Model version tagging, lineage tracking, MLflow/SageMaker Registry |
| Fairness | Segment performance (geography, time-of-day cohort) for bias detection |
| Cold Start | Hierarchical priors or cohort averages when sparse data |
| Privacy | Differential privacy/noise addition for aggregated insights |

---
## Quick Local Commands
Train with synthetic data:
```bash
python models/aws_sagemaker/xgboost_model.py --local-train --model-dir artifacts
```
Train + sample inference:
```bash
python models/aws_sagemaker/xgboost_model.py --local-train --predict-sample --model-dir artifacts
```

---
## File Outputs
| File | Purpose |
|------|---------|
| `artifacts/xgb_model.json` | Serialized XGBoost booster |
| `artifacts/feature_pipeline.joblib` | Imputer + scaler pipeline |
| `artifacts/meta.json` | Baseline risk metadata |
| `artifacts/metrics.json` (SageMaker) | Validation metrics (optional) |

---
## Next Steps
1. Add explicit safety score + mapping function to `predict_fn`.
2. Integrate SHAP for driver-level factor explanations.
3. Move synthetic generation out once real aggregated feature process is ready.
4. Introduce versioned model promotion pipeline (dev → staging → prod).
5. Implement monitoring lambda / batch job to recompute + push updated factors nightly.

---
**Contact / Ownership**: Add model steward details here once assigned.
