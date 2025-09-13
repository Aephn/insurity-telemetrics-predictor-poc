# DynamoDB Schema (Telemetry UBI Platform)

Concise overview of the single-table design used for telematics usage‑based insurance data.

## Table
Name: (default) `TelemetryUserData`  
Billing: On‑demand (PAY_PER_REQUEST)  
PITR: Enabled  
TTL Attribute: `ttl` (only set on expirable items, e.g., recent EVENT rows)  
Primary Keys:  
- Partition Key `PK` = `USER#<driver_id>`  
- Sort Key `SK` = one of entity patterns below

## Entity Types (stored together)
| Entity | SK Pattern | Purpose |
|--------|-----------|---------|
| Profile | `PROFILE` | Static driver profile + base pricing context |
| Period Aggregate | `PERIOD#YYYY-MM` | Monthly aggregated telematics + pricing outputs (chart history) |
| Event | `EVENT#<ISO8601>` | Individual driving event / safety incident (recent feed) |
| Projection | `PROJECTION#YYYY-MM-DD` | Future premium or risk projection points |
| Model Metadata (optional) | `MODEL#<version>` | Model lineage & versioning info |

## Global Secondary Indexes
1. `GSI1_EventsByUser`  
   - PK: `GSI1PK = EVENTS#<driver_id>`  
   - SK: `GSI1SK = <ISO8601 timestamp>`  
   - Use: Query recent events quickly (reverse order client-side).  
   - Projection (INCLUDE): `event_type, severity, value, speedMph`

2. `GSI2_PeriodAggregates`  
   - PK: `GSI2PK = PERIOD#YYYY-MM`  
   - SK: `GSI2SK = <driver_id>`  
   - Use: Cross‑user monthly analytics / cohort comparisons.  
   - Projection (INCLUDE): `risk_score, final_monthly_premium, safety_score`

## Core Fields by Entity
### Profile Item
```
PK, SK=PROFILE, driver_id, name, email, account_created, base_premium, status
```
(Optionally: vehicle info, plan tier, latest_model_version)

### Period Aggregate Item
```
PK, SK=PERIOD#YYYY-MM,
period_start, period_end (optional),
miles_driven, hard_braking_events_per_100mi, harsh_turn_events_per_100mi,
late_night_miles_pct, speeding_minutes, tailgating_ratio,
risk_score, safety_score, final_monthly_premium,
model_multiplier (optional), base_premium, factor_counts (map),
GSI2PK=PERIOD#YYYY-MM, GSI2SK=<driver_id>
```

### Event Item
```
PK, SK=EVENT#<ISO8601>,
event_type, severity, value, speedMph (or context fields),
timestamp (duplicate for convenience), ttl (optional),
GSI1PK=EVENTS#<driver_id>, GSI1SK=<ISO8601>
```
Common `event_type` examples: `hardBraking`, `harshTurning`, `tailgating`, `speeding`.

### Projection Item
```
PK, SK=PROJECTION#YYYY-MM-DD,
projected_premium, projected_risk_score, basis_period (optional), notes
```

### Model Metadata Item (Optional)
```
PK, SK=MODEL#<version>,
version, created_at, features, training_dataset_ref, auc (or other metrics)
```

## Typical Access Patterns
- Get profile: GetItem (PK=USER#id, SK=PROFILE)
- List period history: Query PK=USER#id, SK begins_with PERIOD#
- Latest period: Same query, descending on client or filter by max period
- Recent events: Query GSI1 (GSI1PK=EVENTS#id, descending)
- Future projections: Query PK=USER#id, SK begins_with PROJECTION#
- Cross‑user period analytics: Query GSI2 (GSI2PK=PERIOD#YYYY-MM)

## Design Rationale
- Single table minimizes joins and enables flexible evolving schema.
- Distinct SK prefixes partition entity types cleanly per user.
- GSIs isolate high‑velocity event queries and cross‑user analytics.
- INCLUDE projections keep storage lean while surfacing critical metrics.

## Extension Ideas
- Add `GSI3` for high‑risk users (PK=RISK#<bucket>, SK=<driver_id>)
- Add composite projections for long‑term premium forecast horizons
- Store per‑feature SHAP summaries under `EXPLAIN#<period>` items

(End)
