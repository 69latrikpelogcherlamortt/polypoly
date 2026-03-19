"""
Active health monitoring with auto-recovery.

Periodically checks critical components (database, external services, …),
tracks failure counts, and triggers alerts when recovery attempts are
exhausted.
"""

import asyncio
import logging
import time
from enum import Enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Latency budgets (seconds) for each processing stage
# ---------------------------------------------------------------------------

LATENCY_BUDGETS: Dict[str, float] = {
    "signal_fetch": 25.0,
    "bayes_update": 2.0,
    "gate_validation": 0.5,
    "order_submission": 5.0,
    "portfolio_risk_calc": 1.0,
    "db_write": 0.5,
}


# ---------------------------------------------------------------------------
# Component health states
# ---------------------------------------------------------------------------

class ComponentHealth(Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILED = "failed"
    RECOVERING = "recovering"


# ---------------------------------------------------------------------------
# Internal bookkeeping per component
# ---------------------------------------------------------------------------

@dataclass
class _ComponentState:
    health: ComponentHealth = ComponentHealth.HEALTHY
    consecutive_failures: int = 0
    recovery_attempts: int = 0
    last_check: Optional[datetime] = None
    last_success: Optional[datetime] = None
    last_failure: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Health monitor
# ---------------------------------------------------------------------------

class HealthMonitor:
    """Monitors system components and triggers alerts on persistent failure.

    Parameters
    ----------
    db_conn
        An async-compatible database connection that supports
        ``await db_conn.execute(query)``.
    alert_fn
        Optional async or sync callable invoked with ``(component_name, state)``
        when a component exceeds *MAX_RECOVERY_ATTEMPTS*.
    """

    CHECK_INTERVAL: int = 60  # seconds between check cycles
    MAX_RECOVERY_ATTEMPTS: int = 3

    def __init__(
        self,
        db_conn: Any,
        alert_fn: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.db_conn = db_conn
        self.alert_fn = alert_fn
        self._states: Dict[str, _ComponentState] = {}

    # -- helpers ----------------------------------------------------------

    def _get_state(self, name: str) -> _ComponentState:
        if name not in self._states:
            self._states[name] = _ComponentState()
        return self._states[name]

    # -- public API -------------------------------------------------------

    async def check_component(
        self,
        name: str,
        check_fn: Callable[[], Awaitable[bool]],
    ) -> ComponentHealth:
        """Run *check_fn* and update the health state for *name*.

        *check_fn* should be an async callable returning ``True`` on success
        or raising / returning ``False`` on failure.
        """
        state = self._get_state(name)
        state.last_check = datetime.now(timezone.utc)

        try:
            result = await check_fn()
            if result:
                self.record_success(name)
            else:
                self.record_failure(name)
                await self._handle_unhealthy(name)
        except Exception as exc:
            logger.warning("Health check %r raised: %s", name, exc)
            self.record_failure(name)
            await self._handle_unhealthy(name)

        return state.health

    async def run_checks(self) -> Dict[str, ComponentHealth]:
        """Run built-in checks (currently: database) and return a health dict."""

        # --- database check ---
        async def _db_check() -> bool:
            await self.db_conn.execute("SELECT 1")
            return True

        await self.check_component("database", _db_check)

        return {name: st.health for name, st in self._states.items()}

    async def _handle_unhealthy(self, name: str) -> None:
        """Increment recovery counter and alert if max attempts exceeded."""
        state = self._get_state(name)
        state.recovery_attempts += 1

        if state.recovery_attempts >= self.MAX_RECOVERY_ATTEMPTS:
            state.health = ComponentHealth.FAILED
            logger.error(
                "Component %r FAILED after %d recovery attempts",
                name,
                state.recovery_attempts,
            )
            if self.alert_fn is not None:
                try:
                    result = self.alert_fn(name, state.health)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as exc:
                    logger.error("Alert function raised: %s", exc)
        else:
            state.health = ComponentHealth.RECOVERING
            logger.warning(
                "Component %r recovering (attempt %d/%d)",
                name,
                state.recovery_attempts,
                self.MAX_RECOVERY_ATTEMPTS,
            )

    def record_success(self, name: str) -> None:
        """Record a successful operation for *name*, resetting failure counters."""
        state = self._get_state(name)
        state.health = ComponentHealth.HEALTHY
        state.consecutive_failures = 0
        state.recovery_attempts = 0
        state.last_success = datetime.now(timezone.utc)

    def record_failure(self, name: str) -> None:
        """Record a failed operation for *name*."""
        state = self._get_state(name)
        state.consecutive_failures += 1
        state.last_failure = datetime.now(timezone.utc)

        if state.health == ComponentHealth.HEALTHY:
            state.health = ComponentHealth.DEGRADED

    def get_health_summary(self) -> dict:
        """Return a snapshot of every tracked component's state.

        Returns a dict keyed by component name with sub-dicts containing
        ``health``, ``consecutive_failures``, ``recovery_attempts``,
        ``last_success``, and ``last_failure``.
        """
        summary: Dict[str, dict] = {}
        for name, state in self._states.items():
            summary[name] = {
                "health": state.health,
                "consecutive_failures": state.consecutive_failures,
                "recovery_attempts": state.recovery_attempts,
                "last_success": state.last_success,
                "last_failure": state.last_failure,
                "last_check": state.last_check,
            }
        return summary
