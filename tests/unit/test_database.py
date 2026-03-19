"""Tests for database CRUD + schema integrity."""
import pytest
from datetime import datetime, timezone
from core.database import (
    init_trading_db, TradeRepository, MetricsEngine,
    TradeRecord, OpenPosition, PortfolioState,
)
from pathlib import Path


class TestDatabaseSchema:

    def test_init_creates_all_tables(self, trading_conn):
        tables = trading_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        table_names = {t[0] for t in tables}
        assert "trades" in table_names
        assert "open_positions" in table_names
        assert "nav_history" in table_names
        assert "kill_switch_state" in table_names
        assert "portfolio_snapshots" in table_names

    def test_indexes_created(self, trading_conn):
        indexes = trading_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
        ).fetchall()
        idx_names = {i[0] for i in indexes}
        assert "idx_trades_market_id" in idx_names
        assert "idx_trades_status" in idx_names
        assert "idx_nav_history_ts" in idx_names

    def test_wal_mode_enabled(self, trading_conn):
        mode = trading_conn.execute("PRAGMA journal_mode").fetchone()[0]
        # :memory: DBs return "memory" for journal_mode; file DBs return "wal"
        assert mode in ("wal", "memory")

    def test_idempotent_init(self, trading_conn):
        # Calling init again should not fail
        init_trading_db(Path(":memory:"))


class TestTradeRepository:

    def test_insert_and_retrieve_trade(self, trade_repo):
        trade = TradeRecord(
            market_id="m1", question="Test?", strategy="S1", side="BUY",
            token_id="tok1", size_eur=3.0, n_shares=4.0, fill_price=0.75,
            p_model=0.80, p_market=0.72, edge=0.08, z_score=1.6,
            kelly_fraction=0.03,
        )
        row_id = trade_repo.insert_trade(trade)
        assert row_id > 0

    def test_close_trade(self, trade_repo):
        trade = TradeRecord(
            market_id="m2", question="Test2?", strategy="S1", side="BUY",
            token_id="tok2", size_eur=2.0, n_shares=3.0, fill_price=0.65,
            p_model=0.75, p_market=0.65, edge=0.10, z_score=2.0,
            kelly_fraction=0.02,
        )
        trade_repo.insert_trade(trade)
        trade_repo.close_trade("m2", outcome=1, pnl=0.50,
                               p_at_resolution=1.0)
        closed = trade_repo.get_closed_trades(limit=10)
        assert any(t["market_id"] == "m2" for t in closed)

    def test_upsert_position(self, trade_repo, sample_position):
        trade_repo.upsert_position(sample_position)
        pos = trade_repo.get_position(sample_position.market_id)
        assert pos is not None
        assert pos.p_model == sample_position.p_model

    def test_remove_position(self, trade_repo, sample_position):
        trade_repo.upsert_position(sample_position)
        trade_repo.remove_position(sample_position.market_id)
        pos = trade_repo.get_position(sample_position.market_id)
        assert pos is None

    def test_record_nav(self, trade_repo):
        trade_repo.record_nav(nav=105.0, daily_pnl=5.0)
        history = trade_repo.get_nav_history(days=1)
        assert len(history) == 1
        assert history[0]["nav"] == 105.0

    def test_kill_switch_state(self, trade_repo):
        trade_repo.set_kill_switch("test_key", "test_value")
        val = trade_repo.get_kill_switch("test_key")
        assert val == "test_value"

    def test_kill_switch_default(self, trade_repo):
        val = trade_repo.get_kill_switch("nonexistent", "default")
        assert val == "default"


class TestMetricsEngine:

    def test_brier_no_data(self, metrics):
        assert metrics.brier_score_last_n(15) == 0.15  # conservative prior

    def test_mdd_no_data(self, metrics):
        assert metrics.mdd_last_n_days(30) == 0.0

    def test_win_rate_no_data(self, metrics):
        assert metrics.win_rate() == 0.0

    def test_profit_factor_no_data(self, metrics):
        assert metrics.profit_factor_last_n(50) == 1.0

    def test_consecutive_losses_no_data(self, metrics):
        assert metrics.consecutive_losses() == 0

    def test_build_portfolio_state(self, metrics, sample_position):
        state = metrics.build_portfolio_state(100.0, [sample_position])
        assert isinstance(state, PortfolioState)
        assert state.bankroll == 100.0
        assert state.open_positions == 1
