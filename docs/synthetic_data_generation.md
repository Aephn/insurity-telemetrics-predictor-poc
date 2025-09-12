
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

#### Integration Notes
- Batch JSONL can feed a prototype ingestion API (`POST /telematics/ingest`).
- A downstream aggregation job can roll events into monthly driver-level features consumed by the XGBoost model.
- Keep synthetic vs. production data clearly separated; never mix device identifiers.

#### Future Enhancements
- Add correlated weather or traffic context fields.
- Add synthetic anomaly bursts (e.g., session with elevated speeding).
- Parameterize geographic clusters to simulate multi-region fleets.


