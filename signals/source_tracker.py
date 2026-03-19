"""
source_tracker.py  ·  Polymarket Trading Bot
──────────────────────────────────────────────
Tracks predictive performance of each signal source per market category.
Computes adaptive Brier-weighted source weights and disables poorly
calibrated sources automatically.
"""

from __future__ import annotations

import sqlite3
import logging
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np

log = logging.getLogger("source_tracker")


class SourcePerformanceTracker:
    """
    Monitors every signal source's accuracy per market category.

    Each time a market resolves, Brier scores are recomputed and
    source weights are recalibrated:
        weight = 1 / (brier + 1e-6)
    Weights are normalised per category. Sources with brier > 0.30
    are automatically disabled (weight set to 0).
    """

    def __init__(self, db_conn: sqlite3.Connection):
        self.conn = db_conn
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()

    # ── schema ────────────────────────────────────────────────────────────

    def _create_tables(self) -> None:
        cur = self.conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS source_contributions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                source_name TEXT    NOT NULL,
                market_id   TEXT    NOT NULL,
                category    TEXT    NOT NULL,
                p_contributed REAL  NOT NULL,
                p_final     REAL   NOT NULL,
                outcome     REAL,
                recorded_at TEXT    NOT NULL,
                resolved_at TEXT
            )
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_sc_source_cat
                ON source_contributions (source_name, category)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_sc_market
                ON source_contributions (market_id)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_sc_outcome
                ON source_contributions (outcome)
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS source_weights (
                source_name TEXT NOT NULL,
                category    TEXT NOT NULL,
                weight      REAL NOT NULL DEFAULT 1.0,
                brier       REAL,
                n_samples   INTEGER NOT NULL DEFAULT 0,
                enabled     INTEGER NOT NULL DEFAULT 1,
                updated_at  TEXT NOT NULL,
                PRIMARY KEY (source_name, category)
            )
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_sw_category
                ON source_weights (category)
        """)

        self.conn.commit()
        log.info("source_tracker tables ready")

    # ── public API ────────────────────────────────────────────────────────

    def record_contribution(
        self,
        source_name: str,
        market_id: str,
        category: str,
        p_contributed: float,
        p_final: float,
    ) -> None:
        """Log a source's probabilistic contribution to a market forecast."""
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """
            INSERT INTO source_contributions
                (source_name, market_id, category, p_contributed, p_final, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (source_name, market_id, category, p_contributed, p_final, now),
        )
        self.conn.commit()
        log.debug(
            "recorded contribution: source=%s market=%s cat=%s p=%.4f",
            source_name, market_id, category, p_contributed,
        )

    def record_resolution(self, market_id: str, outcome: float) -> None:
        """
        Update outcome for every contribution tied to *market_id*,
        then trigger a full weight recalibration.
        """
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute(
            """
            UPDATE source_contributions
               SET outcome = ?, resolved_at = ?
             WHERE market_id = ? AND outcome IS NULL
            """,
            (outcome, now, market_id),
        )
        n_updated = cur.rowcount
        self.conn.commit()

        if n_updated == 0:
            log.warning("record_resolution: no unresolved contributions for market %s", market_id)
            return

        log.info(
            "resolved market %s (outcome=%.1f) — %d contributions updated",
            market_id, outcome, n_updated,
        )
        self._recalibrate_weights()

    # ── recalibration engine ──────────────────────────────────────────────

    def _recalibrate_weights(self) -> None:
        """
        Recompute Brier-based weights for every (source, category) pair.

        Formula:  weight_raw = 1 / (brier + 1e-6)
        Normalisation: per-category so weights sum to 1.
        Disable rule: brier > 0.30 → weight = 0, enabled = 0.
        """
        rows = self.conn.execute(
            """
            SELECT source_name, category, p_contributed, outcome
              FROM source_contributions
             WHERE outcome IS NOT NULL
            """
        ).fetchall()

        if not rows:
            log.info("recalibrate: no resolved contributions yet")
            return

        # group (source, category) → list of (p, outcome)
        groups: dict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)
        for source_name, category, p_contributed, outcome in rows:
            groups[(source_name, category)].append((p_contributed, outcome))

        # compute Brier per (source, category)
        brier_map: dict[tuple[str, str], float] = {}
        n_map: dict[tuple[str, str], int] = {}
        for key, pairs in groups.items():
            preds = np.array([p for p, _ in pairs])
            outcomes = np.array([o for _, o in pairs])
            brier = float(np.mean((preds - outcomes) ** 2))
            brier_map[key] = brier
            n_map[key] = len(pairs)

        # raw weights per category (for normalisation)
        cat_raw: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for (source, category), brier in brier_map.items():
            if brier > 0.30:
                raw_w = 0.0
            else:
                raw_w = 1.0 / (brier + 1e-6)
            cat_raw[category].append((source, raw_w))

        # normalise & persist
        now = datetime.now(timezone.utc).isoformat()
        for category, entries in cat_raw.items():
            total = sum(w for _, w in entries)
            for source, raw_w in entries:
                norm_w = raw_w / total if total > 0 else 0.0
                brier = brier_map[(source, category)]
                enabled = 1 if brier <= 0.30 else 0
                n = n_map[(source, category)]

                self.conn.execute(
                    """
                    INSERT INTO source_weights
                        (source_name, category, weight, brier, n_samples, enabled, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_name, category) DO UPDATE SET
                        weight     = excluded.weight,
                        brier      = excluded.brier,
                        n_samples  = excluded.n_samples,
                        enabled    = excluded.enabled,
                        updated_at = excluded.updated_at
                    """,
                    (source, category, norm_w, brier, n, enabled, now),
                )

                status = "ENABLED" if enabled else "DISABLED"
                log.info(
                    "weight[%s][%s] = %.4f  (brier=%.4f, n=%d) %s",
                    source, category, norm_w, brier, n, status,
                )

        self.conn.commit()
        log.info("recalibration complete — %d (source, category) pairs updated", len(brier_map))

    # ── lookups ───────────────────────────────────────────────────────────

    def get_weight(self, source_name: str, category: str) -> float:
        """Return current normalised weight for a source in a category (default 1.0)."""
        row = self.conn.execute(
            """
            SELECT weight, enabled FROM source_weights
             WHERE source_name = ? AND category = ?
            """,
            (source_name, category),
        ).fetchone()

        if row is None:
            return 1.0
        weight, enabled = row
        return float(weight) if enabled else 0.0

    def get_performance_report(self) -> dict:
        """
        Return a report keyed by category, each containing a list of
        source performance dicts.

        Example::

            {
                "crypto": [
                    {"source": "binance", "weight": 0.62, "brier": 0.08,
                     "n_samples": 42, "enabled": True},
                    ...
                ],
                ...
            }
        """
        rows = self.conn.execute(
            """
            SELECT source_name, category, weight, brier, n_samples, enabled, updated_at
              FROM source_weights
             ORDER BY category, weight DESC
            """
        ).fetchall()

        report: dict[str, list[dict]] = defaultdict(list)
        for source_name, category, weight, brier, n_samples, enabled, updated_at in rows:
            report[category].append({
                "source": source_name,
                "weight": round(weight, 6),
                "brier": round(brier, 6) if brier is not None else None,
                "n_samples": n_samples,
                "enabled": bool(enabled),
                "updated_at": updated_at,
            })

        return dict(report)
