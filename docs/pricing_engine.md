# Pricing Engine

Implements transformation of driver-period features + model outputs into a final monthly premium.

## Flow
1. Input feature row(s) (output of feature extraction Lambda).
2. Load trained model artifacts (`artifacts/`).
3. Generate `risk_score` & model `premium_multiplier` via `predict_fn`.
4. Apply tiered behavior adjustments (lightweight, transparent business rules).
5. Combine & cap to obtain `final_multiplier`.
6. Multiply by base premium; enforce min/max bounds.

## Environment Variables
| Variable | Default | Description |
|----------|---------|-------------|
| MODEL_ARTIFACTS_DIR | artifacts | Directory with `xgb_model.json` etc. |
| BASE_MONTHLY_PREMIUM | 110 | Nominal starting premium (USD) |
| MIN_PREMIUM | 50 | Lower bound after adjustments |
| MAX_PREMIUM | 400 | Upper bound after adjustments |
| MIN_FACTOR | 0.7 | Minimum allowed final multiplier |
| MAX_FACTOR | 1.5 | Maximum allowed final multiplier |

## Behavior Adjustment Tiers
Small additive adjustments (applied after model multiplier to avoid double counting) – values are percentage deltas (e.g. +0.02 = +2%).

| Metric | Tier Rule | Adjustment |
|--------|-----------|------------|
| hard_braking_events_per_100mi | <2 | -2% |
|  | 2–<4 | 0% |
|  | 4–<6 | +2% |
|  | ≥6 | +5% |
| tailgating_time_ratio | <0.05 | -1% |
|  | 0.05–<0.15 | 0% |
|  | 0.15–<0.25 | +2% |
|  | ≥0.25 | +4% |
| speeding_minutes_per_100mi | <3 | -1% |
|  | 3–<7 | 0% |
|  | 7–<12 | +2% |
|  | ≥12 | +5% |
| late_night_miles_per_100mi | <1 | 0% |
|  | 1–<4 | +1% |
|  | 4–<8 | +2% |
|  | ≥8 | +4% |
| prior_claim_count | each claim | +5% (capped +15%) |
| miles | <500 | -3% |
|  | 500–1100 | 0% |
|  | >1100 | +3% |

Aggressive turning is left to the model signal only (no explicit tier) to reduce over‑correction.

## Multiplier Composition
```
model_multiplier = output from model.predict_fn
behavior_adjustment_sum = Σ tier adjustments
unbounded_multiplier = model_multiplier * (1 + behavior_adjustment_sum)
final_multiplier = clamp(unbounded_multiplier, MIN_FACTOR, MAX_FACTOR)
```

## Final Premium
```
raw_premium = BASE_MONTHLY_PREMIUM * final_multiplier
final_monthly_premium = clamp(raw_premium, MIN_PREMIUM, MAX_PREMIUM)
```

## Output Schema (per row)
```json
{
  "driver_id": "D0007",
  "period_key": "2025-01",
  "risk_score": 0.42,
  "model_premium_multiplier": 1.05,
  "behavior_adjustments": [
    {"metric": "hard_braking_events_per_100mi", "value": 3.2, "adj": 0.0},
    {"metric": "tailgating_time_ratio", "value": 0.11, "adj": 0.0},
    ...
  ],
  "pricing": {
    "model_multiplier": 1.05,
    "behavior_adjustment_sum": 0.01,
    "unbounded_multiplier": 1.0605,
    "final_multiplier": 1.0605,
    "base_premium": 110.0,
    "raw_premium": 116.65,
    "final_monthly_premium": 116.65,
    "min_premium": 50.0,
    "max_premium": 400.0
  }
}
```

## Local Invocation
After training a model (or using existing artifacts):
```bash
PYTHONPATH=. python src/aws_lambda/pricing_engine/handler.py sample_features.json
```
Where `sample_features.json` is a JSON list of feature rows (e.g. from the local pipeline demo).

## Notes & Future Enhancements
| Area | Idea |
|------|------|
| Calibration | Replace linear multiplier with calibrated relativities curve |
| Caps | Dynamic caps based on regulatory / product filing constraints |
| Safety Score | Derive parallel driver-facing score from risk_score |
| Explainability | Per-pricing decomposition (model SHAP + rule tiers) |
| Bundling | Combine vehicle + coverages + telematics in layered pricing pipeline |
