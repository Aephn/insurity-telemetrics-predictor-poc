
# Mock Telemetry Data Generation (POC)

A flexible synthetic telematics event generator is provided at `data/mock.py` to bootstrap experimentation without real devices and provide a POC example for the pipeline.

Use cases:
- Produce a JSONL or CSV batch of event-level records for local feature engineering.
- Stream events to stdout to simulate a live ingestion feed.
- Adjust distributions or add new event types rapidly.

#### Quick Commands

Generate 1,000 JSONL events (default 10 drivers):
```bash
python data/mock.py --events 1000 --out telemetry_events.jsonl
```

Generate CSV for 25 drivers:
```bash
python data/mock.py --events 5000 --drivers 25 --format csv --out telemetry_events.csv
```

Stream events continuously (Ctrl+C to stop):
```bash
python data/mock.py --stream --interval 0.25
```

Override random seed for reproducibility:
```bash
python data/mock.py --events 2000 --seed 123 --out sample.jsonl
```

#### Output Schema (Representative Fields)
| Field | Description |
|-------|-------------|
| `event_id` | Unique identifier (UUID hex) |
| `driver_id` | Synthetic driver code (D0000..) |
| `trip_id` | Synthetic trip grouping identifier |
| `ts` | ISO8601 UTC timestamp |
| `event_type` | One of: hard_braking, aggressive_turn, speeding, tailgating, late_night_driving, ping |
| `latitude` / `longitude` | Jittered coords near a base point |
| `speed_mph` | Instantaneous speed context |
| `heading_deg` | Compass heading 0–359 |
| `period_minute` | Minutes since generator start |
| Type-specific fields | e.g. `braking_g`, `lateral_g`, `over_speed_mph`, `following_distance_m` |
| `car_value` | Raw vehicle value proxy (normalized later in aggregation if large) |
| `car_sportiness` | 0-1 synthetic performance index (higher → greater aggressive propensity) |
| `car_type` | Category label (e.g., sedan, suv, coupe) |

#### Adding a New Event Type
1. Create an attribute function in `data/mock.py`:
```python
def harsh_accel_attrs(rng: random.Random) -> Dict[str, Any]:
	return {"accel_g": round(rng.uniform(0.3, 1.0), 2)}
```
2. Append a new spec to `EVENT_TYPE_CONFIG` (tune probability weight):
```python
EVENT_TYPE_CONFIG.append(EventTypeSpec("harsh_accel", 0.05, harsh_accel_attrs))
```

### Vehicle Attributes & Interaction (Added)
The generator now assigns each driver static vehicle attributes once, reused across their events:

- `car_value`: Drawn from a skewed distribution (bounded positive). Downstream feature extraction normalizes values >500 by dividing by 10,000 to keep model feature scales comparable.
- `car_sportiness`: Uniform / beta-like draw in [0,1]; influences probability of aggressive events (internal coupling) to create mild correlation with harsh behaviors.
- `car_type`: Simple categorical bucket chosen from a shortlist; currently informational only.

During synthetic model training a derived interaction feature `car_speeding_interaction = (speeding_minutes_per_100mi / 10) * (hard_braking_events_per_100mi / 5)` is added after aggregation (not part of raw events) to give the model access to a multiplicative pattern between speeding and braking.

### Prior Claim Count Coupling (Generator Perspective)
When building the standalone synthetic training frame, `prior_claim_count` is sampled using a Poisson-like process whose rate scales with the driver archetype base risk. If that field is absent in later real-time aggregation, a fallback heuristic (see `feature_extraction.md`) derives an approximate tier from current aggressive metrics.

#### Integration Notes
- Batch JSONL can feed a prototype ingestion API (`POST /telematics/ingest`).
- A downstream aggregation job can roll events into monthly driver-level features consumed by the XGBoost model.
- Keep synthetic vs. production data clearly separated; never mix device identifiers.

#### Future Enhancements
- Deeper correlation structure (copulas) between vehicle value and behavior.
- Weather / traffic context fields.
- Synthetic anomaly bursts (e.g., sustained extreme speeding window).
- Geographic clustering to test regional mix effects.


