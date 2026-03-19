"""
calibration.py  ·  Polymarket Trading Bot
──────────────────────────────────────────
Platt Scaling for post-hoc probability calibration.

Maps raw model probabilities through a learned sigmoid:
    p_cal = sigmoid(alpha * logit(p_raw) + beta)

Parameters are fitted via maximum-likelihood (Nelder-Mead) on the
last 100 resolved trades and persisted in SQLite.
"""

from __future__ import annotations

import sqlite3
import logging

import numpy as np
from scipy.special import expit
from scipy.optimize import minimize

log = logging.getLogger("calibration")


def _logit(p: float) -> float:
    """Numerically safe logit, clipping to avoid ±inf."""
    p = np.clip(p, 1e-7, 1 - 1e-7)
    return float(np.log(p / (1 - p)))


def _brier(probs: np.ndarray, outcomes: np.ndarray) -> float:
    return float(np.mean((probs - outcomes) ** 2))


class PlattScaler:
    """
    Post-hoc Platt calibrator backed by SQLite.

    Singleton row (id=1) in ``calibration_params`` stores the current
    alpha / beta.  ``update_and_refit()`` is called after each market
    resolution; it only actually refits every 10 new resolutions and
    requires >= 20 samples.
    """

    def __init__(self, db_conn: sqlite3.Connection):
        self.conn = db_conn
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._create_table()
        self.alpha: float = 1.0
        self.beta: float = 0.0
        self.fitted: bool = False
        self._load_from_db()

    # ── schema ────────────────────────────────────────────────────────────

    def _create_table(self) -> None:
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS calibration_params (
                id         INTEGER PRIMARY KEY CHECK (id = 1),
                alpha      REAL    NOT NULL DEFAULT 1.0,
                beta       REAL    NOT NULL DEFAULT 0.0,
                n_samples  INTEGER NOT NULL DEFAULT 0,
                fitted_at  TEXT,
                brier_raw  REAL,
                brier_cal  REAL
            )
        """)
        # ensure singleton row exists
        self.conn.execute("""
            INSERT OR IGNORE INTO calibration_params (id) VALUES (1)
        """)
        self.conn.commit()
        log.info("calibration_params table ready")

    # ── calibration ───────────────────────────────────────────────────────

    def calibrate(self, p_raw: float) -> float:
        """
        Apply Platt scaling to a raw probability.

        If the model has not been fitted yet, returns *p_raw* unchanged.
        Output is clipped to [0.01, 0.99].
        """
        if not self.fitted:
            return p_raw

        logit_p = _logit(p_raw)
        p_cal = float(expit(self.alpha * logit_p + self.beta))
        p_cal = float(np.clip(p_cal, 0.01, 0.99))
        return p_cal

    # ── refitting ─────────────────────────────────────────────────────────

    def update_and_refit(self) -> None:
        """
        Load the last 100 resolved trades and refit alpha / beta via
        maximum-likelihood (Nelder-Mead).

        Guards:
        - Only refits every 10 new resolutions (compared to last fit).
        - Requires >= 20 resolved samples.
        """
        # load last 100 resolved trades (p_model, outcome)
        rows = self.conn.execute("""
            SELECT p_model, outcome
              FROM trades
             WHERE outcome IS NOT NULL
             ORDER BY resolved_at DESC
             LIMIT 100
        """).fetchall()

        n_available = len(rows)
        if n_available < 20:
            log.info(
                "calibration refit skipped: only %d resolved samples (need >= 20)",
                n_available,
            )
            return

        # check 10-resolution cadence
        prev_n = self.conn.execute(
            "SELECT n_samples FROM calibration_params WHERE id = 1"
        ).fetchone()[0] or 0

        if abs(n_available - prev_n) < 10:
            log.debug(
                "calibration refit skipped: only %d new resolutions since last fit",
                n_available - prev_n,
            )
            return

        probs = np.array([r[0] for r in rows], dtype=np.float64)
        outcomes = np.array([r[1] for r in rows], dtype=np.float64)

        # brier before calibration
        brier_raw = _brier(probs, outcomes)

        # logits of raw probabilities
        logits = np.array([_logit(p) for p in probs])

        # negative log-likelihood objective
        def neg_log_likelihood(params: np.ndarray) -> float:
            a, b = params
            q = expit(a * logits + b)
            q = np.clip(q, 1e-7, 1 - 1e-7)
            ll = outcomes * np.log(q) + (1 - outcomes) * np.log(1 - q)
            return -float(np.sum(ll))

        result = minimize(
            neg_log_likelihood,
            x0=np.array([self.alpha, self.beta]),
            method="Nelder-Mead",
            options={"maxiter": 2000, "xatol": 1e-6, "fatol": 1e-8},
        )

        if not result.success:
            log.warning("calibration optimisation did not converge: %s", result.message)

        alpha_new, beta_new = float(result.x[0]), float(result.x[1])

        # brier after calibration
        p_cal = np.clip(expit(alpha_new * logits + beta_new), 0.01, 0.99)
        brier_cal = _brier(p_cal, outcomes)

        # persist
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()

        self.conn.execute(
            """
            UPDATE calibration_params
               SET alpha     = ?,
                   beta      = ?,
                   n_samples = ?,
                   fitted_at = ?,
                   brier_raw = ?,
                   brier_cal = ?
             WHERE id = 1
            """,
            (alpha_new, beta_new, n_available, now, brier_raw, brier_cal),
        )
        self.conn.commit()

        self.alpha = alpha_new
        self.beta = beta_new
        self.fitted = True

        improvement = brier_raw - brier_cal
        log.info(
            "calibration refit: alpha=%.4f beta=%.4f  "
            "brier_raw=%.4f → brier_cal=%.4f  (improvement=%.4f, n=%d)",
            alpha_new, beta_new, brier_raw, brier_cal, improvement, n_available,
        )

    # ── persistence ───────────────────────────────────────────────────────

    def _load_from_db(self) -> None:
        """Load alpha / beta from the database singleton row."""
        row = self.conn.execute(
            "SELECT alpha, beta, fitted_at FROM calibration_params WHERE id = 1"
        ).fetchone()

        if row is None:
            return

        alpha, beta, fitted_at = row
        self.alpha = float(alpha)
        self.beta = float(beta)
        self.fitted = fitted_at is not None

        if self.fitted:
            log.info(
                "loaded Platt params: alpha=%.4f beta=%.4f (fitted %s)",
                self.alpha, self.beta, fitted_at,
            )
        else:
            log.info("calibration not yet fitted — pass-through mode")
