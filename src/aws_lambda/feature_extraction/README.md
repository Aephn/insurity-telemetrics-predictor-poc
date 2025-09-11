Feature Extraction Lambda
=========================

Purpose: Consume raw (or enriched) telemetry events from Kinesis, aggregate them into
driver-period feature rows used by the XGBoost risk model. Designed for easy addition
of new features (including future 3rd-party enrichment outputs) via a small
calculator interface.

Flow:
1. Kinesis Trigger -> batch of events (validated upstream)
2. Group by (driver_id, period_key) (monthly by default)
3. Apply feature calculators incrementally per event
4. Finalize each group -> feature dict
5. Emit to FEATURES_STREAM (optional) or log / future DynamoDB sink

Extensibility: Add a file under `features/` implementing `BaseFeatureCalculator`.
Register it inside `features/registry.py`.

Environment Variables:
- FEATURES_STREAM_NAME: (optional) Kinesis stream to publish feature rows
- FEATURES_PARTITION_KEY_FIELD=driver_id
- PERIOD_GRANULARITY=MONTH  (future: DAY, HOUR)
- MIN_EXPOSURE_MILES=5.0   (skip very low exposure feature rows)

Output Schema (baseline):
{
  "driver_id": "D0001",
  "period_key": "2025-01",
  "period_start": "2025-01-01",
  "period_end": "2025-01-31",
  "miles": 812.4,
  "hard_braking_events_per_100mi": 3.21,
  "aggressive_turning_events_per_100mi": 2.14,
  "tailgating_time_ratio": 0.11,
  "speeding_minutes_per_100mi": 5.6,
  "late_night_miles_per_100mi": 2.05,
  "prior_claim_count": 0,            # placeholder (external data integration)
  "feature_version": 1
}

Future Hooks:
- Append enriched feature calculators (weather risk index, traffic congestion exposures, crime risk weighting)
- DynamoDB upsert or S3 parquet sink
- Metrics & quality validation (min counts, anomaly detection)
