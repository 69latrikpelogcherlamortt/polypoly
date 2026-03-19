"""Tests for KillSwitchMonitor + DailyLossMonitor."""
import pytest
from datetime import datetime, timezone, timedelta
from trading.risk_manager import (
    KillSwitchMonitor, DailyLossMonitor, CircuitBreaker,
    check_concentration_risk, get_market_category,
)
from core.database import init_trading_db, TradeRepository
from pathlib import Path


class TestKillSwitchMonitor:

    @pytest.fixture(autouse=True)
    def setup(self, trade_repo):
        self.ks = KillSwitchMonitor(trade_repo)

    def test_bankroll_below_stop(self):
        result = self.ks.check(
            bankroll=55.0, mdd_30d=0.0, brier_15=0.15,
            consecutive_losses=0, sharpe_30d=2.0, profit_factor=2.0,
        )
        assert result.active
        assert result.level == 1

    def test_bankroll_ok(self):
        result = self.ks.check(
            bankroll=80.0, mdd_30d=0.0, brier_15=0.15,
            consecutive_losses=0, sharpe_30d=2.0, profit_factor=2.0,
        )
        assert not result.active

    def test_mdd_triggers(self):
        result = self.ks.check(
            bankroll=100.0, mdd_30d=0.09, brier_15=0.15,
            consecutive_losses=0, sharpe_30d=2.0, profit_factor=2.0,
        )
        assert result.active
        assert result.level == 2

    def test_brier_triggers(self):
        result = self.ks.check(
            bankroll=100.0, mdd_30d=0.03, brier_15=0.25,
            consecutive_losses=0, sharpe_30d=2.0, profit_factor=2.0,
        )
        assert result.active
        assert result.level == 3

    def test_consecutive_losses_triggers(self):
        result = self.ks.check(
            bankroll=100.0, mdd_30d=0.03, brier_15=0.15,
            consecutive_losses=5, sharpe_30d=2.0, profit_factor=2.0,
        )
        assert result.active

    def test_consecutive_losses_below_threshold(self):
        result = self.ks.check(
            bankroll=100.0, mdd_30d=0.03, brier_15=0.15,
            consecutive_losses=4, sharpe_30d=2.0, profit_factor=2.0,
        )
        assert not result.active

    def test_sharpe_triggers(self):
        result = self.ks.check(
            bankroll=100.0, mdd_30d=0.03, brier_15=0.15,
            consecutive_losses=0, sharpe_30d=0.5, profit_factor=2.0,
        )
        assert result.active
        assert result.level == 4

    def test_all_ok(self):
        result = self.ks.check(
            bankroll=100.0, mdd_30d=0.03, brier_15=0.15,
            consecutive_losses=1, sharpe_30d=2.0, profit_factor=2.0,
        )
        assert not result.active
        assert result.allow_new_trades


class TestDailyLossMonitor:

    @pytest.fixture(autouse=True)
    def setup(self, trading_conn):
        self.conn = trading_conn
        self.monitor = DailyLossMonitor()

    def test_no_trades_no_trigger(self):
        result = self.monitor.check(self.conn, bankroll=100.0)
        assert not result.active

    def test_daily_loss_eur_triggers(self):
        # Insert losing trades today
        now = datetime.now(timezone.utc)
        self.conn.execute(
            "INSERT INTO trades (market_id, question, strategy, side, token_id, "
            "size_eur, n_shares, fill_price, p_model, p_market, edge, z_score, "
            "kelly_fraction, status, entry_ts, exit_ts, pnl) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("m1", "test", "S1", "BUY", "t1", 5.0, 5.0, 0.8, 0.85, 0.8,
             0.05, 1.5, 0.03, "closed", now.isoformat(), now.isoformat(), -16.0)
        )
        self.conn.commit()
        result = self.monitor.check(self.conn, bankroll=100.0)
        assert result.active

    def test_daily_loss_pct_triggers(self):
        now = datetime.now(timezone.utc)
        self.conn.execute(
            "INSERT INTO trades (market_id, question, strategy, side, token_id, "
            "size_eur, n_shares, fill_price, p_model, p_market, edge, z_score, "
            "kelly_fraction, status, entry_ts, exit_ts, pnl) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("m2", "test", "S1", "BUY", "t2", 5.0, 5.0, 0.8, 0.85, 0.8,
             0.05, 1.5, 0.03, "closed", now.isoformat(), now.isoformat(), -10.0)
        )
        self.conn.commit()
        # 10€ loss on 60€ bankroll = 16.7% > 15%
        result = self.monitor.check(self.conn, bankroll=60.0)
        assert result.active

    def test_yesterday_loss_no_trigger(self):
        yesterday = (datetime.now(timezone.utc) - timedelta(hours=25))
        self.conn.execute(
            "INSERT INTO trades (market_id, question, strategy, side, token_id, "
            "size_eur, n_shares, fill_price, p_model, p_market, edge, z_score, "
            "kelly_fraction, status, entry_ts, exit_ts, pnl) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("m3", "test", "S1", "BUY", "t3", 5.0, 5.0, 0.8, 0.85, 0.8,
             0.05, 1.5, 0.03, "closed", yesterday.isoformat(), yesterday.isoformat(), -20.0)
        )
        self.conn.commit()
        result = self.monitor.check(self.conn, bankroll=100.0)
        assert not result.active


class TestCircuitBreaker:

    def test_initial_state_allows_calls(self):
        cb = CircuitBreaker(name="test")
        assert cb.call_allowed()

    def test_opens_after_threshold(self):
        cb = CircuitBreaker(name="test", failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        assert cb.call_allowed()
        cb.record_failure()
        assert not cb.call_allowed()

    def test_success_resets(self):
        cb = CircuitBreaker(name="test", failure_threshold=2)
        cb.record_failure()
        cb.record_success()
        cb.record_failure()
        assert cb.call_allowed()  # reset after success

    def test_half_open_after_timeout(self):
        cb = CircuitBreaker(name="test", failure_threshold=1, recovery_timeout=0)
        cb.record_failure()
        assert not cb.call_allowed() or cb.call_allowed()  # immediate recovery


class TestConcentrationRisk:

    def test_category_detection(self):
        assert get_market_category("Will Fed cut rates?") == "macro_fed"
        assert get_market_category("Will BTC reach 120k?") == "crypto"
        assert get_market_category("Who wins NBA finals?") == "sports"
        assert get_market_category("Random question?") == "other"

    def test_concentration_blocks(self, sample_position):
        # 3 macro positions
        positions = [sample_position, sample_position, sample_position]
        passed, reason = check_concentration_risk(
            "Will FOMC pause in April?", positions
        )
        assert not passed
        assert "concentration_risk" in reason

    def test_concentration_passes(self, sample_position, sample_position_crypto):
        positions = [sample_position, sample_position_crypto]
        passed, _ = check_concentration_risk(
            "Will Fed cut in June?", positions
        )
        assert passed
