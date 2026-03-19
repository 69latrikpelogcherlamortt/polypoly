"""
Polymarket-specific price impact and timing models.

Provides binary-market-aware price impact estimation, optimal limit price
computation, and entry timing analysis tuned to Polymarket dynamics.
"""

import math
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import List

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Price impact
# ---------------------------------------------------------------------------

def binary_market_price_impact(
    order_size_eur: float,
    market_volume_24h: float,
    current_price: float,
    lambda_impact: float = 0.001,
) -> float:
    """Estimate price impact for a binary-outcome market on Polymarket.

    Formula:
        impact = lambda * sqrt(participation_rate) * price_amplifier
    where
        participation_rate = order_size_eur / max(market_volume_24h, 1)
        price_amplifier    = 1 / (4*p*(1-p) + 0.1)

    The amplifier grows near p=0 and p=1, reflecting thinner liquidity at
    extreme prices.  Result is clipped to [0, 0.02].
    """
    participation_rate = order_size_eur / max(market_volume_24h, 1.0)
    price_amplifier = 1.0 / (4.0 * current_price * (1.0 - current_price) + 0.1)
    impact = lambda_impact * math.sqrt(participation_rate) * price_amplifier
    return max(0.0, min(impact, 0.02))


# ---------------------------------------------------------------------------
# Optimal limit price
# ---------------------------------------------------------------------------

def compute_optimal_limit_price(
    side: str,
    best_bid: float,
    best_ask: float,
    order_size_eur: float,
    market_volume_24h: float,
    urgency: float = 0.5,
) -> float:
    """Compute an optimal limit price given urgency and estimated impact.

    For a BUY order the price is interpolated between *best_bid* and
    *best_ask* proportionally to *urgency* (0 = passive at bid, 1 = cross
    the spread), then adjusted upward by the estimated price impact.
    The result is clipped to [best_bid, best_ask + impact].

    For a SELL order the logic is mirrored: interpolation goes from
    best_ask down toward best_bid, adjusted downward by impact, and
    clipped to [best_bid - impact, best_ask].
    """
    mid = (best_bid + best_ask) / 2.0
    impact = binary_market_price_impact(
        order_size_eur, market_volume_24h, mid,
    )

    side_upper = side.strip().upper()

    if side_upper == "BUY":
        base = best_bid + urgency * (best_ask - best_bid)
        price = base + impact
        return max(best_bid, min(price, best_ask + impact))

    elif side_upper == "SELL":
        base = best_ask - urgency * (best_ask - best_bid)
        price = base - impact
        return max(best_bid - impact, min(price, best_ask))

    else:
        raise ValueError(f"Unknown side: {side!r}. Expected 'BUY' or 'SELL'.")


# ---------------------------------------------------------------------------
# Entry timing analysis
# ---------------------------------------------------------------------------

class EntryTimingAnalyzer:
    """Determines whether *now* is a good moment to enter a position.

    Scoring rules (multiplicative penalties applied to a base score of 1.0):

    * **Days to resolution:**
      - J-1 (<=1 day)  -> score *= 0.3
      - J-3 (<=3 days) -> score *= 0.7
    * **High volatility:** 24h vol > 15 % -> score *= 0.5
    * **Extreme price:** price < 0.05 or price > 0.95 -> score *= 0.4
    * **Off-peak hours:** hour_utc in [2, 8) -> score *= 0.8
    """

    ENTRY_THRESHOLD: float = 0.5

    def analyze_entry_timing(
        self,
        days_to_resolution: float,
        current_price: float,
        price_volatility_24h: float,
        hour_utc: int,
    ) -> dict:
        """Return a timing assessment dict.

        Keys:
            timing_score  – float in [0, 1]
            should_enter  – bool (True when score >= ENTRY_THRESHOLD)
            reasons       – list[str] explaining applied penalties
        """
        score: float = 1.0
        reasons: List[str] = []

        # --- Days to resolution ---
        if days_to_resolution <= 1.0:
            score *= 0.3
            reasons.append(
                f"Very close to resolution ({days_to_resolution:.1f}d): score *0.3"
            )
        elif days_to_resolution <= 3.0:
            score *= 0.7
            reasons.append(
                f"Near resolution ({days_to_resolution:.1f}d): score *0.7"
            )

        # --- Volatility ---
        if price_volatility_24h > 0.15:
            score *= 0.5
            reasons.append(
                f"High 24h volatility ({price_volatility_24h:.1%}): score *0.5"
            )

        # --- Extreme price ---
        if current_price < 0.05 or current_price > 0.95:
            score *= 0.4
            reasons.append(
                f"Extreme price ({current_price:.3f}): score *0.4"
            )

        # --- Off-peak hours ---
        if 2 <= hour_utc < 8:
            score *= 0.8
            reasons.append(
                f"Off-peak hour (UTC {hour_utc}): score *0.8"
            )

        # Clip to [0, 1]
        score = max(0.0, min(score, 1.0))

        should_enter = score >= self.ENTRY_THRESHOLD

        if not reasons:
            reasons.append("No adverse conditions detected")

        logger.debug(
            "Entry timing: score=%.3f enter=%s reasons=%s",
            score, should_enter, reasons,
        )

        return {
            "timing_score": score,
            "should_enter": should_enter,
            "reasons": reasons,
        }
