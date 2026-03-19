"""Tests for AlertManager multi-level alerting."""
import pytest
import asyncio
from unittest.mock import AsyncMock
from core.alerting import AlertManager, AlertLevel


class TestAlertManager:

    @pytest.fixture
    def manager(self):
        send_fn = AsyncMock()
        return AlertManager(send_fn=send_fn), send_fn

    def test_info_does_not_send_telegram(self, manager):
        mgr, send_fn = manager
        asyncio.get_event_loop().run_until_complete(
            mgr.send(AlertLevel.INFO, "Test", "body")
        )
        send_fn.assert_not_awaited()

    def test_critical_sends_telegram(self, manager):
        mgr, send_fn = manager
        asyncio.get_event_loop().run_until_complete(
            mgr.send(AlertLevel.CRITICAL, "Kill Switch", "MDD exceeded")
        )
        send_fn.assert_awaited_once()

    def test_emergency_sends_telegram(self, manager):
        mgr, send_fn = manager
        asyncio.get_event_loop().run_until_complete(
            mgr.send(AlertLevel.EMERGENCY, "CRASH", "Unhandled error")
        )
        send_fn.assert_awaited_once()

    def test_warning_rate_limited(self, manager):
        mgr, send_fn = manager
        loop = asyncio.get_event_loop()
        # First warning sends
        loop.run_until_complete(mgr.send(AlertLevel.WARNING, "Slow", "body"))
        assert send_fn.await_count == 1
        # Second same warning within 5min → blocked
        loop.run_until_complete(mgr.send(AlertLevel.WARNING, "Slow", "body"))
        assert send_fn.await_count == 1  # Still 1, not 2

    def test_different_warnings_both_send(self, manager):
        mgr, send_fn = manager
        loop = asyncio.get_event_loop()
        loop.run_until_complete(mgr.send(AlertLevel.WARNING, "Slow A", "body"))
        loop.run_until_complete(mgr.send(AlertLevel.WARNING, "Slow B", "body"))
        assert send_fn.await_count == 2
