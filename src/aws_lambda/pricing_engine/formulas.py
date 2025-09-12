from __future__ import annotations

from typing import Dict, Any, List, Tuple
import math


def tier(value: float, bounds: List[Tuple[float, float, float]]) -> float:
    """Return adjustment for value given list of (min_inclusive, max_exclusive, adj).

    Bounds should cover the domain or a default of 0.0 is returned.
    """
    for lo, hi, adj in bounds:
        if (value >= lo) and (value < hi):
            return adj
    return 0.0


def compute_behavior_adjustments(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Compute small additive adjustments (percent deltas) based on raw feature tiers.

    These are lightweight relative adjustments layered *after* the model multiplier
    to introduce transparent business rules and guardrail extreme behaviors.
    """

    metrics = []

    hard_braking = float(row.get("hard_braking_events_per_100mi", 0.0) or 0.0)
    hb_adj = tier(
        hard_braking,
        [
            (0, 2, -0.02),
            (2, 4, 0.0),
            (4, 6, 0.02),
            (6, math.inf, 0.05),
        ],
    )
    metrics.append(
        {"metric": "hard_braking_events_per_100mi", "value": hard_braking, "adj": hb_adj}
    )

    ag_turn = float(row.get("aggressive_turning_events_per_100mi", 0.0) or 0.0)
    # Keep turning implicit via model only (adj 0) but include for transparency
    metrics.append({"metric": "aggressive_turning_events_per_100mi", "value": ag_turn, "adj": 0.0})

    tail_ratio = float(row.get("tailgating_time_ratio", 0.0) or 0.0)
    tg_adj = tier(
        tail_ratio,
        [
            (0, 0.05, -0.01),
            (0.05, 0.15, 0.0),
            (0.15, 0.25, 0.02),
            (0.25, math.inf, 0.04),
        ],
    )
    metrics.append({"metric": "tailgating_time_ratio", "value": tail_ratio, "adj": tg_adj})

    speeding_min = float(row.get("speeding_minutes_per_100mi", 0.0) or 0.0)
    sp_adj = tier(
        speeding_min,
        [
            (0, 3, -0.01),
            (3, 7, 0.0),
            (7, 12, 0.02),
            (12, math.inf, 0.05),
        ],
    )
    metrics.append({"metric": "speeding_minutes_per_100mi", "value": speeding_min, "adj": sp_adj})

    ln_miles = float(row.get("late_night_miles_per_100mi", 0.0) or 0.0)
    ln_adj = tier(
        ln_miles,
        [
            (0, 1, 0.0),
            (1, 4, 0.01),
            (4, 8, 0.02),
            (8, math.inf, 0.04),
        ],
    )
    metrics.append({"metric": "late_night_miles_per_100mi", "value": ln_miles, "adj": ln_adj})

    prior_claims = float(row.get("prior_claim_count", 0.0) or 0.0)
    # Stronger per-claim impact: +12% each up to +36%
    claim_adj = min(0.12 * prior_claims, 0.36)
    metrics.append({"metric": "prior_claim_count", "value": prior_claims, "adj": claim_adj})

    # Car value tier adjustments (severity proxy) applied additively
    car_val_raw = float(row.get("car_value_raw") or row.get("car_value") or 0.0)
    if car_val_raw > 0:
        cv_adj = tier(
            car_val_raw,
            [
                (0, 20000, -0.02),
                (20000, 35000, 0.0),
                (35000, 60000, 0.03),
                (60000, 90000, 0.06),
                (90000, 130000, 0.09),
                (130000, float("inf"), 0.12),
            ],
        )
        metrics.append({"metric": "car_value_raw", "value": car_val_raw, "adj": cv_adj})

    miles = float(row.get("miles", 0.0) or 0.0)
    miles_adj = 0.0
    if miles < 500:
        miles_adj = -0.03
    elif miles > 1100:
        miles_adj = 0.03
    metrics.append({"metric": "miles", "value": miles, "adj": miles_adj})

    return metrics


def finalize_multiplier(
    model_multiplier: float, adjustments: List[Dict[str, Any]], min_factor: float, max_factor: float
) -> Dict[str, Any]:
    behavior_sum = sum(m["adj"] for m in adjustments)
    # Combine multiplicatively: model multiplier then additive behavior adjustments on top.
    combined = model_multiplier * (1 + behavior_sum)
    bounded = max(min_factor, min(max_factor, combined))
    return {
        "model_multiplier": model_multiplier,
        "behavior_adjustment_sum": round(behavior_sum, 6),
        "unbounded_multiplier": round(combined, 6),
        "final_multiplier": round(bounded, 6),
    }


def compute_price(
    base_premium: float, final_multiplier: float, min_premium: float, max_premium: float
) -> Dict[str, Any]:
    raw = base_premium * final_multiplier
    bounded = max(min_premium, min(max_premium, raw))
    return {
        "raw_premium": round(raw, 2),
        "final_monthly_premium": round(bounded, 2),
    }
