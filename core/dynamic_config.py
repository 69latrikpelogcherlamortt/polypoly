import sqlite3
import logging
from datetime import datetime, timezone

log = logging.getLogger("dynamic_config")

DEFAULT_PARAMS = {
    "BAYESIAN_HARD_CAP": 0.15,
    "Z_SCORE_THRESHOLD_macro_fed": 1.5,
    "Z_SCORE_THRESHOLD_crypto": 1.5,
    "Z_SCORE_THRESHOLD_politics": 1.8,
    "Z_SCORE_THRESHOLD_sports": 2.0,
    "Z_SCORE_THRESHOLD_other": 1.5,
    "KELLY_FRACTION_FAVORI": 0.25,
    "KELLY_FRACTION_LONGSHOT": 0.125,
}

# Bounds validation: (min, max) for each parameter prefix
PARAM_BOUNDS: dict[str, tuple[float, float]] = {
    "BAYESIAN_HARD_CAP":  (0.05, 0.25),
    "Z_SCORE_THRESHOLD":  (0.5, 4.0),
    "KELLY_FRACTION":     (0.01, 0.50),
}


class DynamicConfig:
    def __init__(self, db_conn: sqlite3.Connection):
        self.db_conn = db_conn
        self._cache: dict[str, float] = {}
        self._ensure_table()
        self._load_defaults()

    def _ensure_table(self):
        self.db_conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dynamic_config (
                param TEXT PRIMARY KEY,
                value REAL,
                updated_at TEXT,
                reason TEXT
            )
            """
        )
        self.db_conn.commit()

    def _load_defaults(self):
        """Insert default values for any param not already in the DB, then populate cache."""
        now = datetime.now(timezone.utc).isoformat()
        for param, value in DEFAULT_PARAMS.items():
            self.db_conn.execute(
                """
                INSERT OR IGNORE INTO dynamic_config (param, value, updated_at, reason)
                VALUES (?, ?, ?, ?)
                """,
                (param, value, now, "default"),
            )
        self.db_conn.commit()

        # Populate cache from DB (DB is source of truth)
        rows = self.db_conn.execute(
            "SELECT param, value FROM dynamic_config"
        ).fetchall()
        for param, value in rows:
            self._cache[param] = value
        log.info("DynamicConfig loaded %d parameters", len(self._cache))

    def get(self, param: str) -> float:
        """Return the current value for *param*, using the in-memory cache."""
        if param in self._cache:
            return self._cache[param]
        row = self.db_conn.execute(
            "SELECT value FROM dynamic_config WHERE param = ?", (param,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown dynamic config param: {param}")
        self._cache[param] = row[0]
        return row[0]

    def _validate_bounds(self, param: str, value: float) -> float:
        """Clamp value to allowed bounds. Logs a warning if clamped."""
        for prefix, (lo, hi) in PARAM_BOUNDS.items():
            if param.startswith(prefix):
                clamped = max(lo, min(hi, value))
                if clamped != value:
                    log.warning(
                        "DynamicConfig: %s=%.4f clamped to [%.2f, %.2f] → %.4f",
                        param, value, lo, hi, clamped,
                    )
                return clamped
        return value

    def set(self, param: str, value: float, reason: str):
        """Persist a new value for *param* and update the cache."""
        value = self._validate_bounds(param, value)
        now = datetime.now(timezone.utc).isoformat()
        self.db_conn.execute(
            """
            INSERT INTO dynamic_config (param, value, updated_at, reason)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(param) DO UPDATE SET value = excluded.value,
                                             updated_at = excluded.updated_at,
                                             reason = excluded.reason
            """,
            (param, value, now, reason),
        )
        self.db_conn.commit()
        self._cache[param] = value
        log.info("Set %s = %.4f  reason=%s", param, value, reason)

    def get_z_score_threshold(self, category: str) -> float:
        """Return the Z-score threshold for the given market category."""
        param = f"Z_SCORE_THRESHOLD_{category}"
        try:
            return self.get(param)
        except KeyError:
            log.warning(
                "No Z_SCORE_THRESHOLD for category '%s', falling back to 'other'",
                category,
            )
            return self.get("Z_SCORE_THRESHOLD_other")

    def set_z_score_threshold(self, category: str, value: float, reason: str):
        """Persist a new Z-score threshold for the given market category."""
        param = f"Z_SCORE_THRESHOLD_{category}"
        self.set(param, value, reason)
