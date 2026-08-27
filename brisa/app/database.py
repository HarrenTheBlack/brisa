import logging
import sqlite3
import time
from pathlib import Path

from app.sensor_ids import (
    is_legacy_drive_id_candidate,
    migrate_legacy_drive_sensor_id,
)

logger = logging.getLogger(__name__)

DB_PATH = Path("/data/history.db")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create tables and indexes if they don't exist."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS readings (
                ts        INTEGER NOT NULL,
                sensor_id TEXT NOT NULL,
                temp      REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS fan_readings (
                ts         INTEGER NOT NULL,
                fan_id     TEXT NOT NULL,
                percent    INTEGER NOT NULL,
                rpm        REAL
            );

            CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(ts);
            CREATE INDEX IF NOT EXISTS idx_fan_readings_ts ON fan_readings(ts);
        """)
    logger.info("Database initialized at %s", DB_PATH)
    migrate_reading_sensor_ids()


def migrate_reading_sensor_ids() -> int:
    """Transactionally migrate recognized legacy IDs in temperature history."""
    conn = _connect()
    migrated_rows = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        sensor_ids = conn.execute(
            "SELECT DISTINCT sensor_id FROM readings"
        ).fetchall()

        for row in sensor_ids:
            old_id = row["sensor_id"]
            new_id = migrate_legacy_drive_sensor_id(old_id)
            if new_id == old_id:
                if is_legacy_drive_id_candidate(old_id):
                    logger.warning(
                        "Could not safely migrate history sensor ID: %s", old_id
                    )
                continue

            cursor = conn.execute(
                "UPDATE readings SET sensor_id = ? WHERE sensor_id = ?",
                (new_id, old_id),
            )
            migrated_rows += cursor.rowcount
            logger.info("Migrated history sensor ID: %s -> %s", old_id, new_id)

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    if migrated_rows:
        logger.warning("Migrated %d temperature history row(s)", migrated_rows)
    return migrated_rows


def write_cycle_readings(
    ts: int,
    sensor_readings: dict[str, float],
    fan_readings: list[tuple[str, int, float | None]],
) -> None:
    """Persist all history rows from one controller cycle atomically."""
    with _connect() as conn:
        conn.executemany(
            "INSERT INTO readings (ts, sensor_id, temp) VALUES (?, ?, ?)",
            ((ts, sensor_id, temp) for sensor_id, temp in sensor_readings.items()),
        )
        conn.executemany(
            "INSERT INTO fan_readings (ts, fan_id, percent, rpm) VALUES (?, ?, ?, ?)",
            ((ts, fan_id, percent, rpm) for fan_id, percent, rpm in fan_readings),
        )


def query_history(hours: int) -> dict:
    """
    Return temp and fan readings for the last `hours` hours.
    """
    since = int(time.time()) - (hours * 3600)

    with _connect() as conn:
        sensor_rows = conn.execute(
            "SELECT ts, sensor_id, temp FROM readings WHERE ts >= ? ORDER BY ts ASC",
            (since,),
        ).fetchall()

        fan_rows = conn.execute(
            "SELECT ts, fan_id, percent, rpm FROM fan_readings WHERE ts >= ? ORDER BY ts ASC",
            (since,),
        ).fetchall()

    return {
        "sensors": [dict(r) for r in sensor_rows],
        "fans": [dict(r) for r in fan_rows],
    }


def prune_old_rows(history_days: int) -> None:
    """Delete rows older than history_days."""
    cutoff = int(time.time()) - (history_days * 86400)
    with _connect() as conn:
        r = conn.execute("DELETE FROM readings WHERE ts < ?", (cutoff,))
        f = conn.execute("DELETE FROM fan_readings WHERE ts < ?", (cutoff,))
        if r.rowcount or f.rowcount:
            logger.debug("Pruned %d sensor row(s) and %d fan row(s)", r.rowcount, f.rowcount)
