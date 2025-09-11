# XGBoost Telematics Risk Model Documentation

## Purpose
This document explains the structure, assumptions, parameters, and transformation formulas of the prototype XGBoost model implemented in `models/aws_sagemaker/xgboost_model.py` (script-mode compatible). It supports a usage-based (UBI) auto insurance pricing workflow by mapping aggregated telematics behavior metrics to a continuous risk estimate and downstream premium & safety score / pricing transformations.

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
## Synthetic Data Generation (Current Implementation v3)
The original simple linear + sigmoid generator evolved through multiple iterations. The current version (`synthesize_dataset_improved`) introduces:
1. Population heterogeneity via archetypes.
2. Non-linear & interaction effects.
3. Mild seasonality.
4. Correlated prior claim counts.
5. (New) Stronger emphasis on speeding via BOTH linear and convex (quadratic) terms plus a higher-weight speed×braking interaction to increase model sensitivity to aggressive speed behavior.

This version targets a broader effective gradient zone so that incremental worsening (especially in speeding) materially shifts predicted risk.

### 1. Driver Archetypes
Each synthetic driver is assigned one of three archetypes with probabilities (safe 70%, moderate 20%, risky 10%). Each archetype sets a base risk anchor and governs feature distributions.

| Archetype | Weight | Base Risk | Qualitative Pattern |
|-----------|--------|-----------|---------------------|
| safe      | 0.70   | 0.10      | Low harsh events, short speeding time, minimal late-night miles |
| moderate  | 0.20   | 0.30      | Mid-level events, moderate speeding & night miles |
| risky     | 0.10   | 0.70      | High harsh/turn events, higher tailgating & speeding, more night miles |

### 2. Feature Sampling by Archetype
Gamma / normal draws differ per archetype to shift means and dispersion (e.g., risky drivers have higher shape & scale for harsh braking). Tailgating ratio and night miles are clipped to realistic bounds.

### 3. Interaction & Non-linear Effects (Updated Emphasis)
Updated risk core (weights changed to increase sensitivity to speeding):
```
tailgating_effect         = tailgating_time_ratio * 2.0
speed_braking_interaction = (speeding_minutes_per_100mi / 10) * (hard_braking_events_per_100mi / 5) * 0.6
speeding_linear           = 0.10 * speeding_minutes_per_100mi
speeding_convex           = 0.02 * (speeding_minutes_per_100mi ** 2 / 100.0)

linear_risk = (tailgating_effect
               + 0.08 * hard_braking_events_per_100mi
               + 0.06 * aggressive_turning_events_per_100mi
               + speeding_linear
               + speeding_convex
               + 0.03 * late_night_miles_per_100mi
               + 0.02 * prior_claim_count
               + speed_braking_interaction)
```
Rationale: Steeper slope + curvature for speeding yields larger marginal differences between mid and extreme behaviors, addressing earlier low output variance.

### 4. Seasonality
A mild seasonal factor (sine wave over period index) inflates or deflates risk:
```
seasonal_factor = 1 + 0.1 * sin(2π * period_index / 12)
```

### 5. Noise & Transformation
Gaussian noise (σ≈0.07) is applied then passed through a logistic with a **slightly lower center (0.9)** to widen mid-range gradient sensitivity:
```
raw_sigmoid = 1 / (1 + exp(-(linear_risk + noise - 0.9)))
risk = base_risk + raw_sigmoid * seasonal_factor
risk = clip(risk, 0.01, 0.99)
```
Lowering the center from 1.0 → 0.9 increases slope at moderate linear_risk values, widening score separation for common risk scenarios.

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
## Risk → Premium Mapping (Prototype, Dynamic Scaling v2)
Inference yields `risk_score` predictions which are mapped to a premium multiplier via a *dynamic scaling* mechanism using prediction distribution statistics captured at training time.

### Stored Distribution Stats
At model training we persist (`meta.json`):
```
{
  "baseline_risk": <float>,
  "dist_stats": {
     "pred_p5": <float>,
     "pred_p50": <float>,
     "pred_p95": <float>,
     "pred_std": <float>
  }
}
```
The percentile spread (p95 - p5) informs a dynamic scaling factor so the effective premium band remains stable despite generator tuning.

### Formula
```
scaling_factor = TARGET_SPREAD / (p95 - p5)
premium_multiplier = 1 + (risk_score - baseline_risk) * scaling_factor
```
Defaults:
- `TARGET_SPREAD` (desired impact of central 90% band) defaults to 0.35 unless overridden by env var `PREMIUM_SCALING_TARGET_SPREAD`.
- If distribution stats unavailable, fallback constant ~0.35 used.

This replaces the earlier fixed `k=0.25` approach, making the pricing adjustment resilient to upstream risk distribution widening/narrowing.

### Environment Override
```
export PREMIUM_SCALING_TARGET_SPREAD=0.45   # larger differentiation
```
Adjust upward for stronger incentives; monitor tail relativities.

### Interpreting `risk_score` (Scale & Meaning)
**What it IS:** A *relative*, continuous latent risk indicator produced by a regression model trained on a synthetic target in (0,1).

**What it is NOT (yet):** A calibrated probability of claim / loss ratio. Because:
1. The training objective is squared error on a synthetic, sigmoid‑shaped construct (not an observed Bernoulli outcome).
2. No calibration step (Platt, isotonic, beta scaling) has been applied.
3. Synthetic generation blends frequency *and* severity proxies into a single scalar.

Practical interpretation today:
```
Lower risk_score  → comparatively safer cohort (below baseline mean)
Higher risk_score → comparatively riskier cohort (above baseline mean)
```

### Typical Numeric Range (Prototype Data)
The synthetic generator clips targets to [0.01, 0.99], but empirical distributions concentrate around ~0.18–0.75 with a mean (`baseline_risk`) usually ~0.30–0.40 (will appear in `meta.json`). Values outside that (e.g. <0.1 or >0.85) are tail cases and should be treated with caution until calibrated.

### Choosing TARGET_SPREAD
| Objective | TARGET_SPREAD Guidance |
|----------|------------------------|
| Conservative intro | 0.20–0.30 |
| Moderate differentiation | 0.30–0.40 |
| Aggressive | 0.40–0.55 (pair with caps) |

Validate that the realized premium multiplier distribution matches intent; extremely skewed feature populations may still require capping or monotonic smoothing.

### Calibration Path (Future)
To turn `risk_score` into a probability / expected loss factor:
1. Obtain true targets (e.g. claim frequency per exposure unit). 
2. Retrain using an appropriate objective (`count:poisson` or `binary:logistic` for frequency). 
3. Apply calibration (isotonic or Platt) using out‑of‑fold predictions. 
4. Store calibration artifact; wrap inference to apply transform before premium mapping.

### Safety Score Mapping (Expanded Rationale)
If exposing a driver‑facing score, map *monotonically inverse* to risk, smooth over time, and bucket for UI clarity (e.g. A/B/C tiers). Provide relative positioning ("Top 18% safest") rather than raw decimals.

### Sanity Checks Before Production
| Check | Why |
|-------|-----|
| Compare distribution shift vs. training baseline_risk | Detect drift / population change |
| Correlate risk_score with known loss proxies | Validate predictive ordering |
| Backtest multiplier band impact on premium mix | Ensure revenue neutrality / fairness |
| Evaluate segmentation fairness (age/territory if available) | Governance & compliance |
| Stress test extreme feature combinations | Guardrail against extrapolation |

> Until these checks are done on real data, treat the prototype multiplier as a *sandbox heuristic*.

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
## Hyperparameter / Retraining Controls
Added CLI flag in local pipeline (`--force-retrain`) to force rebuild of artifacts after sensitivity changes.

Environment variable (inference): `PREMIUM_SCALING_TARGET_SPREAD` adjusts dynamic scaling without retraining.

### Hyperparameter Search Space Helper
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
| `artifacts/meta.json` | Baseline risk + distribution stats |
| `artifacts/metrics.json` (SageMaker) | Validation metrics (optional) |

---
## Next Steps
1. Add explicit safety score + mapping function to `predict_fn`.
2. Integrate SHAP for driver-level factor explanations.
3. Evaluate calibration (isotonic / Platt) on real labels.
4. Introduce versioned model promotion pipeline (dev → staging → prod).
5. Implement monitoring job using distribution stats to detect drift (compare live p5/p95 vs training values). 
6. Add multiplier capping & guardrails (e.g., clamp to [0.7, 1.4]) configurable by product line.

---
**Contact / Ownership**: Add model steward details here once assigned.
