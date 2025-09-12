# API Endpoints (Ingestion Layer)

All endpoints: POST, `Content-Type: application/json`. Body may be a single object or an array. Responses include counts of valid / invalid and (if enabled) Kinesis forwarding metadata.

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/validate`  | POST | Generic validation + forwarding (all event types) |
| `/telemetry` | POST | Primary ingestion alias (mixed batch) |
| `/status`    | POST | Driver/device status pings (event_type `ping`) |
| `/location`  | POST | High-frequency location updates (usually `ping`) |
| `/trips`     | POST | Trip-segment event batch (mixed event types) |

### Allowed event_type values
`hard_braking`, `aggressive_turn`, `speeding`, `tailgating`, `late_night_driving`, `ping`

### Validation Requirements 
- `event_id`: 32 hex chars
- `driver_id`: `D` + 4+ digits
- `ts`: ISO8601 UTC ending with `Z`
- `speed_mph`: 0–200; `heading_deg`: 0–359
- Type-specific fields must match the `event_type` (e.g., `braking_g` only with `hard_braking`).

### Response Example
```json
{
  "valid_count": 1,
  "invalid_count": 0,
  "sample_valid": [
    {
      "event_id": "abcdef0123456789abcdef0123456789",
      "driver_id": "D0001",
      "event_type": "hard_braking"
    }
  ],
  "kinesis": { "enabled": true, "attempted": 1, "success": 1, "failed": 0 }
}
```

### Status codes
200 (all valid), 207 (partial failures), 400 (bad body), 405 (non-POST), 500 (internal).

---
## Minimal Payload Examples

#### /validate (single object)
```json
{
  "event_id": "abcdef0123456789abcdef0123456789",
  "driver_id": "D0001",
  "trip_id": "TRIP12345",
  "ts": "2025-01-01T12:00:00.000Z",
  "event_type": "hard_braking",
  "latitude": 37.77,
  "longitude": -122.41,
  "speed_mph": 42.5,
  "heading_deg": 123,
  "period_minute": 15,
  "braking_g": 0.55
}
```

#### /telemetry (array batch)
```json
[
  {
    "event_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "driver_id": "D0001",
    "trip_id": "TRIPA1",
    "ts": "2025-01-01T12:01:00.000Z",
    "event_type": "ping",
    "latitude": 37.7701,
    "longitude": -122.4102,
    "speed_mph": 15.2,
    "heading_deg": 40,
    "period_minute": 16
  }
]
```

#### /status
```json
{
  "event_id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "driver_id": "D0001",
  "trip_id": "TRIPA1",
  "ts": "2025-01-01T12:02:00.000Z",
  "event_type": "ping",
  "latitude": 37.7702,
  "longitude": -122.4103,
  "speed_mph": 12.0,
  "heading_deg": 55,
  "period_minute": 17
}
```

#### /location
```json
{
  "event_id": "cccccccccccccccccccccccccccccccc",
  "driver_id": "D0001",
  "trip_id": "TRIPA1",
  "ts": "2025-01-01T12:02:30.000Z",
  "event_type": "ping",
  "latitude": 37.77025,
  "longitude": -122.41035,
  "speed_mph": 18.7,
  "heading_deg": 72,
  "period_minute": 17
}
```

#### /trips (array with event-specific fields)
```json
[
  {
    "event_id": "dddddddddddddddddddddddddddddddd",
    "driver_id": "D0001",
    "trip_id": "TRIPA1",
    "ts": "2025-01-01T12:03:00.000Z",
    "event_type": "speeding",
    "latitude": 37.7703,
    "longitude": -122.4104,
    "speed_mph": 55.0,
    "heading_deg": 90,
    "period_minute": 18,
    "posted_speed_mph": 35,
    "over_speed_mph": 20.0,
    "duration_sec": 60
  }
]
```
