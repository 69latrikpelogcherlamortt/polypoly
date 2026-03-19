"""Tests for GateValidator — 7 gates before each trade."""
import pytest
from trading.risk_manager import GateValidator, GateCheckInput, GateCheckResult


class TestGateValidator:

    def setup_method(self):
        self.gv = GateValidator()

    def _make_input(self, **overrides) -> GateCheckInput:
        defaults = dict(
            edge=0.08, ev=0.15, size_requested=3.0, kelly_size=3.0,
            bankroll=100.0, total_exposure=10.0, open_positions=2,
            var_95=-2.0, mdd_30d=0.03, brier_15=0.15,
            strategy="S1", is_longshot=False, z_score=2.0,
            p_model=0.80, market_price=0.72,
        )
        defaults.update(overrides)
        return GateCheckInput(**defaults)

    def test_all_gates_pass(self):
        result = self.gv.validate(self._make_input())
        assert result.action == "TRADE"

    def test_edge_gate_fails(self):
        result = self.gv.validate(self._make_input(edge=0.03))
        assert "edge_gate" in result.failures

    def test_ev_gate_fails(self):
        result = self.gv.validate(self._make_input(ev=-0.01))
        assert "ev_gate" in result.failures

    def test_kelly_gate_reduce(self):
        result = self.gv.validate(self._make_input(size_requested=5.0, kelly_size=2.0))
        assert "kelly_gate_reduce" in result.failures
        assert result.action == "REDUCE"

    def test_exposure_gate_fails(self):
        result = self.gv.validate(self._make_input(total_exposure=38.0))
        assert "exposure_gate" in result.failures

    def test_max_positions_gate_fails(self):
        result = self.gv.validate(self._make_input(open_positions=8))
        assert "max_positions_gate" in result.failures

    def test_var_gate_fails(self):
        result = self.gv.validate(self._make_input(var_95=-10.0))
        assert "var_gate" in result.failures

    def test_mdd_gate_halts(self):
        result = self.gv.validate(self._make_input(mdd_30d=0.09))
        assert "mdd_gate" in result.failures
        assert result.action == "HALT"

    def test_brier_gate_halts(self):
        result = self.gv.validate(self._make_input(brier_15=0.25))
        assert "brier_gate" in result.failures
        assert result.action == "HALT"

    def test_bankroll_stop(self):
        result = self.gv.validate(self._make_input(bankroll=55.0))
        assert "bankroll_stop" in result.failures

    def test_s2_edge_ratio_gate(self):
        result = self.gv.validate(self._make_input(
            strategy="S2", p_model=0.06, market_price=0.05, edge=0.01
        ))
        assert any("s2_" in f for f in result.failures)

    def test_z_score_gate_longshot(self):
        result = self.gv.validate(self._make_input(
            is_longshot=True, z_score=1.2
        ))
        assert "z_score_gate_longshot" in result.failures
