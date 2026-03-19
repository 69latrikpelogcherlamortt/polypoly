"""Orphan order reconciliation system.

Detects and reconciles orphaned orders, stale pending orders,
and partially filled orders on startup.
"""

import asyncio
import hashlib
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger("reconciliation")


@dataclass
class ReconciliationReport:
    """Result of a reconciliation pass."""

    orphaned_orders: list[str] = field(default_factory=list)
    stale_pending: list[str] = field(default_factory=list)
    partially_filled: list[dict] = field(default_factory=list)
    reconciled_at: str = ""


class OrderDeduplicator:
    """Prevents duplicate order submissions using an idempotency key table."""

    def __init__(self, db_conn: sqlite3.Connection) -> None:
        self.db_conn = db_conn
        self._ensure_table()

    def _ensure_table(self) -> None:
        self.db_conn.execute(
            """
            CREATE TABLE IF NOT EXISTS order_dedup (
                key TEXT PRIMARY KEY,
                order_id TEXT,
                created_at TEXT,
                status TEXT DEFAULT 'PENDING'
            )
            """
        )
        self.db_conn.commit()

    def is_duplicate(self, key: str) -> bool:
        """Return True if an order with this idempotency key already exists."""
        cursor = self.db_conn.execute(
            "SELECT 1 FROM order_dedup WHERE key = ?", (key,)
        )
        return cursor.fetchone() is not None

    def record(self, key: str, order_id: str, status: str) -> None:
        """Record an order submission with its idempotency key."""
        now = datetime.now(timezone.utc).isoformat()
        self.db_conn.execute(
            """
            INSERT INTO order_dedup (key, order_id, created_at, status)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                order_id = excluded.order_id,
                status = excluded.status
            """,
            (key, order_id, now, status),
        )
        self.db_conn.commit()

    @staticmethod
    def generate_idempotency_key(
        market_id: str, side: str, price: float, size: float
    ) -> str:
        """Generate a deterministic idempotency key for an order.

        The key is stable within the same clock-minute so that rapid
        retries within a single minute are de-duplicated.
        """
        timestamp_minute = int(time.time() // 60)
        raw = f"{market_id}:{side}:{price:.6f}:{size:.6f}:{timestamp_minute}"
        return hashlib.sha256(raw.encode()).hexdigest()


async def reconcile_on_startup(
    clob_client: Optional[Any],
    db_conn: sqlite3.Connection,
    log: logging.Logger,
) -> ReconciliationReport:
    """Reconcile local order state against the CLOB on startup.

    Steps:
      1. Load pending orders from the local ``trades`` table.
      2. Fetch active orders from the CLOB (if client available).
      3. Mark stale pending orders (in DB but not on CLOB) as failed.
      4. Log orphan orders (on CLOB but not in DB).

    Returns a :class:`ReconciliationReport` summarising what was found.
    """
    report = ReconciliationReport(
        reconciled_at=datetime.now(timezone.utc).isoformat(),
    )

    # ------------------------------------------------------------------
    # 1. Get locally-tracked pending orders
    # ------------------------------------------------------------------
    cursor = db_conn.execute(
        "SELECT order_id FROM trades WHERE status = 'open' AND order_id IS NOT NULL"
    )
    local_pending: dict[str, bool] = {row[0]: True for row in cursor.fetchall()}
    log.info("Found %d pending orders in local DB", len(local_pending))

    # ------------------------------------------------------------------
    # 2. Fetch active orders from the CLOB
    # ------------------------------------------------------------------
    clob_order_ids: set[str] = set()

    if clob_client is not None:
        try:
            active_orders = await asyncio.to_thread(clob_client.get_orders)
            if active_orders:
                for order in active_orders:
                    order_id = (
                        order.get("id") or order.get("order_id") or ""
                    )
                    if order_id:
                        clob_order_ids.add(order_id)

                        # Check for partially filled orders
                        size_matched = float(order.get("size_matched", 0))
                        original_size = float(order.get("original_size", 0))
                        if 0 < size_matched < original_size:
                            report.partially_filled.append(
                                {
                                    "order_id": order_id,
                                    "size_matched": size_matched,
                                    "original_size": original_size,
                                }
                            )

            log.info("Found %d active orders on CLOB", len(clob_order_ids))
        except Exception as exc:
            log.error("Failed to fetch active orders from CLOB: %s", exc)

    # ------------------------------------------------------------------
    # 3. Mark stale pending orders as failed
    # ------------------------------------------------------------------
    for order_id in local_pending:
        if order_id not in clob_order_ids:
            report.stale_pending.append(order_id)
            try:
                db_conn.execute(
                    "UPDATE trades SET status = 'failed' WHERE order_id = ?",
                    (order_id,),
                )
            except Exception as exc:
                log.error(
                    "Failed to mark stale order %s as failed: %s",
                    order_id,
                    exc,
                )
    if report.stale_pending:
        db_conn.commit()
        log.warning(
            "Marked %d stale pending orders as failed: %s",
            len(report.stale_pending),
            report.stale_pending,
        )

    # ------------------------------------------------------------------
    # 4. Log orphan orders (on CLOB but not tracked locally)
    # ------------------------------------------------------------------
    for order_id in clob_order_ids:
        if order_id not in local_pending:
            report.orphaned_orders.append(order_id)

    if report.orphaned_orders:
        log.warning(
            "Found %d orphan orders on CLOB not tracked in DB: %s",
            len(report.orphaned_orders),
            report.orphaned_orders,
        )

    if report.partially_filled:
        log.info(
            "Found %d partially filled orders", len(report.partially_filled)
        )

    log.info("Reconciliation complete at %s", report.reconciled_at)
    return report
