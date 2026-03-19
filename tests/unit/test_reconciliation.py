"""Tests for order reconciliation and idempotency."""
import pytest
import sqlite3
from pathlib import Path
from trading.reconciliation import (
    OrderDeduplicator, ReconciliationReport,
)

# generate_idempotency_key is a static method of OrderDeduplicator
generate_idempotency_key = OrderDeduplicator.generate_idempotency_key


class TestIdempotencyKey:

    def test_deterministic(self):
        k1 = generate_idempotency_key("m1", "BUY", 0.72, 3.5)
        k2 = generate_idempotency_key("m1", "BUY", 0.72, 3.5)
        assert k1 == k2  # same inputs within same minute → same key

    def test_different_inputs_different_keys(self):
        k1 = generate_idempotency_key("m1", "BUY", 0.72, 3.5)
        k2 = generate_idempotency_key("m2", "BUY", 0.72, 3.5)
        assert k1 != k2

    def test_key_length(self):
        k = generate_idempotency_key("m1", "BUY", 0.50, 1.0)
        assert len(k) in (32, 64)  # sha256 hex = 64 chars (or truncated to 32)


class TestOrderDeduplicator:

    @pytest.fixture
    def dedup(self):
        conn = sqlite3.connect(":memory:")
        return OrderDeduplicator(conn)

    def test_first_submission_allowed(self, dedup):
        assert not dedup.is_duplicate("key1")

    def test_records_and_detects_duplicate(self, dedup):
        dedup.record("key1", "order-001", "PENDING")
        assert dedup.is_duplicate("key1")

    def test_recorded_order_is_duplicate(self, dedup):
        dedup.record("key1", "order-001", "FAILED")
        # Implementation tracks all submissions regardless of status
        # A FAILED order still exists in the dedup table
        assert dedup.is_duplicate("key1")

    def test_different_keys_independent(self, dedup):
        dedup.record("key1", "order-001", "PENDING")
        assert not dedup.is_duplicate("key2")


class TestReconciliationReport:

    def test_empty_report(self):
        report = ReconciliationReport(
            orphaned_orders=[], stale_pending=[],
            partially_filled=[], reconciled_at="2026-03-19T10:00:00"
        )
        assert len(report.orphaned_orders) == 0
        assert len(report.stale_pending) == 0

    def test_report_with_issues(self):
        report = ReconciliationReport(
            orphaned_orders=["o1", "o2"],
            stale_pending=["p1"],
            partially_filled=[{"order_id": "pf1", "filled": 2.0, "original": 5.0}],
            reconciled_at="2026-03-19T10:00:00"
        )
        assert len(report.orphaned_orders) == 2
        assert len(report.stale_pending) == 1
        assert len(report.partially_filled) == 1
