"""Tests for walk-forward backtesting and go-live checker."""
import pytest
from backtesting.walk_forward import WalkForwardResult, WalkForwardReport, GoLiveChecker


class TestWalkForwardResult:

    def test_sharpe_with_trades(self):
        trades = [
            {"pnl_pct": 0.05, "status": "CLOSED"},
            {"pnl_pct": 0.03, "status": "CLOSED"},
            {"pnl_pct": -0.02, "status": "CLOSED"},
            {"pnl_pct": 0.04, "status": "CLOSED"},
            {"pnl_pct": 0.01, "status": "CLOSED"},
        ]
        w = WalkForwardResult(0, "2024-01", "2024-03", "2024-04", "2024-04", trades)
        assert w.sharpe > 0
        assert w.n_trades == 5

    def test_sharpe_insufficient_data(self):
        w = WalkForwardResult(0, "a", "b", "c", "d", [{"pnl_pct": 0.1, "status": "CLOSED"}])
        assert w.sharpe == 0.0

    def test_profit_factor(self):
        trades = [
            {"pnl": 10.0, "pnl_pct": 0.10, "status": "CLOSED"},
            {"pnl": -3.0, "pnl_pct": -0.03, "status": "CLOSED"},
        ]
        w = WalkForwardResult(0, "a", "b", "c", "d", trades)
        assert w.profit_factor == pytest.approx(10.0 / 3.0)

    def test_brier_score(self):
        trades = [
            {"p_model": 0.80, "outcome": 1, "status": "CLOSED"},
            {"p_model": 0.20, "outcome": 0, "status": "CLOSED"},
        ]
        w = WalkForwardResult(0, "a", "b", "c", "d", trades)
        expected = ((0.80 - 1)**2 + (0.20 - 0)**2) / 2
        assert w.brier_score == pytest.approx(expected)


class TestWalkForwardReport:

    def _make_window(self, sharpe, pf, brier):
        """Helper to create a mock window with known metrics."""
        trades = [
            {"pnl_pct": sharpe * 0.01, "status": "CLOSED", "pnl": pf,
             "p_model": 0.5 + (0.5 - brier**0.5), "outcome": 1}
            for _ in range(10)
        ]
        return WalkForwardResult(0, "a", "b", "c", "d", trades)

    def test_viable_strategy(self):
        # Create windows with high sharpe
        windows = []
        for _ in range(8):
            trades = [
                {"pnl_pct": 0.08, "status": "CLOSED", "pnl": 5.0,
                 "p_model": 0.85, "outcome": 1}
                for _ in range(10)
            ]
            trades.append({"pnl_pct": -0.02, "status": "CLOSED", "pnl": -1.0,
                          "p_model": 0.15, "outcome": 0})
            windows.append(WalkForwardResult(0, "a", "b", "c", "d", trades))

        report = WalkForwardReport(windows=windows)
        # Check properties don't crash
        assert report.mean_sharpe is not None
        assert report.mean_profit_factor is not None
        assert report.mean_brier is not None


class TestGoLiveChecker:

    def test_all_conditions_met(self):
        checker = GoLiveChecker()
        ready, blockers = checker.check(
            paper_days=20, paper_trades=30,
            brier=0.18, sharpe=1.5, pf=1.5,
            walkforward_viable=True,
        )
        assert ready
        assert len(blockers) == 0

    def test_insufficient_paper_days(self):
        checker = GoLiveChecker()
        ready, blockers = checker.check(
            paper_days=5, paper_trades=30,
            brier=0.18, sharpe=1.5, pf=1.5,
            walkforward_viable=True,
        )
        assert not ready
        assert any("paper_trading_days" in b.lower() or "days" in b.lower() for b in blockers)

    def test_brier_too_high(self):
        checker = GoLiveChecker()
        ready, blockers = checker.check(
            paper_days=20, paper_trades=30,
            brier=0.25, sharpe=1.5, pf=1.5,
            walkforward_viable=True,
        )
        assert not ready
        assert any("brier" in b.lower() for b in blockers)

    def test_walkforward_not_viable(self):
        checker = GoLiveChecker()
        ready, blockers = checker.check(
            paper_days=20, paper_trades=30,
            brier=0.18, sharpe=1.5, pf=1.5,
            walkforward_viable=False,
        )
        assert not ready
        assert any("walk" in b.lower() for b in blockers)

    def test_multiple_blockers(self):
        checker = GoLiveChecker()
        ready, blockers = checker.check(
            paper_days=3, paper_trades=5,
            brier=0.30, sharpe=0.5, pf=0.8,
            walkforward_viable=False,
        )
        assert not ready
        assert len(blockers) >= 3
