"""Tests for SizingEngine — Kelly fractional sizing."""
import math
import pytest
from trading.risk_manager import SizingEngine


class TestKellySize:

    def setup_method(self):
        self.engine = SizingEngine()

    def test_favori_sizing_standard(self):
        size = self.engine.kelly_size(
            p_model=0.80, market_price=0.72, bankroll=100.0, is_longshot=False
        )
        assert 0 < size <= 5.0

    def test_longshot_sizing_smaller(self):
        size_l = self.engine.kelly_size(0.12, 0.05, 100.0, is_longshot=True)
        size_f = self.engine.kelly_size(0.80, 0.72, 100.0, is_longshot=False)
        assert size_l < size_f

    def test_negative_edge_returns_zero(self):
        size = self.engine.kelly_size(0.60, 0.75, 100.0, False)
        assert size == 0.0

    def test_max_trade_cap(self):
        size = self.engine.kelly_size(0.99, 0.01, 1000.0, False)
        assert size <= 5.0

    def test_max_pct_bankroll(self):
        size = self.engine.kelly_size(0.85, 0.70, 50.0, False)
        assert size <= 50.0 * 0.05

    def test_bankroll_zero(self):
        size = self.engine.kelly_size(0.80, 0.70, 0.0, False)
        assert size == 0.0

    def test_bankroll_negative(self):
        size = self.engine.kelly_size(0.80, 0.70, -10.0, False)
        assert size == 0.0

    def test_price_at_boundary_zero(self):
        size = self.engine.kelly_size(0.50, 0.0, 100.0, False)
        assert size == 0.0

    def test_price_at_boundary_one(self):
        size = self.engine.kelly_size(0.50, 1.0, 100.0, False)
        assert size == 0.0

    def test_even_odds_no_edge(self):
        size = self.engine.kelly_size(0.50, 0.50, 100.0, False)
        assert size == 0.0

    @pytest.mark.parametrize("p_model,p_market", [
        (0.01, 0.50), (0.99, 0.50), (0.50, 0.01), (0.50, 0.99),
    ])
    def test_edge_cases_no_crash(self, p_model, p_market):
        size = self.engine.kelly_size(p_model, p_market, 100.0, False)
        assert size >= 0.0


class TestExpectedValue:

    def setup_method(self):
        self.engine = SizingEngine()

    def test_positive_edge_positive_ev(self):
        ev = self.engine.expected_value(0.80, 0.70)
        assert ev > 0

    def test_negative_edge_negative_ev(self):
        ev = self.engine.expected_value(0.60, 0.75)
        assert ev < 0

    def test_fair_price_zero_ev(self):
        ev = self.engine.expected_value(0.50, 0.50)
        assert abs(ev) < 0.01


class TestMonteCarloVaR:

    def setup_method(self):
        self.engine = SizingEngine()

    def test_insufficient_data_returns_zero(self):
        var, es = self.engine.monte_carlo_var([1.0, 2.0])
        assert var == 0.0

    def test_var_with_data(self):
        returns = [0.5, -0.3, 0.2, -0.8, 0.1, -0.2, 0.3, -0.1, 0.4, -0.5]
        var, es = self.engine.monte_carlo_var(returns, n_paths=1000, horizon=10)
        assert var <= 0  # VaR should be negative (loss)
