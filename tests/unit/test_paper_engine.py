"""Tests for PaperTradeEngine."""
import pytest
from trading.paper_engine import PaperTradeEngine


class TestPaperTradeEngine:

    def setup_method(self):
        self.engine = PaperTradeEngine(initial_bankroll=100.0)

    def test_initial_state(self):
        perf = self.engine.get_performance()
        assert perf["bankroll"] == 100.0
        assert perf["open_positions"] == 0
        assert perf["total_pnl"] == 0.0

    def test_open_position_reduces_bankroll(self):
        self.engine.simulate_open("m1", "Will Fed cut?", 5.0, 0.72)
        assert self.engine.bankroll < 100.0
        assert len(self.engine.positions) == 1

    def test_open_applies_commission(self):
        result = self.engine.simulate_open("m1", "Test?", 10.0, 0.50)
        assert result.commission > 0
        assert result.commission == pytest.approx(10.0 * 0.002)

    def test_open_applies_slippage(self):
        result = self.engine.simulate_open("m1", "Test?", 5.0, 0.50, slippage_estimate=0.01)
        assert result.price > 0.50

    def test_close_position_returns_pnl(self):
        self.engine.simulate_open("m1", "Test?", 5.0, 0.50)
        result = self.engine.simulate_close("m1", 0.60)
        assert result is not None
        assert result.pnl is not None
        assert "m1" not in self.engine.positions

    def test_close_nonexistent_returns_none(self):
        result = self.engine.simulate_close("m_nonexistent", 0.50)
        assert result is None

    def test_profitable_trade(self):
        self.engine.simulate_open("m1", "Test?", 5.0, 0.50, slippage_estimate=0.001)
        result = self.engine.simulate_close("m1", 0.80)
        assert result.pnl > 0

    def test_losing_trade(self):
        self.engine.simulate_open("m1", "Test?", 5.0, 0.50, slippage_estimate=0.001)
        result = self.engine.simulate_close("m1", 0.30)
        assert result.pnl < 0

    def test_update_prices(self):
        self.engine.simulate_open("m1", "Test?", 5.0, 0.50)
        self.engine.update_prices({"m1": 0.75})
        assert self.engine.positions["m1"].current_price == 0.75

    def test_performance_with_positions(self):
        self.engine.simulate_open("m1", "Test?", 5.0, 0.50)
        self.engine.update_prices({"m1": 0.60})
        perf = self.engine.get_performance()
        assert perf["open_positions"] == 1
        assert perf["unrealized_pnl"] != 0.0
