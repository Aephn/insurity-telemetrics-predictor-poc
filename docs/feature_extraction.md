# Feature Extraction Documentation

This document describes the driver-period features currently produced by the Feature Extraction Lambda located at `src/aws_lambda/feature_extraction/`. It explains:
- The raw telemetry event fields each feature depends on.
- The aggregation period and normalization logic.
- Exact formulas and edge-case handling.
- Extension points for adding new features (including 3rd‑party enrichments later).

## 1. Input Event Schema (Relevant Fields)
Events originate from the validation / enrichment pipeline (currently raw mock generator) with (at minimum):

| Field | Type | Description |
|-------|------|-------------|
| `driver_id` | str | Synthetic driver identifier (e.g. D0001) |
| `ts` | ISO8601 str | Event timestamp (UTC) |
| `event_type` | str | One of: hard_braking, aggressive_turn, speeding, tailgating, late_night_driving, ping |
| `speed_mph` | float | Instantaneous speed (used for exposure approximation) |
| `duration_sec` | int/float | Duration of a speeding episode (speeding events only) |
| `period_minute` | int | Minute offset since generator start (not directly used by feature Lambda) |
| Additional type-specific fields | varies | e.g. `braking_g`, `lateral_g`, etc. (not yet aggregated) |

## 2. Aggregation Period Semantics
Default period granularity: MONTH
- Period key: `YYYY-MM`
- Period start: first day of month (e.g. 2025-01-01)
- Period end: last day of month (e.g. 2025-01-31)

Environment variable `PERIOD_GRANULARITY` supports future values (DAY, HOUR) but only MONTH is expected in current workflow.

Grouping key: `(driver_id, period_key)`

## 3. Exposure Approximation
Because the mock stream lacks explicit odometer deltas, exposure miles are estimated by treating each event as a one-minute time slice:
```
∆miles_event = speed_mph / 60.0
miles = Σ (speed_mph / 60) over all events for the driver-period
```
If `speed_mph` is missing or not numeric, the event contributes 0 miles.

This exposure forms the denominator for per-100-mile normalizations.

A minimum exposure threshold (`MIN_EXPOSURE_MILES`, default 5.0) filters out sparse periods to reduce noise; rows below this threshold are discarded.

## 4. Feature List & Formulas
| Feature Name | Description | Raw Dependencies | Formula / Logic | Edge Handling |
|--------------|-------------|------------------|-----------------|---------------|
| `miles` | Estimated exposure miles in period | `speed_mph` | See Exposure Approximation above | Missing speed → 0 contribution |
| `hard_braking_events_per_100mi` | Frequency of harsh braking normalized per 100 miles | `event_type`, `miles` | `100 * count(event_type == hard_braking) / miles` | If miles ≤ 0 → 0.0 |
| `aggressive_turning_events_per_100mi` | Aggressive turn frequency per 100 miles | `event_type`, `miles` | `100 * count(event_type == aggressive_turn) / miles` | If miles ≤ 0 → 0.0 |
| `tailgating_time_ratio` | Proportion of tailgating event minutes to total event minutes | `event_type`, total events | `count(event_type == tailgating) / total_event_minutes` | total_event_minutes ≤ 0 → 0.0 |
| `speeding_minutes_per_100mi` | Minutes spent speeding per 100 miles | `event_type`, `duration_sec`, `miles` | `100 * (Σ duration_sec(speeding)/60) / miles` (fallback +1/60 if duration missing) | If miles ≤ 0 → 0.0 |
| `late_night_miles_per_100mi` | Exposure during late-night events per 100 miles | `event_type`, `speed_mph`, `miles` | `100 * (Σ speed_mph/60 for late_night_driving) / miles` | If miles ≤ 0 → 0.0 |
| `prior_claim_count` | Placeholder for external claims history | (None internal) | Always `0` (stub) | To be replaced by external join |

All computed per driver-period; per-100mi metrics use the *final period exposure miles* after all events processed.

## 5. Calculation Order & Shared State
1. `ExposureMiles` (establishes `exposure_miles` & `total_event_minutes`).
2. Count-based calculators (hard braking, aggressive turn) use final `exposure_miles` in `finalize`.
3. Ratio / per-100mi calculators reference shared exposure and total minutes.
4. Placeholder calculators append static fields.

Shared state keys:
- `exposure_miles` (float)
- `total_event_minutes` (int)
- Period metadata: `period_start`, `period_end`

## 6. Output Row Schema (Current)
Example JSON row:
```json
{
  "driver_id": "D0007",
  "period_key": "2025-01",
  "period_start": "2025-01-01",
  "period_end": "2025-01-31",
  "feature_version": 1,
  "miles": 843.27,
  "hard_braking_events_per_100mi": 3.1825,
  "aggressive_turning_events_per_100mi": 2.4113,
  "tailgating_time_ratio": 0.1125,
  "speeding_minutes_per_100mi": 6.5401,
  "late_night_miles_per_100mi": 1.8459,
  "prior_claim_count": 0
}
```

## 7. Design Choices & Rationale
| Aspect | Decision | Rationale |
|--------|----------|-----------|
| Minutes-as-slices | Each event treated as 1 minute | Simplifies exposure estimation in absence of trip duration fields |
| Per-100mi normalization | Standardize frequency metrics | Comparable across different exposure levels |
| Minimum exposure filter | Skip miles < 5 | Removes extremely noisy small denominators |
| Placeholder claims | Static 0 | Allows model schema stability until external integration |
| Late-night definition | Events tagged with `late_night_driving` | Explicit tagging simplifies separation from general events |

## 8. Adding New Features
1. Create a class in `features/` implementing `BaseFeatureCalculator` with `init_state`, `update`, and `finalize`.
2. Register it in `features/registry.py` (maintain desired ordering).
3. Use `shared` dict in `finalize` to access exposure or total minutes.
4. For enrichment-derived metrics (e.g. weather risk): ensure upstream enrichment attaches required fields, then reference them in the new calculator.

## 9. Future Enhancements
| Enhancement | Description |
|-------------|-------------|
| Trip-level duration | Replace minutes heuristic with explicit trip segmentation for mileage | 
| Distance integration | Utilize GPS delta or odometer for precise miles | 
| Weighted speeding severity | Combine duration with over_speed_mph for severity scoring | 
| Braking severity index | Aggregate `braking_g` distribution (e.g., p95 or weighted sum) | 
| Tailgating distance normalization | Incorporate following distance & speed context into exposure metric | 
| External context features | Weather severity exposure, congestion-adjusted miles, crime-weighted nighttime driving | 
| Quality checks | Flag periods with anomalously low/high counts vs. distribution | 

## 10. Versioning
- `feature_version` increments whenever a calculation definition or normalization strategy changes.
- Downstream model training pipelines should record the feature version to ensure compatibility and enable backfills.

---
**Ownership:** Feature engineering module (WIP) – update this document when new calculators are added.
