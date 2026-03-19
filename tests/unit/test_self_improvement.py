"""Tests for SelfImprovementEngine and DynamicConfig."""
import pytest
import sqlite3
from core.dynamic_config import DynamicConfig
from core.self_improvement import SelfImprovementEngine, ResolutionAnalysis


@pytest.fixture
def dynamic_config():
    conn = sqlite3.connect(":memory:")
    return DynamicConfig(conn), conn


@pytest.fixture
def improvement_engine(dynamic_config):
    dc, conn = dynamic_config
    return SelfImprovementEngine(conn, dc), conn, dc


class TestDynamicConfig:

    def test_default_values(self, dynamic_config):
        dc, _ = dynamic_config
        assert dc.get("BAYESIAN_HARD_CAP") == 0.15
        assert dc.get_z_score_threshold("macro_fed") == 1.5
        assert dc.get_z_score_threshold("sports") == 2.0

    def test_set_and_get(self, dynamic_config):
        dc, _ = dynamic_config
        dc.set("BAYESIAN_HARD_CAP", 0.12, reason="test")
        assert dc.get("BAYESIAN_HARD_CAP") == 0.12

    def test_z_score_threshold_per_category(self, dynamic_config):
        dc, _ = dynamic_config
        dc.set_z_score_threshold("crypto", 2.5, reason="underperformance")
        assert dc.get_z_score_threshold("crypto") == 2.5
        assert dc.get_z_score_threshold("macro_fed") == 1.5  # Unchanged

    def test_unknown_param_raises_or_returns(self, dynamic_config):
        dc, _ = dynamic_config
        # Implementation may raise KeyError or return a default
        try:
            val = dc.get("NONEXISTENT_PARAM")
            assert isinstance(val, (int, float))  # If it returns, it's numeric
        except KeyError:
            pass  # Also acceptable to raise

    def test_persists_across_reload(self, dynamic_config):
        dc, conn = dynamic_config
        dc.set("BAYESIAN_HARD_CAP", 0.10, reason="test")
        dc2 = DynamicConfig(conn)
        assert dc2.get("BAYESIAN_HARD_CAP") == 0.10


class TestResolutionAnalysis:

    def test_model_error_overconfident(self):
        a = ResolutionAnalysis("m1", "Test?", "macro_fed", 0.80, 0.72, 0.0, -4.0)
        assert a.model_error == 0.80  # p_model=0.80, outcome=0 → error=0.80

    def test_model_error_underconfident(self):
        a = ResolutionAnalysis("m1", "Test?", "crypto", 0.30, 0.25, 1.0, 2.0)
        assert a.model_error == -0.70  # p_model=0.30, outcome=1 → error=-0.70

    def test_edge_was_real_correct(self):
        a = ResolutionAnalysis("m1", "Test?", "macro_fed", 0.80, 0.72, 1.0, 1.5)
        assert a.edge_was_real  # Predicted YES (0.80>0.72), outcome=1

    def test_edge_was_real_wrong(self):
        a = ResolutionAnalysis("m1", "Test?", "macro_fed", 0.80, 0.72, 0.0, -4.0)
        assert not a.edge_was_real  # Predicted YES, outcome=0

    def test_brier_contribution(self):
        a = ResolutionAnalysis("m1", "Test?", "crypto", 0.70, 0.60, 1.0, 0.5)
        assert a.brier_contribution == pytest.approx((0.70 - 1.0) ** 2)


class TestSelfImprovementEngine:

    def test_analyze_returns_analysis(self, improvement_engine):
        engine, conn, dc = improvement_engine
        # Create the table that SelfImprovementEngine reads from
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                market_id TEXT, question TEXT, category TEXT,
                p_model REAL, p_market REAL, pnl REAL,
                p_model_entry REAL, p_market_entry REAL, trade_pnl REAL,
                outcome INTEGER, status TEXT, exit_ts TEXT
            );
        """)
        conn.execute(
            "INSERT INTO trades (market_id, question, category, p_model, p_market, pnl, "
            "p_model_entry, p_market_entry, trade_pnl, outcome, status, exit_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("m1", "Will Fed cut?", "macro_fed", 0.80, 0.72, 1.5, 0.80, 0.72, 1.5,
             1, "closed", "2026-03-19")
        )
        conn.commit()
        result = engine.analyze_after_resolution("m1", 1.0)
        assert result is not None
        assert result.outcome == 1.0
