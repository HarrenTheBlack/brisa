import sqlite3

import pytest

from app import database


@pytest.mark.regression
def test_cycle_history_write_persists_sensor_and_fan_rows(tmp_path, monkeypatch):
    db_path = tmp_path / "history.db"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    database.init_db()

    database.write_cycle_readings(
        100,
        {"sensor-a": 42.5, "sensor-b": 37.0},
        [("fan-a", 50, 1200.0), ("fan-b", 30, None)],
    )

    with sqlite3.connect(db_path) as conn:
        sensor_rows = conn.execute(
            "SELECT ts, sensor_id, temp FROM readings ORDER BY sensor_id"
        ).fetchall()
        fan_rows = conn.execute(
            "SELECT ts, fan_id, percent, rpm FROM fan_readings ORDER BY fan_id"
        ).fetchall()

    assert sensor_rows == [(100, "sensor-a", 42.5), (100, "sensor-b", 37.0)]
    assert fan_rows == [(100, "fan-a", 50, 1200.0), (100, "fan-b", 30, None)]


@pytest.mark.regression
def test_cycle_history_write_rolls_back_when_fan_insert_fails(tmp_path, monkeypatch):
    db_path = tmp_path / "history.db"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    database.init_db()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TRIGGER abort_cycle_fan_insert
            BEFORE INSERT ON fan_readings
            BEGIN
                SELECT RAISE(ABORT, 'test rollback');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="test rollback"):
        database.write_cycle_readings(
            100, {"sensor-a": 42.5}, [("fan-a", 50, 1200.0)]
        )

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM fan_readings").fetchone()[0] == 0


@pytest.mark.regression
def test_history_migration_is_transactional_and_idempotent(
    tmp_path, monkeypatch, drive_cases
):
    db_path = tmp_path / "history.db"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    database.init_db()

    rows = [
        (1, drive_cases["legacy_smartctl_id"], 40.0),
        (2, drive_cases["legacy_smartctl_id"], 41.0),
        (3, drive_cases["legacy_drivetemp_id"], 42.0),
        (4, drive_cases["canonical_id"], 43.0),
        (5, "coretemp-hwmon4/Package id 0", 44.0),
        (6, "smartctl-sda/Unknown", 45.0),
    ]
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            "INSERT INTO readings (ts, sensor_id, temp) VALUES (?, ?, ?)", rows
        )
        conn.execute(
            "INSERT INTO fan_readings (ts, fan_id, percent, rpm) VALUES (?, ?, ?, ?)",
            (1, "fan1", 50, 1000.0),
        )

    assert database.migrate_reading_sensor_ids() == 3
    assert database.migrate_reading_sensor_ids() == 0

    with sqlite3.connect(db_path) as conn:
        migrated = conn.execute(
            "SELECT ts, sensor_id, temp FROM readings ORDER BY ts"
        ).fetchall()
        fan_rows = conn.execute(
            "SELECT ts, fan_id, percent, rpm FROM fan_readings"
        ).fetchall()

    assert migrated == [
        (1, drive_cases["canonical_id"], 40.0),
        (2, drive_cases["canonical_id"], 41.0),
        (3, drive_cases["canonical_id"], 42.0),
        (4, drive_cases["canonical_id"], 43.0),
        (5, "coretemp-hwmon4/Package id 0", 44.0),
        (6, "smartctl-sda/Unknown", 45.0),
    ]
    assert fan_rows == [(1, "fan1", 50, 1000.0)]


@pytest.mark.regression
def test_init_db_runs_history_migration(tmp_path, monkeypatch, drive_cases):
    db_path = tmp_path / "startup.db"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE readings (ts INTEGER NOT NULL, sensor_id TEXT NOT NULL, temp REAL NOT NULL)"
        )
        conn.execute(
            "INSERT INTO readings (ts, sensor_id, temp) VALUES (?, ?, ?)",
            (1, drive_cases["legacy_smartctl_id"], 40.0),
        )

    database.init_db()

    with sqlite3.connect(db_path) as conn:
        sensor_id = conn.execute("SELECT sensor_id FROM readings").fetchone()[0]
    assert sensor_id == drive_cases["canonical_id"]


@pytest.mark.regression
def test_history_migration_rolls_back_all_updates_on_error(
    tmp_path, monkeypatch, drive_cases
):
    db_path = tmp_path / "rollback.db"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    database.init_db()
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            "INSERT INTO readings (ts, sensor_id, temp) VALUES (?, ?, ?)",
            [
                (1, drive_cases["legacy_smartctl_id"], 40.0),
                (2, drive_cases["legacy_drivetemp_id"], 41.0),
            ],
        )
        conn.execute(
            """
            CREATE TRIGGER abort_drivetemp_migration
            BEFORE UPDATE OF sensor_id ON readings
            WHEN OLD.sensor_id LIKE 'drivetemp-wwid-%'
            BEGIN
                SELECT RAISE(ABORT, 'test rollback');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="test rollback"):
        database.migrate_reading_sensor_ids()

    with sqlite3.connect(db_path) as conn:
        sensor_ids = [
            row[0]
            for row in conn.execute("SELECT sensor_id FROM readings ORDER BY ts")
        ]
    assert sensor_ids == [
        drive_cases["legacy_smartctl_id"],
        drive_cases["legacy_drivetemp_id"],
    ]
