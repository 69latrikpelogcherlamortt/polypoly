import sqlite3
import logging
import numpy as np
from dataclasses import dataclass
from collections import defaultdict
from datetime import datetime, timezone

from core.dynamic_config import DynamicConfig

log = logging.getLogger("self_improvement")


@dataclass
class ResolutionAnalysis:
    market_id: str
    question: str
    category: str
    p_model_entry: float
    p_market_entry: float
    outcome: float          # 1.0 for YES, 0.0 for NO
    trade_pnl: float

    @property
    def model_error(self) -> float:
        """Signed error: positive means the model was overconfident."""
        return self.p_model_entry - self.outcome

    @property
    def edge_was_real(self) -> bool:
        """True when the model's directional edge materialised profitably."""
        return self.trade_pnl > 0

    @property
    def brier_contribution(self) -> float:
        return (self.p_model_entry - self.outcome) ** 2


class SelfImprovementEngine:
    ANALYSIS_WINDOW = 20
    MIN_PATTERN_OCCURRENCES = 5

    def __init__(self, db_conn: sqlite3.Connection, dynamic_config: DynamicConfig):
        self.db_conn = db_conn
        self.dynamic_config = dynamic_config
        self._ensure_table()

    def _ensure_table(self):
        self.db_conn.execute(
            """
            CREATE TABLE IF NOT EXISTS resolution_analyses (
                market_id TEXT PRIMARY KEY,
                question TEXT,
                category TEXT,
                p_model_entry REAL,
                p_market_entry REAL,
                outcome REAL,
                trade_pnl REAL,
                model_error REAL,
                edge_was_real INTEGER,
                brier_contribution REAL,
                analyzed_at TEXT
            )
            """
        )
        self.db_conn.commit()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze_after_resolution(
        self, market_id: str, outcome: float
    ) -> ResolutionAnalysis | None:
        """Create a ResolutionAnalysis for *market_id* and trigger pattern detection."""
        row = self.db_conn.execute(
            """
            SELECT market_id, question, category, p_model_entry, p_market_entry, trade_pnl
            FROM trades
            WHERE market_id = ?
            """,
            (market_id,),
        ).fetchone()

        if row is None:
            log.warning("No trade record found for market %s", market_id)
            return None

        analysis = ResolutionAnalysis(
            market_id=row[0],
            question=row[1],
            category=row[2],
            p_model_entry=row[3],
            p_market_entry=row[4],
            outcome=outcome,
            trade_pnl=row[5],
        )

        now = datetime.now(timezone.utc).isoformat()
        self.db_conn.execute(
            """
            INSERT OR REPLACE INTO resolution_analyses
                (market_id, question, category, p_model_entry, p_market_entry,
                 outcome, trade_pnl, model_error, edge_was_real, brier_contribution, analyzed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                analysis.market_id,
                analysis.question,
                analysis.category,
                analysis.p_model_entry,
                analysis.p_market_entry,
                analysis.outcome,
                analysis.trade_pnl,
                analysis.model_error,
                int(analysis.edge_was_real),
                analysis.brier_contribution,
                now,
            ),
        )
        self.db_conn.commit()
        log.info(
            "Resolution analysis for %s: error=%.3f edge_real=%s brier=%.4f pnl=%.4f",
            market_id,
            analysis.model_error,
            analysis.edge_was_real,
            analysis.brier_contribution,
            analysis.trade_pnl,
        )

        self._detect_and_respond_to_patterns()
        return analysis

    # ------------------------------------------------------------------
    # Pattern detection
    # ------------------------------------------------------------------

    def _detect_and_respond_to_patterns(self):
        """Load the most recent analyses and auto-adjust config when patterns emerge."""
        rows = self.db_conn.execute(
            """
            SELECT category, model_error, edge_was_real, brier_contribution
            FROM resolution_analyses
            ORDER BY analyzed_at DESC
            LIMIT ?
            """,
            (self.ANALYSIS_WINDOW,),
        ).fetchall()

        if len(rows) < self.MIN_PATTERN_OCCURRENCES:
            log.debug(
                "Only %d analyses available, need %d — skipping pattern detection",
                len(rows),
                self.MIN_PATTERN_OCCURRENCES,
            )
            return

        # Aggregate across all recent analyses
        all_errors = np.array([r[1] for r in rows])
        all_edge_real = np.array([r[2] for r in rows])
        all_brier = np.array([r[3] for r in rows])

        mean_error = float(np.mean(all_errors))
        edge_real_rate = float(np.mean(all_edge_real))
        mean_brier = float(np.mean(all_brier))

        # --- Pattern 1: Systematic overconfidence ---
        if mean_error > 0.08:
            current_cap = self.dynamic_config.get("BAYESIAN_HARD_CAP")
            new_cap = max(0.08, current_cap - 0.02)
            if new_cap < current_cap:
                self.dynamic_config.set(
                    "BAYESIAN_HARD_CAP",
                    new_cap,
                    f"auto: systematic overconfidence detected (mean_error={mean_error:.3f})",
                )
                log.warning(
                    "Pattern 1 triggered: mean_error=%.3f — reduced BAYESIAN_HARD_CAP %.3f -> %.3f",
                    mean_error,
                    current_cap,
                    new_cap,
                )

        # --- Per-category analysis for Patterns 2 & 3 ---
        by_category: dict[str, list] = defaultdict(list)
        for category, model_error, edge_real, brier in rows:
            by_category[category].append((model_error, edge_real, brier))

        for category, entries in by_category.items():
            if len(entries) < self.MIN_PATTERN_OCCURRENCES:
                continue

            cat_edge_real = np.array([e[1] for e in entries])
            cat_brier = np.array([e[2] for e in entries])
            cat_edge_rate = float(np.mean(cat_edge_real))
            cat_mean_brier = float(np.mean(cat_brier))

            # --- Pattern 2: Low edge-realisation for a category ---
            if cat_edge_rate < 0.40:
                current_z = self.dynamic_config.get_z_score_threshold(category)
                new_z = min(3.0, current_z + 0.5)
                if new_z > current_z:
                    self.dynamic_config.set_z_score_threshold(
                        category,
                        new_z,
                        f"auto: low edge realisation in '{category}' "
                        f"(rate={cat_edge_rate:.2f})",
                    )
                    log.warning(
                        "Pattern 2 triggered [%s]: edge_real_rate=%.2f — "
                        "raised Z_SCORE_THRESHOLD %.2f -> %.2f",
                        category,
                        cat_edge_rate,
                        current_z,
                        new_z,
                    )

            # --- Pattern 3: Excellent performance ---
            if cat_edge_rate > 0.65 and cat_mean_brier < 0.15:
                log.info(
                    "Pattern 3 [%s]: excellent performance — "
                    "edge_real_rate=%.2f, brier=%.4f",
                    category,
                    cat_edge_rate,
                    cat_mean_brier,
                )

    # ------------------------------------------------------------------
    # Weekly report
    # ------------------------------------------------------------------

    def generate_weekly_report(self) -> dict:
        """Return performance stats by category and a list of recent auto-adjustments."""
        rows = self.db_conn.execute(
            """
            SELECT category, model_error, edge_was_real, brier_contribution, trade_pnl
            FROM resolution_analyses
            ORDER BY analyzed_at DESC
            LIMIT ?
            """,
            (self.ANALYSIS_WINDOW,),
        ).fetchall()

        by_category: dict[str, list] = defaultdict(list)
        for category, model_error, edge_real, brier, pnl in rows:
            by_category[category].append(
                {
                    "model_error": model_error,
                    "edge_was_real": bool(edge_real),
                    "brier": brier,
                    "pnl": pnl,
                }
            )

        category_stats = {}
        for category, entries in by_category.items():
            errors = [e["model_error"] for e in entries]
            briers = [e["brier"] for e in entries]
            pnls = [e["pnl"] for e in entries]
            edge_reals = [e["edge_was_real"] for e in entries]

            category_stats[category] = {
                "count": len(entries),
                "mean_model_error": float(np.mean(errors)),
                "mean_brier": float(np.mean(briers)),
                "total_pnl": float(np.sum(pnls)),
                "edge_real_rate": float(np.mean(edge_reals)),
                "current_z_threshold": self.dynamic_config.get_z_score_threshold(
                    category
                ),
            }

        # Gather recent auto-adjustments from dynamic_config table
        adjustments = self.db_conn.execute(
            """
            SELECT param, value, updated_at, reason
            FROM dynamic_config
            WHERE reason LIKE 'auto:%'
            ORDER BY updated_at DESC
            """
        ).fetchall()

        adjustment_list = [
            {
                "param": a[0],
                "value": a[1],
                "updated_at": a[2],
                "reason": a[3],
            }
            for a in adjustments
        ]

        report = {
            "analysis_window": self.ANALYSIS_WINDOW,
            "total_analyses": len(rows),
            "category_stats": category_stats,
            "auto_adjustments": adjustment_list,
        }

        log.info("Weekly report generated: %d analyses across %d categories",
                 len(rows), len(category_stats))
        return report
