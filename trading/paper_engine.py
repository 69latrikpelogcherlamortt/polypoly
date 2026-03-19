"""
paper_engine.py  ·  PAF-001 Paper Trading Engine
─────────────────────────────────────────────────
Simulates execution with real Polymarket prices.
Unlike DRY_RUN (which just logs), PaperEngine simulates fills
following real price evolution with realistic slippage.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("paper_engine")


@dataclass
class PaperPosition:
    market_id: str
    question: str
    side: str              # "YES" / "NO"
    entry_price: float
    size_eur: float
    n_shares: float
    entry_ts: str
    current_price: float = 0.0

    @property
    def unrealized_pnl(self) -> float:
        return self.n_shares * self.current_price - self.size_eur


@dataclass
class PaperTradeResult:
    type: str              # "PAPER_OPEN" / "PAPER_CLOSE"
    market_id: str
    side: str
    price: float
    size_eur: float
    n_shares: float
    commission: float
    slippage_estimate: float
    pnl: Optional[float] = None


class PaperTradeEngine:
    """
    Simulates execution following real Polymarket prices.
    Applies commission (0.2%) and estimated slippage.
    """

    COMMISSION_PCT = 0.002  # 0.2% per trade

    def __init__(self, initial_bankroll: float):
        self.bankroll = initial_bankroll
        self.initial_bankroll = initial_bankroll
        self.positions: dict[str, PaperPosition] = {}
        self.closed_trades: list[PaperTradeResult] = []

    def simulate_open(
        self,
        market_id: str,
        question: str,
        size_eur: float,
        market_price: float,
        slippage_estimate: float = 0.003,
    ) -> PaperTradeResult:
        """Simulate opening a position with commission + slippage."""
        commission = size_eur * self.COMMISSION_PCT
        fill_price = market_price + slippage_estimate
        fill_price = min(fill_price, 0.99)
        actual_eur = size_eur - commission
        n_shares = actual_eur / max(fill_price, 0.001)

        pos = PaperPosition(
            market_id=market_id,
            question=question,
            side="YES",
            entry_price=fill_price,
            size_eur=size_eur,
            n_shares=n_shares,
            entry_ts=datetime.now(timezone.utc).isoformat(),
            current_price=market_price,
        )
        self.positions[market_id] = pos
        self.bankroll -= size_eur

        result = PaperTradeResult(
            type="PAPER_OPEN", market_id=market_id, side="YES",
            price=fill_price, size_eur=size_eur, n_shares=n_shares,
            commission=commission, slippage_estimate=slippage_estimate,
        )
        log.info(
            "PAPER OPEN: %s %.2f€ @ %.4f (comm: %.4f€, slip: %.4f)",
            market_id[:20], size_eur, fill_price, commission, slippage_estimate
        )
        return result

    def simulate_close(
        self,
        market_id: str,
        current_price: float,
        reason: str = "resolution",
    ) -> Optional[PaperTradeResult]:
        """Simulate closing a position."""
        pos = self.positions.pop(market_id, None)
        if pos is None:
            return None

        received = pos.n_shares * current_price
        commission = received * self.COMMISSION_PCT
        net_received = received - commission
        pnl = net_received - pos.size_eur

        self.bankroll += net_received

        result = PaperTradeResult(
            type="PAPER_CLOSE", market_id=market_id, side="SELL",
            price=current_price, size_eur=net_received, n_shares=pos.n_shares,
            commission=commission, slippage_estimate=0.0, pnl=pnl,
        )
        self.closed_trades.append(result)
        log.info(
            "PAPER CLOSE: %s PnL=%+.2f€ (reason: %s)",
            market_id[:20], pnl, reason
        )
        return result

    def update_prices(self, live_prices: dict[str, float]) -> None:
        """Update current prices for all open positions."""
        for mid, pos in self.positions.items():
            if mid in live_prices:
                pos.current_price = live_prices[mid]

    def get_performance(self) -> dict:
        """Real-time paper trading performance metrics."""
        unrealized = sum(p.unrealized_pnl for p in self.positions.values())
        realized = sum(t.pnl for t in self.closed_trades if t.pnl is not None)
        return {
            "bankroll": round(self.bankroll, 2),
            "initial_bankroll": self.initial_bankroll,
            "open_positions": len(self.positions),
            "closed_trades": len(self.closed_trades),
            "unrealized_pnl": round(unrealized, 4),
            "realized_pnl": round(realized, 4),
            "total_pnl": round(unrealized + realized, 4),
            "return_pct": round((self.bankroll + unrealized - self.initial_bankroll)
                                / self.initial_bankroll * 100, 2),
        }
