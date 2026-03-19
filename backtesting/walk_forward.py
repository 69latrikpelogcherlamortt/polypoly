from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np

log = logging.getLogger("backtesting")


# ---------------------------------------------------------------------------
# WalkForwardResult
# ---------------------------------------------------------------------------
@dataclass
class WalkForwardResult:
    """Metrics for a single walk-forward window."""

    window_id: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    trades: list[dict] = field(default_factory=list)

    # -- derived properties --------------------------------------------------

    @property
    def n_trades(self) -> int:
        return len(self.trades)

    @property
    def sharpe(self) -> float:
        """Annualised Sharpe ratio from trades' *pnl_pct*."""
        pnls = [t["pnl_pct"] for t in self.trades if "pnl_pct" in t]
        if len(pnls) < 2:
            return 0.0
        arr = np.array(pnls, dtype=float)
        std = float(np.std(arr, ddof=1))
        if std == 0.0:
            return 0.0
        return float(np.mean(arr) / std * np.sqrt(252))

    @property
    def profit_factor(self) -> float:
        """Sum of winning pnl_pct / sum of absolute losing pnl_pct."""
        pnls = [t["pnl_pct"] for t in self.trades if "pnl_pct" in t]
        if not pnls:
            return 0.0
        wins = sum(p for p in pnls if p > 0)
        losses = sum(abs(p) for p in pnls if p < 0)
        if losses == 0.0:
            return float("inf") if wins > 0 else 0.0
        return wins / losses

    @property
    def brier_score(self) -> float:
        """Mean (p_model - outcome)^2 for resolved trades."""
        resolved = [
            t for t in self.trades
            if "p_model" in t and "outcome" in t
        ]
        if not resolved:
            return 0.0
        scores = [(t["p_model"] - t["outcome"]) ** 2 for t in resolved]
        return float(np.mean(scores))


# ---------------------------------------------------------------------------
# WalkForwardReport
# ---------------------------------------------------------------------------
@dataclass
class WalkForwardReport:
    """Aggregated report across all walk-forward windows."""

    windows: list[WalkForwardResult] = field(default_factory=list)

    # -- aggregate properties ------------------------------------------------

    @property
    def mean_sharpe(self) -> float:
        if not self.windows:
            return 0.0
        return float(np.mean([w.sharpe for w in self.windows]))

    @property
    def mean_profit_factor(self) -> float:
        if not self.windows:
            return 0.0
        pfs = [w.profit_factor for w in self.windows if w.profit_factor != float("inf")]
        return float(np.mean(pfs)) if pfs else 0.0

    @property
    def mean_brier(self) -> float:
        if not self.windows:
            return 0.0
        return float(np.mean([w.brier_score for w in self.windows]))

    @property
    def is_strategy_viable(self) -> bool:
        if not self.windows:
            return False
        positive_sharpe_pct = sum(1 for w in self.windows if w.sharpe > 0) / len(self.windows)
        return (
            self.mean_sharpe > 1.5
            and self.mean_profit_factor > 1.3
            and self.mean_brier < 0.22
            and positive_sharpe_pct >= 0.75
        )

    # -- display -------------------------------------------------------------

    def print_report(self) -> None:
        """Print a formatted summary table to stdout."""
        header = (
            f"{'Win':>4} | {'Train':^23} | {'Test':^23} | "
            f"{'#Tr':>4} | {'Sharpe':>7} | {'PF':>7} | {'Brier':>6}"
        )
        sep = "-" * len(header)
        print(sep)
        print(header)
        print(sep)
        for w in self.windows:
            train_range = f"{w.train_start} -> {w.train_end}"
            test_range = f"{w.test_start} -> {w.test_end}"
            pf_str = "inf" if w.profit_factor == float("inf") else f"{w.profit_factor:.3f}"
            print(
                f"{w.window_id:>4} | {train_range:^23} | {test_range:^23} | "
                f"{w.n_trades:>4} | {w.sharpe:>7.3f} | {pf_str:>7} | {w.brier_score:>6.4f}"
            )
        print(sep)
        print(
            f"Mean Sharpe: {self.mean_sharpe:.3f}  |  "
            f"Mean PF: {self.mean_profit_factor:.3f}  |  "
            f"Mean Brier: {self.mean_brier:.4f}"
        )
        print(f"Strategy viable: {self.is_strategy_viable}")
        print(sep)


# ---------------------------------------------------------------------------
# GoLiveChecker
# ---------------------------------------------------------------------------
class GoLiveChecker:
    """Gate-check before transitioning from paper to live trading."""

    REQUIREMENTS: dict[str, Any] = {
        "paper_trading_days": 14,
        "paper_trades_minimum": 20,
        "brier_max": 0.20,
        "sharpe_min": 1.0,
        "profit_factor_min": 1.2,
    }

    def check(
        self,
        paper_days: int,
        paper_trades: int,
        brier: float,
        sharpe: float,
        pf: float,
        walkforward_viable: bool,
    ) -> tuple[bool, list[str]]:
        """Return *(ready, blockers)* – ready is True only when all gates pass."""
        blockers: list[str] = []
        req = self.REQUIREMENTS

        if paper_days < req["paper_trading_days"]:
            blockers.append(
                f"Paper trading days ({paper_days}) < required ({req['paper_trading_days']})"
            )
        if paper_trades < req["paper_trades_minimum"]:
            blockers.append(
                f"Paper trades ({paper_trades}) < required ({req['paper_trades_minimum']})"
            )
        if brier > req["brier_max"]:
            blockers.append(
                f"Brier score ({brier:.4f}) > max allowed ({req['brier_max']})"
            )
        if sharpe < req["sharpe_min"]:
            blockers.append(
                f"Sharpe ({sharpe:.3f}) < required ({req['sharpe_min']})"
            )
        if pf < req["profit_factor_min"]:
            blockers.append(
                f"Profit factor ({pf:.3f}) < required ({req['profit_factor_min']})"
            )
        if not walkforward_viable:
            blockers.append("Walk-forward analysis indicates strategy is NOT viable")

        ready = len(blockers) == 0
        if ready:
            log.info("Go-live check PASSED — all gates clear.")
        else:
            log.warning("Go-live check FAILED — %d blocker(s).", len(blockers))
            for b in blockers:
                log.warning("  • %s", b)

        return ready, blockers
