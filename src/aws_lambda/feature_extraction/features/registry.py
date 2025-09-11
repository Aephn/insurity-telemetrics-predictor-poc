from __future__ import annotations
from typing import List
from .core import (
    ExposureMiles,
    CountPer100Mi,
    TailgatingRatio,
    SpeedingMinutesPer100Mi,
    LateNightMilesPer100Mi,
    PriorClaimsPlaceholder,
)


def load_feature_calculators() -> List[object]:
    """Instantiate and return ordered feature calculators.

    ExposureMiles must appear first (others rely on shared exposure_miles / total_event_minutes).
    """
    return [
        ExposureMiles(),
        CountPer100Mi("hard_braking", "hard_braking_events_per_100mi"),
        CountPer100Mi("aggressive_turn", "aggressive_turning_events_per_100mi"),
        TailgatingRatio(),
        SpeedingMinutesPer100Mi(),
        LateNightMilesPer100Mi(),
        PriorClaimsPlaceholder(),
    ]
