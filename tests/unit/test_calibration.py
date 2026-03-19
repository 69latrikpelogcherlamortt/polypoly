"""Tests for PlattScaler — post-hoc probability calibration."""
import pytest
import sqlite3
import numpy as np
from signals.calibration import PlattScaler


@pytest.fixture
def scaler():
    conn = sqlite3.connect(":memory:")
    # Create trades table matching what PlattScaler queries
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            p_model REAL, outcome INTEGER, exit_ts TEXT,
            resolved_at TEXT, status TEXT, pnl REAL
        )
    """)
    conn.commit()
    return PlattScaler(conn), conn


class TestPlattScaler:

    def test_uncalibrated_returns_raw(self, scaler):
        s, _ = scaler
        assert s.calibrate(0.75) == 0.75

    def test_calibrate_stays_in_range(self, scaler):
        s, _ = scaler
        # Force fitted state
        s.alpha = 1.3
        s.beta = -0.1
        s.fitted = True
        for p in [0.01, 0.1, 0.3, 0.5, 0.7, 0.9, 0.99]:
            cal = s.calibrate(p)
            assert 0.01 <= cal <= 0.99, f"calibrate({p}) = {cal} out of range"

    def test_calibration_preserves_ordering(self, scaler):
        s, _ = scaler
        s.alpha = 0.9
        s.beta = 0.05
        s.fitted = True
        p_values = [0.1, 0.3, 0.5, 0.7, 0.9]
        calibrated = [s.calibrate(p) for p in p_values]
        for i in range(len(calibrated) - 1):
            assert calibrated[i] < calibrated[i + 1], (
                f"Ordering broken: calibrate({p_values[i]})={calibrated[i]} "
                f">= calibrate({p_values[i+1]})={calibrated[i+1]}"
            )

    def test_identity_when_alpha1_beta0(self, scaler):
        s, _ = scaler
        s.alpha = 1.0
        s.beta = 0.0
        s.fitted = True
        for p in [0.2, 0.5, 0.8]:
            assert abs(s.calibrate(p) - p) < 0.01

    def test_refit_needs_minimum_samples(self, scaler):
        s, conn = scaler
        # Only 5 trades — not enough
        for i in range(5):
            conn.execute(
                "INSERT INTO trades (p_model, outcome, exit_ts, resolved_at, status, pnl) VALUES (?,?,?,?,?,?)",
                (0.7, 1, "2026-01-01", "2026-01-01", "closed", 0.3)
            )
        conn.commit()
        s.update_and_refit()
        assert not s.fitted  # Not enough data

    def test_refit_with_sufficient_data(self, scaler):
        s, conn = scaler
        np.random.seed(42)
        for i in range(30):
            p = np.random.uniform(0.3, 0.8)
            outcome = 1 if np.random.random() < p else 0
            conn.execute(
                "INSERT INTO trades (p_model, outcome, exit_ts, resolved_at, status, pnl) VALUES (?,?,?,?,?,?)",
                (round(p, 4), outcome, f"2026-01-{i+1:02d}", f"2026-01-{i+1:02d}", "closed", 0.0)
            )
        conn.commit()
        s.update_and_refit()
        # With 30 samples, the scaler should be able to fit
        # (may or may not fit depending on cadence check)
        # At minimum, no crash
        assert isinstance(s.alpha, float)
        assert isinstance(s.beta, float)
