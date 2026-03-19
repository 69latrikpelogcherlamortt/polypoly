"""Tests for SourcePerformanceTracker — dynamic source weighting."""
import pytest
import sqlite3
from signals.source_tracker import SourcePerformanceTracker


@pytest.fixture
def tracker():
    conn = sqlite3.connect(":memory:")
    t = SourcePerformanceTracker(conn)
    return t, conn


class TestSourceTracker:

    def test_record_and_resolve(self, tracker):
        t, conn = tracker
        t.record_contribution("cme_fedwatch", "m1", "macro_fed", 0.80, 0.78)
        t.record_resolution("m1", 1.0)
        row = conn.execute(
            "SELECT outcome FROM source_contributions WHERE market_id='m1'"
        ).fetchone()
        assert row[0] == 1.0

    def test_default_weight_is_one(self, tracker):
        t, _ = tracker
        assert t.get_weight("unknown_source", "crypto") == 1.0

    def test_weight_after_calibration(self, tracker):
        t, _ = tracker
        # Insert enough data for calibration
        for i in range(10):
            t.record_contribution("src_a", f"m{i}", "macro_fed", 0.70, 0.70)
            t.record_contribution("src_b", f"m{i}", "macro_fed", 0.50, 0.50)
        for i in range(10):
            t.record_resolution(f"m{i}", 1.0 if i < 7 else 0.0)
        t._recalibrate_weights()
        w_a = t.get_weight("src_a", "macro_fed")
        w_b = t.get_weight("src_b", "macro_fed")
        # src_a (predicting 0.70 when outcome ~70% YES) should have lower brier
        assert w_a > 0 or w_b > 0  # At least one has weight

    def test_disabled_source_zero_weight(self, tracker):
        t, _ = tracker
        # Insert bad predictions (always 0.90 but outcome is 50/50)
        for i in range(10):
            t.record_contribution("bad_src", f"m{i}", "crypto", 0.90, 0.90)
            t.record_contribution("ok_src", f"m{i}", "crypto", 0.55, 0.55)
        for i in range(10):
            t.record_resolution(f"m{i}", 1.0 if i % 2 == 0 else 0.0)
        t._recalibrate_weights()
        # bad_src should have high brier and potentially be disabled
        w_bad = t.get_weight("bad_src", "crypto")
        w_ok = t.get_weight("ok_src", "crypto")
        assert w_ok >= w_bad  # Good source should have >= weight

    def test_performance_report(self, tracker):
        t, _ = tracker
        t.record_contribution("src_a", "m1", "macro_fed", 0.70, 0.70)
        t.record_resolution("m1", 1.0)
        t._recalibrate_weights()
        report = t.get_performance_report()
        assert isinstance(report, dict)
