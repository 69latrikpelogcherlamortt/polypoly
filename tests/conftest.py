"""
Shared fixtures for PAF-001 test suite.
"""
import sqlite3
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from core.database import init_trading_db, TradeRepository, MetricsEngine, OpenPosition


@pytest.fixture
def trading_conn():
    """In-memory SQLite DB with full schema — isolated per test."""
    conn = init_trading_db(Path(":memory:"))
    yield conn
    conn.close()


@pytest.fixture
def trade_repo(trading_conn):
    return TradeRepository(trading_conn)


@pytest.fixture
def metrics(trading_conn):
    return MetricsEngine(trading_conn)


@pytest.fixture
def sample_position():
    return OpenPosition(
        market_id="0xtest123",
        question="Will the Fed cut rates in March 2026?",
        token_id="tok_yes_123",
        strategy="S1",
        p_model=0.80,
        p_market=0.72,
        edge=0.08,
        z_score=1.6,
        n_shares=6.0,
        cost_basis=4.32,
        current_price=0.74,
        days_to_res=10.0,
        entry_ts="2026-03-10T10:00:00+00:00",
        last_updated="2026-03-19T10:00:00+00:00",
        category="macro",
        keywords=["fed", "fomc", "cut", "march"],
        sigma_14d=0.06,
    )


@pytest.fixture
def sample_position_crypto():
    return OpenPosition(
        market_id="0xbtc120k",
        question="Will BTC reach $120,000 before December 2026?",
        token_id="tok_yes_btc",
        strategy="S2",
        p_model=0.12,
        p_market=0.05,
        edge=0.07,
        z_score=2.4,
        n_shares=80.0,
        cost_basis=4.00,
        current_price=0.06,
        days_to_res=45.0,
        entry_ts="2026-03-01T10:00:00+00:00",
        last_updated="2026-03-19T10:00:00+00:00",
        category="crypto",
        keywords=["btc", "bitcoin", "120k"],
        sigma_14d=0.03,
    )
