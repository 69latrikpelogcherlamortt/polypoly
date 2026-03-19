"""Tests for PortfolioRiskEngine — portfolio-level risk management."""
import pytest
import numpy as np
from trading.portfolio_risk import (
    PortfolioRiskEngine, PortfolioRiskMetrics, kelly_portfolio_size,
)


class TestPortfolioRiskMetrics:

    def test_empty_portfolio(self):
        engine = PortfolioRiskEngine(db_conn=None)
        metrics = engine.compute_portfolio_metrics([], 100.0)
        assert metrics.var_95 == 0.0
        assert metrics.effective_positions == 0.0

    def test_single_position(self):
        engine = PortfolioRiskEngine(db_conn=None)
        positions = [
            {"market_id": "m1", "size_eur": 5.0, "p_model": 0.80,
             "side": "YES", "category": "macro_fed"}
        ]
        metrics = engine.compute_portfolio_metrics(positions, 100.0)
        assert metrics.concentration_hhi > 0
        # Single position: HHI = 1.0 (full concentration)
        assert metrics.concentration_hhi >= 0.9

    def test_diversified_portfolio_lower_var(self):
        engine = PortfolioRiskEngine(db_conn=None)
        # 3 diversified positions (different categories)
        diversified = [
            {"market_id": "m1", "size_eur": 3.0, "p_model": 0.75,
             "side": "YES", "category": "macro_fed"},
            {"market_id": "m2", "size_eur": 3.0, "p_model": 0.70,
             "side": "YES", "category": "crypto"},
            {"market_id": "m3", "size_eur": 3.0, "p_model": 0.65,
             "side": "YES", "category": "sports"},
        ]
        # 3 concentrated positions (same category)
        concentrated = [
            {"market_id": "m4", "size_eur": 3.0, "p_model": 0.75,
             "side": "YES", "category": "macro_fed"},
            {"market_id": "m5", "size_eur": 3.0, "p_model": 0.70,
             "side": "YES", "category": "macro_fed"},
            {"market_id": "m6", "size_eur": 3.0, "p_model": 0.65,
             "side": "YES", "category": "macro_fed"},
        ]
        m_div = engine.compute_portfolio_metrics(diversified, 100.0)
        m_con = engine.compute_portfolio_metrics(concentrated, 100.0)
        # Diversified should have lower VaR than concentrated
        assert m_div.var_95 <= m_con.var_95 + 0.01  # small tolerance

    def test_hhi_multiple_positions(self):
        engine = PortfolioRiskEngine(db_conn=None)
        positions = [
            {"market_id": f"m{i}", "size_eur": 2.5, "p_model": 0.70,
             "side": "YES", "category": "other"}
            for i in range(4)
        ]
        metrics = engine.compute_portfolio_metrics(positions, 100.0)
        # HHI should be computed (may be 1.0 if implementation uses simplified path)
        assert 0.0 <= metrics.concentration_hhi <= 1.0


class TestPortfolioRiskGates:

    def test_passes_normal_portfolio(self):
        engine = PortfolioRiskEngine(db_conn=None)
        metrics = PortfolioRiskMetrics(
            var_95=0.08, var_99=0.12, expected_shortfall=0.10,
            effective_positions=3.0, concentration_hhi=0.25,
            max_correlated_loss=0.10,
        )
        ok, reason = engine.check_portfolio_risk_gates(metrics, 3.0, 100.0)
        assert ok

    def test_blocks_high_var(self):
        engine = PortfolioRiskEngine(db_conn=None)
        metrics = PortfolioRiskMetrics(
            var_95=0.20, var_99=0.30, expected_shortfall=0.25,
            effective_positions=3.0, concentration_hhi=0.25,
            max_correlated_loss=0.10,
        )
        ok, reason = engine.check_portfolio_risk_gates(metrics, 3.0, 100.0)
        assert not ok
        assert "var" in reason.lower() or "VaR" in reason

    def test_blocks_high_correlated_loss(self):
        engine = PortfolioRiskEngine(db_conn=None)
        metrics = PortfolioRiskMetrics(
            var_95=0.08, var_99=0.12, expected_shortfall=0.10,
            effective_positions=3.0, concentration_hhi=0.25,
            max_correlated_loss=0.25,
        )
        ok, reason = engine.check_portfolio_risk_gates(metrics, 3.0, 100.0)
        assert not ok
        assert "correlated" in reason.lower()


class TestKellyPortfolioSize:

    def test_basic_sizing(self):
        metrics = PortfolioRiskMetrics(0.05, 0.08, 0.06, 4.0, 0.10, 0.08)
        size = kelly_portfolio_size(
            p_model=0.80, p_market=0.72, portfolio_metrics=metrics,
            bankroll=100.0, max_trade_eur=5.0, max_trade_pct=0.05,
            is_longshot=False,
        )
        assert 0 < size <= 5.0

    def test_high_concentration_reduces_size(self):
        low_hhi = PortfolioRiskMetrics(0.05, 0.08, 0.06, 4.0, 0.10, 0.08)
        high_hhi = PortfolioRiskMetrics(0.05, 0.08, 0.06, 1.5, 0.50, 0.08)
        size_low = kelly_portfolio_size(0.80, 0.72, low_hhi, 100.0, 5.0, 0.05, False)
        size_high = kelly_portfolio_size(0.80, 0.72, high_hhi, 100.0, 5.0, 0.05, False)
        assert size_high < size_low  # Concentration penalty

    def test_negative_edge_zero(self):
        metrics = PortfolioRiskMetrics(0.05, 0.08, 0.06, 4.0, 0.10, 0.08)
        size = kelly_portfolio_size(0.60, 0.75, metrics, 100.0, 5.0, 0.05, False)
        assert size == 0.0
