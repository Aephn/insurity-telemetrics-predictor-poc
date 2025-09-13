"""Third-Party Data Ingestion Lambda (Stub)

Purpose:
  Periodically fetch external context data (e.g., crime stats, weather) and push
  lightweight enrichment events onto the main Kinesis stream for downstream
  feature aggregation.

Current Implementation:
  - Stub only; generates a placeholder enrichment JSON record.
  - Emits to Kinesis if KINESIS_STREAM_NAME env var set; otherwise logs.

Enhance later:
  - Integrate real APIs (rate limiting, retries)
  - Enrich with geo resolution
  - Batch multiple drivers / regions
"""

from __future__ import annotations
import os, json, time, random
from datetime import datetime

try:
    import boto3  # type: ignore
except Exception:  # pragma: no cover
    boto3 = None  # type: ignore

STREAM = os.getenv("KINESIS_STREAM_NAME")
REGION = os.getenv("AWS_REGION", "us-east-1")


def lambda_handler(event, context):  # noqa: ANN001, D401
    record = {
        "event_id": f"enrich{int(time.time()*1000)}{random.randint(100,999)}",
        "driver_id": "D00000",  # placeholder until multi-driver mapping implemented
        "trip_id": "enrichment",
        "ts": datetime.utcnow().isoformat(timespec="milliseconds") + "Z",
        "event_type": "ping",
        "latitude": 37.77,
        "longitude": -122.41,
        "speed_mph": 0,
        "heading_deg": 0,
        "period_minute": 0,
        "local_hour": datetime.utcnow().hour,
        "context": {"crime_index": random.uniform(0, 100), "weather_code": random.randint(0, 99)},
    }
    if STREAM and boto3 is not None:
        k = boto3.client("kinesis", region_name=REGION)
        k.put_record(
            StreamName=STREAM,
            Data=json.dumps(record).encode("utf-8"),
            PartitionKey=record["driver_id"],
        )
        return {"status": "sent", "stream": STREAM}
    print("[ingestion] would send:", record)
    return {"status": "logged"}


if __name__ == "__main__":  # local test
    print(lambda_handler({}, None))
