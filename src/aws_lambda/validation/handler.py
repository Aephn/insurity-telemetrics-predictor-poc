"""Telemetry Event Validation Lambda
===================================

AWS Lambda entrypoint for validating incoming telematics events delivered via
API Gateway (REST or HTTP API). The handler parses the request body (single JSON
object or JSON array), validates each event against the Pydantic schema, and
returns a structured response summarizing acceptance / rejection results.

Intended Usage:
 - Place behind API Gateway integration.
 - On success, forward valid events to downstream pipeline (e.g., Kinesis, SQS, Firehose).
 - On partial failure, return 207-like multi-status (here represented in JSON) so
   the client can re-submit invalid records if desired.

Security / Hardening (Future):
 - Add auth (IAM / Cognito / API key validation) before processing.
 - Enforce size limits (API Gateway default 10MB) and record count caps.
 - Add schema version field & compatibility negotiation.
 - Integrate structured logging & metrics (CloudWatch / EMF).
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Union, Tuple
import os

try:  # boto3 is available in AWS Lambda runtime by default
    import boto3  # type: ignore
    from botocore.exceptions import BotoCoreError, ClientError  # type: ignore
except Exception:  # pragma: no cover - local dev w/out boto3
    boto3 = None  # type: ignore
    BotoCoreError = ClientError = Exception  # type: ignore

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

# -------------------------- Pydantic Models ---------------------------------

UUID_HEX_RE = re.compile(r"^[0-9a-fA-F]{32}$")
EVENT_TYPES = {
    "hard_braking",
    "aggressive_turn",
    "speeding",
    "tailgating",
    "late_night_driving",
    "ping",
}


class TelemetryEvent(BaseModel):
    event_id: str = Field(..., description="UUID hex (32 chars)")
    driver_id: str = Field(..., pattern=r"^D\d{4,}$")
    trip_id: str = Field(..., min_length=5)
    ts: str = Field(..., description="ISO8601 timestamp with 'Z'")
    event_type: str
    latitude: float
    longitude: float
    speed_mph: float = Field(..., ge=0, le=200)
    heading_deg: int = Field(..., ge=0, le=359)
    period_minute: int = Field(..., ge=0, le=100000)

    # Optional type-specific attributes (all optional to allow generic batching)
    braking_g: Optional[float] = Field(None, ge=0, le=2.5)
    abs_activation: Optional[bool] = None
    lateral_g: Optional[float] = Field(None, ge=0, le=3.0)
    turn_direction: Optional[str] = Field(None, pattern=r"^(left|right)$")
    posted_speed_mph: Optional[int] = Field(None, ge=0, le=120)
    over_speed_mph: Optional[float] = Field(None, ge=0, le=100)
    duration_sec: Optional[int] = Field(None, ge=0, le=7200)
    following_distance_m: Optional[float] = Field(None, ge=0, le=200)
    speed_context_mph: Optional[int] = Field(None, ge=0, le=200)
    local_hour: Optional[int] = Field(None, ge=0, le=23)

    @field_validator("event_id")
    @classmethod
    def validate_event_id(cls, v: str) -> str:
        if not UUID_HEX_RE.match(v):
            raise ValueError("event_id must be 32 hex chars (uuid4 hex without dashes)")
        return v.lower()

    @field_validator("event_type")
    @classmethod
    def validate_event_type(cls, v: str) -> str:
        if v not in EVENT_TYPES:
            raise ValueError(f"Unsupported event_type '{v}'")
        return v

    @field_validator("ts")
    @classmethod
    def validate_ts(cls, v: str) -> str:
        # Basic RFC 3339 / ISO8601 (allow microseconds). Must end with Z.
        try:
            if not v.endswith("Z"):
                raise ValueError
            # Remove Z and parse
            datetime.fromisoformat(v[:-1])
        except ValueError as e:
            raise ValueError("ts must be ISO8601 UTC ending with 'Z'") from e
        return v

    @model_validator(mode="after")
    def cross_field_rules(self) -> "TelemetryEvent":
        # Example: braking_g should only appear with hard_braking
        if self.braking_g is not None and self.event_type != "hard_braking":
            raise ValueError("braking_g present but event_type != hard_braking")
        if self.lateral_g is not None and self.event_type != "aggressive_turn":
            raise ValueError("lateral_g present but event_type != aggressive_turn")
        if self.over_speed_mph is not None and self.event_type != "speeding":
            raise ValueError("over_speed_mph present but event_type != speeding")
        if self.following_distance_m is not None and self.event_type != "tailgating":
            raise ValueError("following_distance_m present but event_type != tailgating")
        if self.local_hour is not None and self.event_type != "late_night_driving":
            raise ValueError("local_hour present but event_type != late_night_driving")
        return self


class ValidationResult(BaseModel):
    valid_count: int
    invalid_count: int
    errors: List[Dict[str, Any]] = Field(default_factory=list)
    sample_valid: List[Dict[str, Any]] = Field(default_factory=list)
    # kinesis forwarding metadata (populated only when stream forwarding attempted)
    kinesis: Optional[Dict[str, Any]] = None


# --------------------------- Handler Logic ------------------------------------


def parse_body(event: Dict[str, Any]) -> Union[List[Dict[str, Any]], Dict[str, Any]]:
    body = event.get("body")
    if body is None:
        raise ValueError("Missing request body")
    if event.get("isBase64Encoded"):
        import base64

        body = base64.b64decode(body).decode()
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:  # noqa: BLE001
        raise ValueError("Body must be valid JSON") from e
    return data


def validate_events(raw: Union[List[Dict[str, Any]], Dict[str, Any]]) -> Tuple[ValidationResult, List[Dict[str, Any]]]:
    if isinstance(raw, dict):
        records = [raw]
    else:
        records = raw
    valid: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    for idx, rec in enumerate(records):
        try:
            model = TelemetryEvent(**rec)
            valid.append(model.model_dump())
        except ValidationError as ve:
            # Use ve.json() to ensure all contents are JSON-serializable
            try:
                parsed_errors = json.loads(ve.json())
            except Exception:  # fallback
                parsed_errors = [err if isinstance(err, dict) else {"msg": str(err)} for err in ve.errors()]  # type: ignore[arg-type]
            errors.append({
                "index": idx,
                "errors": parsed_errors,
                "event_id": rec.get("event_id"),
            })
        except Exception as e:  # noqa: BLE001
            errors.append({"index": idx, "errors": [str(e)], "event_id": rec.get("event_id")})
    return (
        ValidationResult(
            valid_count=len(valid),
            invalid_count=len(errors),
            errors=errors,
            sample_valid=valid[: min(5, len(valid))],
        ),
        valid,
    )


def build_response(status_code: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload),
    }


_kinesis_client = None  # cached between warm invocations


def _get_kinesis_client():  # lazy init to keep cold start minimal
    global _kinesis_client  # noqa: PLW0603
    if _kinesis_client is None and boto3 is not None:
        _kinesis_client = boto3.client("kinesis")
    return _kinesis_client


def _chunk_records(records: List[Dict[str, Any]], max_count: int = 500, max_bytes: int = 5_000_000) -> List[List[Dict[str, Any]]]:
    """Chunk records according to Kinesis PutRecords limits.

    - Up to 500 records per request
    - Request payload <= 5MB
    - Individual record <= 1MB (implicitly enforced by serialization size check)
    """
    batches: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    current_bytes = 0
    for rec in records:
        data = json.dumps(rec, separators=(",", ":")).encode("utf-8")
        size = len(data)
        if size > 1_000_000:
            # Skip oversize record; will be reported as failure later.
            continue
        if current and (len(current) >= max_count or current_bytes + size > max_bytes):
            batches.append(current)
            current = []
            current_bytes = 0
        current.append(rec)
        current_bytes += size
    if current:
        batches.append(current)
    return batches


def forward_to_kinesis(valid_events: List[Dict[str, Any]]) -> Dict[str, Any]:
    stream = os.getenv("KINESIS_STREAM_NAME")
    if not stream or not valid_events:
        return {"enabled": False, "forwarded": 0}
    client = _get_kinesis_client()
    if client is None:
        return {"enabled": False, "error": "boto3 not available"}
    pk_field = os.getenv("KINESIS_PARTITION_KEY_FIELD", "driver_id")
    total = len(valid_events)
    success = 0
    failed: List[Dict[str, Any]] = []
    oversized = 0
    batches = _chunk_records(valid_events)
    for batch in batches:
        entries = []
        for rec in batch:
            try:
                data_bytes = json.dumps(rec).encode("utf-8")
            except Exception:  # pragma: no cover
                oversized += 1
                continue
            if len(data_bytes) > 1_000_000:
                oversized += 1
                continue
            pk_val = str(rec.get(pk_field) or rec.get("event_id") or "default")
            entries.append({"Data": data_bytes, "PartitionKey": pk_val})
        if not entries:
            continue
        try:
            resp = client.put_records(StreamName=stream, Records=entries)
            recs = resp.get("Records", [])
            for i, r in enumerate(recs):
                if "ErrorCode" in r and r["ErrorCode"]:
                    failed.append({"index": i, "error": r["ErrorCode"], "message": r.get("ErrorMessage")})
                else:
                    success += 1
        except (BotoCoreError, ClientError) as e:  # pragma: no cover - network
            # Whole batch failed
            for _ in entries:
                failed.append({"error": type(e).__name__, "message": str(e)})
    return {
        "enabled": True,
        "attempted": total,
        "success": success,
        "failed": len(failed),
        "oversized_skipped": oversized,
        "failure_samples": failed[:5],
    }


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:  # AWS entrypoint
    if event.get("httpMethod") not in (None, "POST"):
        return build_response(405, {"message": "Method Not Allowed"})
    try:
        raw = parse_body(event)
        result, all_valid = validate_events(raw)
        # Forward only valid events (even if partial failure) if stream configured
        kinesis_meta = forward_to_kinesis(all_valid)
        if kinesis_meta.get("enabled"):
            result.kinesis = {k: v for k, v in kinesis_meta.items() if k != "failure_samples" or v}
        status = 200 if result.invalid_count == 0 else 207
        payload = result.model_dump(exclude_none=True)
        payload["message"] = "OK" if status == 200 else "Partial Failure"
        if kinesis_meta.get("enabled") and kinesis_meta.get("failed"):
            # elevate to 207 if Kinesis forwarding had failures
            status = 207 if status == 200 else status
        return build_response(status, payload)
    except ValueError as ve:
        return build_response(400, {"message": str(ve)})
    except Exception as e:  # noqa: BLE001
        return build_response(500, {"message": "Internal Error", "detail": str(e)})


# Local test helper
if __name__ == "__main__":  # pragma: no cover
    sample = [
        {
            "event_id": "abcdef0123456789abcdef0123456789",
            "driver_id": "D0001",
            "trip_id": "T-12345",
            "ts": "2025-01-01T12:00:00.000Z",
            "event_type": "hard_braking",
            "latitude": 37.77,
            "longitude": -122.41,
            "speed_mph": 42.5,
            "heading_deg": 123,
            "period_minute": 15,
            "braking_g": 0.55,
        },
        {
            "event_id": "badid",
            "driver_id": "D0002",
            "trip_id": "T-abc",
            "ts": "not-a-time",
            "event_type": "unknown",
            "latitude": 37.0,
            "longitude": -122.0,
            "speed_mph": 10,
            "heading_deg": 10,
            "period_minute": 1,
        },
    ]
    fake_event = {"httpMethod": "POST", "body": json.dumps(sample)}
    out = lambda_handler(fake_event, None)
    print(out)
