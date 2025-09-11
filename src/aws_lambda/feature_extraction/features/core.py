from __future__ import annotations
from typing import Dict, Any
from .base import BaseFeatureCalculator


class ExposureMiles(BaseFeatureCalculator):
    """Accumulate exposure miles approximated from speed.

    Assumption: Each event approximates one time slice (minute) for a driver.
    Miles ~= speed_mph / 60.
    """

    name = "exposure_miles"

    def init_state(self) -> Dict[str, Any]:
        return {"miles": 0.0, "event_minutes": 0}

    def update(self, state: Dict[str, Any], event: Dict[str, Any]) -> None:
        spd = event.get("speed_mph")
        if isinstance(spd, (int, float)):
            state["miles"] += float(spd) / 60.0
        state["event_minutes"] += 1

    def finalize(self, state: Dict[str, Any], shared: Dict[str, Any]) -> Dict[str, Any]:
        shared["exposure_miles"] = state.get("miles", 0.0)
        shared["total_event_minutes"] = state.get("event_minutes", 0)
        return {"miles": round(state.get("miles", 0.0), 2)}


class CountPer100Mi(BaseFeatureCalculator):
    """Generic counter for specific event_type normalized by exposure miles."""

    def __init__(self, event_type: str, feature_name: str):
        self.event_type = event_type
        self.name = feature_name

    def init_state(self) -> Dict[str, Any]:
        return {"count": 0}

    def update(self, state: Dict[str, Any], event: Dict[str, Any]) -> None:
        if event.get("event_type") == self.event_type:
            state["count"] += 1

    def finalize(self, state: Dict[str, Any], shared: Dict[str, Any]) -> Dict[str, Any]:
        miles = shared.get("exposure_miles", 0.0)
        if miles <= 0:
            value = 0.0
        else:
            value = 100.0 * state["count"] / miles
        return {self.name: round(value, 4)}


class TailgatingRatio(BaseFeatureCalculator):
    """Proportion of tailgating events vs total event minutes."""

    name = "tailgating_time_ratio"

    def init_state(self) -> Dict[str, Any]:
        return {"tailgating": 0}

    def update(self, state: Dict[str, Any], event: Dict[str, Any]) -> None:
        if event.get("event_type") == "tailgating":
            state["tailgating"] += 1

    def finalize(self, state: Dict[str, Any], shared: Dict[str, Any]) -> Dict[str, Any]:
        total = shared.get("total_event_minutes", 0)
        if total <= 0:
            ratio = 0.0
        else:
            ratio = state["tailgating"] / total
        return {self.name: round(ratio, 4)}


class SpeedingMinutesPer100Mi(BaseFeatureCalculator):
    name = "speeding_minutes_per_100mi"

    def init_state(self) -> Dict[str, Any]:
        return {"speeding_minutes": 0.0}

    def update(self, state: Dict[str, Any], event: Dict[str, Any]) -> None:
        if event.get("event_type") == "speeding":
            dur = event.get("duration_sec")
            if isinstance(dur, (int, float)):
                state["speeding_minutes"] += float(dur) / 60.0
            else:
                state["speeding_minutes"] += 1.0 / 60.0  # fallback tiny slice

    def finalize(self, state: Dict[str, Any], shared: Dict[str, Any]) -> Dict[str, Any]:
        miles = shared.get("exposure_miles", 0.0)
        if miles <= 0:
            val = 0.0
        else:
            val = 100.0 * state["speeding_minutes"] / miles
        return {self.name: round(val, 4)}


class LateNightMilesPer100Mi(BaseFeatureCalculator):
    name = "late_night_miles_per_100mi"

    def init_state(self) -> Dict[str, Any]:
        return {"ln_miles": 0.0}

    def update(self, state: Dict[str, Any], event: Dict[str, Any]) -> None:
        if event.get("event_type") == "late_night_driving":
            spd = event.get("speed_mph")
            if isinstance(spd, (int, float)):
                state["ln_miles"] += float(spd) / 60.0

    def finalize(self, state: Dict[str, Any], shared: Dict[str, Any]) -> Dict[str, Any]:
        miles = shared.get("exposure_miles", 0.0)
        if miles <= 0:
            val = 0.0
        else:
            val = 100.0 * state["ln_miles"] / miles
        return {self.name: round(val, 4)}


class PriorClaimsPlaceholder(BaseFeatureCalculator):
    """Placeholder for prior claim count (external source integration later)."""

    name = "prior_claim_count"

    def init_state(self) -> Dict[str, Any]:  # no state needed
        return {}

    def update(self, state: Dict[str, Any], event: Dict[str, Any]) -> None:
        return

    def finalize(self, state: Dict[str, Any], shared: Dict[str, Any]) -> Dict[str, Any]:
        return {self.name: 0}
