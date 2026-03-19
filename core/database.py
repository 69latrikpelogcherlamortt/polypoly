"""
database.py  ·  Polymarket Trading Bot
────────────────────────────────────────
Couche de persistance SQLite pour :
  - trades (log, P&L, outcomes)
  - positions ouvertes
  - métriques de performance (Brier, Sharpe, MDD)
  - fills partiels
  - repricing log

Le signal_db (signaux Crucix, calibration) est géré par crucix_router.py.
Ce fichier gère le trading_db (positions, trades, performance).
"""

from __future__ import annotations

import json
import sqlite3
import statistics
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from core.config import DB_PATH


def _safe_json_loads(value, default):
    """json.loads with fallback on JSONDecodeError."""
    try:
        return json.loads(value or json.dumps(default))
    except json.JSONDecodeError:
        return default


# ═══════════════════════════════════════════════════════════════════════════
# 1. DATACLASSES
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class TradeRecord:
    """Log complet d'un trade."""
    market_id:       str
    question:        str
    strategy:        str           # "S1" ou "S2"
    side:            str           # "BUY" (on n'achète que YES)
    token_id:        str
    size_eur:        float         # montant engagé en EUR
    n_shares:        float         # nombre de shares achetées
    fill_price:      float         # prix moyen de fill
    p_model:         float         # probabilité modèle au moment du trade
    p_market:        float         # prix marché au moment du trade
    edge:            float         # p_model - p_market
    z_score:         float
    kelly_fraction:  float
    category:        str           = ""
    gates_passed:    list[str]     = field(default_factory=list)
    outcome:         Optional[int] = None    # 1=YES, 0=NO (après résolution)
    pnl:             Optional[float] = None
    brier_contrib:   Optional[float] = None
    p_at_resolution: Optional[float] = None
    status:          str           = "open"  # open | closed | cancelled
    entry_ts:        str           = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    exit_ts:         Optional[str] = None
    exit_reason:     Optional[str] = None    # "resolution" | "edge_dead" | "p_flip" | ...
    order_id:        Optional[str] = None
    fill_rate:       float         = 1.0     # % rempli


@dataclass
class OpenPosition:
    """Position actuellement ouverte."""
    market_id:    str
    question:     str
    token_id:     str
    strategy:     str
    p_model:      float
    p_market:     float
    edge:         float
    z_score:      float
    n_shares:     float
    cost_basis:   float          # montant total engagé (EUR)
    current_price: float         # dernier prix marché observé
    days_to_res:  float
    entry_ts:     str
    last_updated: str
    category:     str
    keywords:     list[str]      = field(default_factory=list)
    sigma_14d:    float          = 0.06
    order_id:     Optional[str]  = None


@dataclass
class PortfolioState:
    """Snapshot de l'état global du portefeuille."""
    bankroll:          float
    total_exposure:    float
    open_positions:    int
    unrealized_pnl:    float
    realized_pnl:      float
    total_pnl:         float
    mdd_30d:           float
    brier_15:          float       # Brier sur 15 dernières résolutions
    sharpe_30d:        float
    profit_factor:     float
    win_rate:          float
    var_95:            float
    consecutive_losses: int
    ts:                str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


# ═══════════════════════════════════════════════════════════════════════════
# 2. INIT BASE DE DONNÉES
# ═══════════════════════════════════════════════════════════════════════════

def init_trading_db(path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False)
    c = conn.cursor()

    # ── Trades ──────────────────────────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id        TEXT    NOT NULL,
            question         TEXT    NOT NULL,
            strategy         TEXT    NOT NULL,
            side             TEXT    NOT NULL DEFAULT 'BUY',
            token_id         TEXT    NOT NULL,
            size_eur         REAL    NOT NULL,
            n_shares         REAL    NOT NULL,
            fill_price       REAL    NOT NULL,
            p_model          REAL    NOT NULL,
            p_market         REAL    NOT NULL,
            edge             REAL    NOT NULL,
            z_score          REAL    NOT NULL,
            kelly_fraction   REAL    NOT NULL,
            gates_passed     TEXT    NOT NULL DEFAULT '[]',
            outcome          INTEGER,
            pnl              REAL,
            brier_contrib    REAL,
            p_at_resolution  REAL,
            status           TEXT    NOT NULL DEFAULT 'open',
            entry_ts         TEXT    NOT NULL,
            exit_ts          TEXT,
            exit_reason      TEXT,
            order_id         TEXT,
            fill_rate        REAL    NOT NULL DEFAULT 1.0,
            category         TEXT    NOT NULL DEFAULT ''
        )
    """)
    # Migration : ajoute la colonne si absente (DB existante)
    try:
        c.execute("ALTER TABLE trades ADD COLUMN category TEXT NOT NULL DEFAULT ''")
    except Exception:
        pass  # colonne déjà présente

    # ── Positions ouvertes ───────────────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS open_positions (
            market_id     TEXT    PRIMARY KEY,
            question      TEXT    NOT NULL,
            token_id      TEXT    NOT NULL,
            strategy      TEXT    NOT NULL,
            p_model       REAL    NOT NULL,
            p_market      REAL    NOT NULL,
            edge          REAL    NOT NULL,
            z_score       REAL    NOT NULL,
            n_shares      REAL    NOT NULL,
            cost_basis    REAL    NOT NULL,
            current_price REAL    NOT NULL,
            days_to_res   REAL    NOT NULL,
            entry_ts      TEXT    NOT NULL,
            last_updated  TEXT    NOT NULL,
            category      TEXT    NOT NULL DEFAULT '',
            keywords      TEXT    NOT NULL DEFAULT '[]',
            sigma_14d     REAL    NOT NULL DEFAULT 0.06,
            order_id      TEXT
        )
    """)

    # ── Portfolio snapshots ───────────────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS portfolio_snapshots (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            ts                  TEXT    NOT NULL,
            bankroll            REAL    NOT NULL,
            total_exposure      REAL    NOT NULL,
            open_positions      INTEGER NOT NULL,
            unrealized_pnl      REAL    NOT NULL,
            realized_pnl        REAL    NOT NULL,
            total_pnl           REAL    NOT NULL,
            mdd_30d             REAL    NOT NULL,
            brier_15            REAL    NOT NULL,
            sharpe_30d          REAL    NOT NULL,
            profit_factor       REAL    NOT NULL,
            win_rate            REAL    NOT NULL,
            var_95              REAL    NOT NULL,
            consecutive_losses  INTEGER NOT NULL
        )
    """)

    # ── Partial fill log ─────────────────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS partial_fills (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              TEXT    NOT NULL,
            market_id       TEXT    NOT NULL,
            order_id        TEXT    NOT NULL,
            fill_rate       REAL    NOT NULL,
            action_taken    TEXT    NOT NULL,
            days_resolution REAL    NOT NULL,
            edge_at_entry   REAL    NOT NULL,
            edge_at_partial REAL    NOT NULL,
            outcome         INTEGER,
            pnl             REAL
        )
    """)

    # ── Reprice log ──────────────────────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS reprice_log (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            ts             TEXT    NOT NULL,
            market_id      TEXT    NOT NULL,
            old_price      REAL    NOT NULL,
            new_price      REAL    NOT NULL,
            edge_before    REAL    NOT NULL,
            edge_after     REAL    NOT NULL,
            mid_delta      REAL    NOT NULL,
            reprice_count  INTEGER NOT NULL,
            outcome        INTEGER,
            filled_after   INTEGER
        )
    """)

    # ── Kill switch state ────────────────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS kill_switch_state (
            key         TEXT PRIMARY KEY,
            value       TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        )
    """)

    # ── NAV history (pour courbe de NAV) ────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS nav_history (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            ts       TEXT    NOT NULL,
            nav      REAL    NOT NULL,
            daily_pnl REAL   NOT NULL DEFAULT 0.0
        )
    """)

    conn.commit()
    return conn


# ═══════════════════════════════════════════════════════════════════════════
# 3. TRADE REPOSITORY
# ═══════════════════════════════════════════════════════════════════════════

class TradeRepository:
    """CRUD complet pour les trades et positions."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._lock = threading.Lock()

    # ── Trades ────────────────────────────────────────────────────────────

    def insert_trade(self, t: TradeRecord) -> int:
        with self._lock:
            c = self.conn.execute("""
                INSERT INTO trades
                (market_id, question, strategy, side, token_id, size_eur,
                 n_shares, fill_price, p_model, p_market, edge, z_score,
                 kelly_fraction, gates_passed, status, entry_ts, order_id, fill_rate,
                 category)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                t.market_id, t.question, t.strategy, t.side, t.token_id,
                t.size_eur, t.n_shares, t.fill_price, t.p_model, t.p_market,
                t.edge, t.z_score, t.kelly_fraction,
                json.dumps(t.gates_passed), t.status, t.entry_ts,
                t.order_id, t.fill_rate, t.category,
            ))
            self.conn.commit()
            return c.lastrowid

    def close_trade(
        self,
        market_id: str,
        outcome: Optional[int],
        pnl: float,
        p_at_resolution: float,
        exit_reason: str = "resolution",
    ):
        brier = None
        if outcome is not None:
            # Brier calculé seulement sur les résolutions réelles (pas les sorties anticipées)
            row = self.conn.execute(
                "SELECT p_model FROM trades WHERE market_id=? AND status='open'",
                (market_id,)
            ).fetchone()
            if row:
                brier = round((row[0] - outcome) ** 2, 4)

        with self._lock:
            self.conn.execute("""
                UPDATE trades
                SET outcome=?, pnl=?, brier_contrib=?, p_at_resolution=?,
                    status='closed', exit_ts=?, exit_reason=?
                WHERE market_id=? AND status='open'
            """, (
                outcome, pnl, brier, p_at_resolution,
                datetime.now(timezone.utc).isoformat(),
                exit_reason, market_id,
            ))
            self.conn.commit()

    def get_open_trades(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM trades WHERE status='open' ORDER BY entry_ts DESC"
        ).fetchall()
        cols = [d[0] for d in self.conn.execute(
            "SELECT * FROM trades LIMIT 0"
        ).description or []]
        # fallback column names
        cols = ["id","market_id","question","strategy","side","token_id",
                "size_eur","n_shares","fill_price","p_model","p_market",
                "edge","z_score","kelly_fraction","gates_passed","outcome",
                "pnl","brier_contrib","p_at_resolution","status","entry_ts",
                "exit_ts","exit_reason","order_id","fill_rate"]
        return [dict(zip(cols, r)) for r in rows]

    def get_closed_trades(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM trades WHERE status='closed' ORDER BY exit_ts DESC LIMIT ?",
            (limit,)
        ).fetchall()
        cols = ["id","market_id","question","strategy","side","token_id",
                "size_eur","n_shares","fill_price","p_model","p_market",
                "edge","z_score","kelly_fraction","gates_passed","outcome",
                "pnl","brier_contrib","p_at_resolution","status","entry_ts",
                "exit_ts","exit_reason","order_id","fill_rate"]
        return [dict(zip(cols, r)) for r in rows]

    # ── Positions ─────────────────────────────────────────────────────────

    def upsert_position(self, pos: OpenPosition):
        with self._lock:
            self.conn.execute("""
                INSERT INTO open_positions
                (market_id, question, token_id, strategy, p_model, p_market,
                 edge, z_score, n_shares, cost_basis, current_price, days_to_res,
                 entry_ts, last_updated, category, keywords, sigma_14d, order_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(market_id) DO UPDATE SET
                    p_model=excluded.p_model,
                    p_market=excluded.p_market,
                    edge=excluded.edge,
                    z_score=excluded.z_score,
                    n_shares=excluded.n_shares,
                    cost_basis=excluded.cost_basis,
                    current_price=excluded.current_price,
                    days_to_res=excluded.days_to_res,
                    last_updated=excluded.last_updated,
                    sigma_14d=excluded.sigma_14d
            """, (
                pos.market_id, pos.question, pos.token_id, pos.strategy,
                pos.p_model, pos.p_market, pos.edge, pos.z_score,
                pos.n_shares, pos.cost_basis, pos.current_price, pos.days_to_res,
                pos.entry_ts, pos.last_updated, pos.category,
                json.dumps(pos.keywords), pos.sigma_14d, pos.order_id,
            ))
            self.conn.commit()

    def get_all_positions(self) -> list[OpenPosition]:
        rows = self.conn.execute(
            "SELECT * FROM open_positions ORDER BY entry_ts"
        ).fetchall()
        result = []
        for r in rows:
            result.append(OpenPosition(
                market_id=r[0], question=r[1], token_id=r[2], strategy=r[3],
                p_model=r[4], p_market=r[5], edge=r[6], z_score=r[7],
                n_shares=r[8], cost_basis=r[9], current_price=r[10],
                days_to_res=r[11], entry_ts=r[12], last_updated=r[13],
                category=r[14], keywords=_safe_json_loads(r[15], []),
                sigma_14d=r[16], order_id=r[17],
            ))
        return result

    def get_position(self, market_id: str) -> Optional[OpenPosition]:
        row = self.conn.execute(
            "SELECT * FROM open_positions WHERE market_id=?", (market_id,)
        ).fetchone()
        if not row:
            return None
        return OpenPosition(
            market_id=row[0], question=row[1], token_id=row[2], strategy=row[3],
            p_model=row[4], p_market=row[5], edge=row[6], z_score=row[7],
            n_shares=row[8], cost_basis=row[9], current_price=row[10],
            days_to_res=row[11], entry_ts=row[12], last_updated=row[13],
            category=row[14], keywords=_safe_json_loads(row[15], []),
            sigma_14d=row[16], order_id=row[17],
        )

    def remove_position(self, market_id: str):
        with self._lock:
            self.conn.execute(
                "DELETE FROM open_positions WHERE market_id=?", (market_id,)
            )
            self.conn.commit()

    def update_position_price(self, market_id: str, new_price: float,
                               new_p_model: float, new_edge: float,
                               new_z: float, days_to_res: float):
        with self._lock:
            self.conn.execute("""
                UPDATE open_positions
                SET current_price=?, p_model=?, edge=?, z_score=?,
                    days_to_res=?, last_updated=?
                WHERE market_id=?
            """, (
                new_price, new_p_model, new_edge, new_z, days_to_res,
                datetime.now(timezone.utc).isoformat(), market_id,
            ))
            self.conn.commit()

    # ── NAV ───────────────────────────────────────────────────────────────

    def record_nav(self, nav: float, daily_pnl: float = 0.0):
        with self._lock:
            self.conn.execute(
                "INSERT INTO nav_history (ts, nav, daily_pnl) VALUES (?,?,?)",
                (datetime.now(timezone.utc).isoformat(), nav, daily_pnl)
            )
            self.conn.commit()

    def get_nav_history(self, days: int = 30) -> list[dict]:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = self.conn.execute(
            "SELECT ts, nav, daily_pnl FROM nav_history WHERE ts > ? ORDER BY ts",
            (since,)
        ).fetchall()
        return [{"ts": r[0], "nav": r[1], "daily_pnl": r[2]} for r in rows]

    # ── Kill switch state ────────────────────────────────────────────────

    def set_kill_switch(self, key: str, value: str):
        with self._lock:
            self.conn.execute("""
                INSERT INTO kill_switch_state (key, value, updated_at) VALUES (?,?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """, (key, value, datetime.now(timezone.utc).isoformat()))
            self.conn.commit()

    def get_kill_switch(self, key: str, default: str = "") -> str:
        row = self.conn.execute(
            "SELECT value FROM kill_switch_state WHERE key=?", (key,)
        ).fetchone()
        return row[0] if row else default

    # ── Partial fill log ─────────────────────────────────────────────────

    def log_partial_fill(self, market_id: str, order_id: str, fill_rate: float,
                          action: str, days_res: float, edge_entry: float,
                          edge_partial: float):
        with self._lock:
            self.conn.execute("""
                INSERT INTO partial_fills
                (ts, market_id, order_id, fill_rate, action_taken,
                 days_resolution, edge_at_entry, edge_at_partial)
                VALUES (?,?,?,?,?,?,?,?)
            """, (
                datetime.now(timezone.utc).isoformat(),
                market_id, order_id, fill_rate, action,
                days_res, edge_entry, edge_partial,
            ))
            self.conn.commit()

    # ── Reprice log ──────────────────────────────────────────────────────

    def log_reprice(self, market_id: str, old_price: float, new_price: float,
                    edge_before: float, edge_after: float, mid_delta: float,
                    reprice_count: int):
        with self._lock:
            self.conn.execute("""
                INSERT INTO reprice_log
                (ts, market_id, old_price, new_price, edge_before, edge_after,
                 mid_delta, reprice_count)
                VALUES (?,?,?,?,?,?,?,?)
            """, (
                datetime.now(timezone.utc).isoformat(),
                market_id, old_price, new_price,
                edge_before, edge_after, mid_delta, reprice_count,
            ))
            self.conn.commit()


# ═══════════════════════════════════════════════════════════════════════════
# 4. METRICS ENGINE
# ═══════════════════════════════════════════════════════════════════════════

class MetricsEngine:
    """
    Calcule toutes les métriques de performance à partir de l'historique.
    Appelé à chaque snapshot portfolio.
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def brier_score_last_n(self, n: int = 15) -> float:
        """Brier score sur les n dernières résolutions."""
        rows = self.conn.execute("""
            SELECT p_model, outcome FROM trades
            WHERE status='closed' AND outcome IS NOT NULL
            ORDER BY exit_ts DESC LIMIT ?
        """, (n,)).fetchall()
        if not rows:
            return 0.15   # prior conservateur
        return sum((p - o) ** 2 for p, o in rows) / len(rows)

    def mdd_last_n_days(self, days: int = 30) -> float:
        """Max Drawdown sur les n derniers jours (depuis nav_history)."""
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = self.conn.execute(
            "SELECT nav FROM nav_history WHERE ts > ? ORDER BY ts",
            (since,)
        ).fetchall()
        if len(rows) < 2:
            return 0.0
        navs = [r[0] for r in rows]
        peak = navs[0]
        mdd  = 0.0
        for nav in navs[1:]:
            if nav > peak:
                peak = nav
            dd = (peak - nav) / peak if peak > 0 else 0.0
            mdd = max(mdd, dd)
        return round(mdd, 4)

    def sharpe_last_n_days(self, days: int = 30) -> float:
        """Sharpe Ratio rolling sur n jours (taux sans risque = 4.9%)."""
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = self.conn.execute(
            "SELECT daily_pnl, nav FROM nav_history WHERE ts > ? ORDER BY ts",
            (since,)
        ).fetchall()
        if len(rows) < 5:
            return 0.0
        returns = [r[0] / max(r[1], 1.0) for r in rows if r[1] > 0]
        if len(returns) < 2:
            return 0.0
        rf_daily = 0.049 / 365
        excess = [r - rf_daily for r in returns]
        mean_ex = sum(excess) / len(excess)
        try:
            std_ex = statistics.stdev(excess)
        except statistics.StatisticsError:
            return 0.0
        return round(mean_ex / std_ex * (365 ** 0.5), 3) if std_ex > 0 else 0.0

    def profit_factor_last_n(self, n: int = 50) -> float:
        """Profit Factor = gross_profit / gross_loss sur n trades."""
        rows = self.conn.execute("""
            SELECT pnl FROM trades
            WHERE status='closed' AND pnl IS NOT NULL
            ORDER BY exit_ts DESC LIMIT ?
        """, (n,)).fetchall()
        if not rows:
            return 1.0
        gains  = sum(r[0] for r in rows if r[0] > 0)
        losses = sum(abs(r[0]) for r in rows if r[0] < 0)
        return round(gains / losses, 3) if losses > 0 else 999.99

    def win_rate(self) -> float:
        row = self.conn.execute("""
            SELECT
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END),
                COUNT(*)
            FROM trades WHERE status='closed' AND pnl IS NOT NULL
        """).fetchone()
        if not row or not row[1]:
            return 0.0
        return round(row[0] / row[1], 3)

    def consecutive_losses(self) -> int:
        """Nombre de pertes consécutives depuis la dernière victoire."""
        rows = self.conn.execute("""
            SELECT pnl FROM trades
            WHERE status='closed' AND pnl IS NOT NULL
            ORDER BY exit_ts DESC LIMIT 20
        """).fetchall()
        count = 0
        for r in rows:
            if r[0] < 0:
                count += 1
            else:
                break
        return count

    def total_exposure(self, positions: list[OpenPosition]) -> float:
        return sum(p.cost_basis for p in positions)

    def unrealized_pnl(self, positions: list[OpenPosition]) -> float:
        total = 0.0
        for pos in positions:
            current_val = pos.n_shares * pos.current_price
            total += current_val - pos.cost_basis
        return round(total, 4)

    def realized_pnl(self) -> float:
        row = self.conn.execute(
            "SELECT SUM(pnl) FROM trades WHERE status='closed' AND pnl IS NOT NULL"
        ).fetchone()
        return round(row[0] or 0.0, 4)

    def build_portfolio_state(
        self,
        bankroll: float,
        positions: list[OpenPosition],
        var_95: float = 0.0,
    ) -> PortfolioState:
        return PortfolioState(
            bankroll          = round(bankroll, 2),
            total_exposure    = round(self.total_exposure(positions), 2),
            open_positions    = len(positions),
            unrealized_pnl    = self.unrealized_pnl(positions),
            realized_pnl      = self.realized_pnl(),
            total_pnl         = round(
                self.unrealized_pnl(positions) + self.realized_pnl(), 2
            ),
            mdd_30d           = self.mdd_last_n_days(30),
            brier_15          = self.brier_score_last_n(15),
            sharpe_30d        = self.sharpe_last_n_days(30),
            profit_factor     = self.profit_factor_last_n(50),
            win_rate          = self.win_rate(),
            var_95            = var_95,
            consecutive_losses= self.consecutive_losses(),
        )
