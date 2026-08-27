import asyncio
import json

from fastapi import HTTPException
import pytest

from app import config as config_module
from app import controller
from app.api import routes
from app.config import migrate_sensor_ids, validate_config
from app.models import (
    AppConfig,
    Curve,
    CurvePoint,
    DashboardGroup,
    FanConfig,
    VirtualSensor,
)


def _make_config(drive_cases, source_ids=None):
    source_ids = source_ids or [
        drive_cases["canonical_id"],
        *drive_cases["other_sensor_ids"],
    ]
    return AppConfig(
        curves=[
            Curve(
                name="Drive Curve",
                points=[
                    CurvePoint(temp=30, percent=30),
                    CurvePoint(temp=50, percent=100),
                ],
            )
        ],
        virtual_sensors=[
            VirtualSensor(
                id=drive_cases["virtual_id"],
                name="Drive Array",
                source_sensor_ids=source_ids,
                aggregation="max",
            )
        ],
        fan_configs=[
            FanConfig(
                fan_id=drive_cases["fan_id"],
                fan_label="Test Fan",
                curve_name="Drive Curve",
                sensor_id=drive_cases["virtual_id"],
                backend="hwmon-pwm",
            )
        ],
    )


@pytest.mark.regression
def test_migrates_every_persisted_sensor_reference(drive_cases):
    old = drive_cases["legacy_smartctl_id"]
    canonical = drive_cases["canonical_id"]
    config = _make_config(drive_cases, [old, *drive_cases["other_sensor_ids"]])
    config.sensor_aliases = {old: "Array Disk"}
    config.fan_configs[0].sensor_id = old
    config.dashboard_groups = [
        DashboardGroup(
            id="storage", name="Storage", type="sensor", item_ids=[old]
        )
    ]
    config.card_colors = {old: "teal"}

    migrated, count = migrate_sensor_ids(config)

    assert count == 5
    assert migrated.sensor_aliases == {canonical: "Array Disk"}
    assert migrated.virtual_sensors[0].source_sensor_ids[0] == canonical
    assert migrated.fan_configs[0].sensor_id == canonical
    assert migrated.dashboard_groups[0].item_ids == [canonical]
    assert migrated.card_colors == {canonical: "teal"}


@pytest.mark.regression
def test_migration_deduplicates_sources_in_original_order(drive_cases):
    config = _make_config(
        drive_cases,
        [
            drive_cases["other_sensor_ids"][0],
            drive_cases["legacy_smartctl_id"],
            drive_cases["legacy_drivetemp_id"],
            drive_cases["canonical_id"],
            drive_cases["other_sensor_ids"][1],
        ],
    )

    migrated, _ = migrate_sensor_ids(config)

    assert migrated.virtual_sensors[0].source_sensor_ids == [
        drive_cases["other_sensor_ids"][0],
        drive_cases["canonical_id"],
        drive_cases["other_sensor_ids"][1],
    ]


@pytest.mark.regression
def test_mapping_collision_prefers_canonical_then_lexical_legacy(drive_cases):
    canonical = drive_cases["canonical_id"]
    config = _make_config(drive_cases)
    config.sensor_aliases = {
        drive_cases["legacy_smartctl_id"]: "Smart alias",
        canonical: "Canonical alias",
        drive_cases["legacy_drivetemp_id"]: "Drive alias",
    }
    config.card_colors = {
        drive_cases["legacy_smartctl_id"]: "blue",
        drive_cases["legacy_drivetemp_id"]: "amber",
    }

    migrated, _ = migrate_sensor_ids(config)

    assert migrated.sensor_aliases == {canonical: "Canonical alias"}
    assert migrated.card_colors == {canonical: "amber"}


@pytest.mark.regression
def test_migration_is_idempotent(drive_cases):
    config = _make_config(
        drive_cases,
        [drive_cases["legacy_smartctl_id"], *drive_cases["other_sensor_ids"]],
    )

    migrated, first_count = migrate_sensor_ids(config)
    snapshot = migrated.model_dump()
    migrated_again, second_count = migrate_sensor_ids(migrated)

    assert first_count > 0
    assert second_count == 0
    assert migrated_again.model_dump() == snapshot


@pytest.mark.regression
def test_load_config_migrates_offline_id_and_only_saves_when_changed(
    tmp_path, monkeypatch, drive_cases
):
    config_path = tmp_path / "config.json"
    persisted = _make_config(
        drive_cases,
        [drive_cases["legacy_drivetemp_id"], *drive_cases["other_sensor_ids"]],
    )
    config_path.write_text(json.dumps(persisted.model_dump()), encoding="utf-8")
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)

    loaded = config_module.load_config()
    first_contents = config_path.read_text(encoding="utf-8")
    loaded_again = config_module.load_config()

    assert loaded.virtual_sensors[0].source_sensor_ids[0] == drive_cases["canonical_id"]
    assert loaded_again.model_dump() == loaded.model_dump()
    assert config_path.read_text(encoding="utf-8") == first_contents


@pytest.mark.regression
def test_load_config_does_not_rewrite_canonical_config(
    tmp_path, monkeypatch, drive_cases
):
    config_path = tmp_path / "config.json"
    persisted = _make_config(drive_cases)
    config_path.write_text(json.dumps(persisted.model_dump()), encoding="utf-8")
    saved = []
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    monkeypatch.setattr(config_module, "save_config", saved.append)

    loaded = config_module.load_config()

    assert loaded.model_dump() == persisted.model_dump()
    assert saved == []


@pytest.mark.regression
@pytest.mark.parametrize("available_count", [0, 1, 3])
def test_offline_virtual_sources_remain_structurally_valid(drive_cases, available_count):
    config = _make_config(drive_cases)
    # Availability is intentionally irrelevant to structural validation.
    available = config.virtual_sensors[0].source_sensor_ids[:available_count]

    assert isinstance(available, list)
    assert validate_config(config) == []


@pytest.mark.regression
def test_runtime_partial_source_aggregation_still_works(drive_cases):
    config = _make_config(drive_cases)
    readings = {
        drive_cases["other_sensor_ids"][0]: 38.0,
        drive_cases["other_sensor_ids"][1]: 44.0,
    }

    resolved = controller.resolve_virtual_sensors(config.virtual_sensors, readings)

    assert resolved == {drive_cases["virtual_id"]: 44.0}


@pytest.mark.regression
def test_all_missing_virtual_sources_use_fan_safety_floor(monkeypatch, drive_cases):
    config = _make_config(drive_cases)
    applied = []
    monkeypatch.setattr(controller, "detect_sensors", lambda: [])
    monkeypatch.setattr(
        controller,
        "_apply_fan_speed",
        lambda fan_id, backend, percent: applied.append((fan_id, backend, percent)),
    )
    monkeypatch.setattr(controller, "_get_rpm_map", lambda _config: {})
    history_writes = []
    monkeypatch.setattr(
        controller, "write_cycle_readings", lambda *args: history_writes.append(args)
    )
    monkeypatch.setattr(controller, "prune_old_rows", lambda *_args: None)
    monkeypatch.setattr(controller, "_next_history_prune_at", None)

    asyncio.run(controller.run_once(config))

    assert applied == [
        (drive_cases["fan_id"], "hwmon-pwm", config.settings.safety_floor_percent)
    ]
    assert len(history_writes) == 1
    _, sensor_readings, fan_readings = history_writes[0]
    assert sensor_readings == {}
    assert fan_readings == [(drive_cases["fan_id"], config.settings.safety_floor_percent, None)]


@pytest.mark.regression
def test_history_pruning_runs_once_per_hour(monkeypatch):
    pruned = []
    monotonic_values = iter([100.0, 101.0, 3700.0])
    monkeypatch.setattr(controller.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(controller, "prune_old_rows", pruned.append)
    monkeypatch.setattr(controller, "_next_history_prune_at", None)

    controller._prune_history_if_due(30)
    controller._prune_history_if_due(30)
    controller._prune_history_if_due(30)

    assert pruned == [30, 30]


@pytest.mark.regression
def test_history_pruning_retries_after_failure(monkeypatch):
    attempts = []

    def prune(history_days):
        attempts.append(history_days)
        if len(attempts) == 1:
            raise RuntimeError("temporary database failure")

    monotonic_values = iter([100.0, 101.0])
    monkeypatch.setattr(controller.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(controller, "prune_old_rows", prune)
    monkeypatch.setattr(controller, "_next_history_prune_at", None)

    with pytest.raises(RuntimeError, match="temporary database failure"):
        controller._prune_history_if_due(30)
    assert controller._next_history_prune_at is None

    controller._prune_history_if_due(30)

    assert attempts == [30, 30]
    assert controller._next_history_prune_at == 3701.0


@pytest.mark.regression
def test_post_config_accepts_offline_physical_source(monkeypatch, drive_cases):
    config = _make_config(drive_cases)
    saved = []
    installed = []
    monkeypatch.setattr(
        routes,
        "detect_sensors",
        lambda: pytest.fail("POST /config must not detect hardware"),
    )
    monkeypatch.setattr(config_module, "save_config", saved.append)
    monkeypatch.setattr(routes, "_set_app_config", installed.append)

    result = asyncio.run(routes.post_config(config))

    assert result == {"status": "ok"}
    assert saved == [config]
    assert installed == [config]


@pytest.mark.regression
def test_post_config_migrates_legacy_offline_source(monkeypatch, drive_cases):
    config = _make_config(
        drive_cases,
        [drive_cases["legacy_smartctl_id"], *drive_cases["other_sensor_ids"]],
    )
    saved = []
    monkeypatch.setattr(config_module, "save_config", saved.append)
    monkeypatch.setattr(routes, "_set_app_config", lambda _config: None)

    result = asyncio.run(routes.post_config(config))

    assert result == {"status": "ok"}
    assert saved[0].virtual_sensors[0].source_sensor_ids[0] == drive_cases["canonical_id"]


@pytest.mark.regression
def test_missing_source_can_be_removed_and_saved(monkeypatch, drive_cases):
    config = _make_config(drive_cases, drive_cases["other_sensor_ids"])
    saved = []
    monkeypatch.setattr(config_module, "save_config", saved.append)
    monkeypatch.setattr(routes, "_set_app_config", lambda _config: None)

    assert asyncio.run(routes.post_config(config)) == {"status": "ok"}
    assert saved[0].virtual_sensors[0].source_sensor_ids == drive_cases["other_sensor_ids"]


@pytest.mark.regression
def test_structural_errors_still_fail(monkeypatch, drive_cases):
    config = _make_config(drive_cases)
    config.virtual_sensors[0].aggregation = "median"
    config.virtual_sensors[0].source_sensor_ids[0] = "virtual/nested"
    config.fan_configs[0].backend = "unknown"
    config.fan_configs[0].curve_name = "missing"
    saved = []
    monkeypatch.setattr(config_module, "save_config", saved.append)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(routes.post_config(config))

    assert exc_info.value.status_code == 422
    assert any("invalid aggregation" in error for error in exc_info.value.detail)
    assert any("cannot reference another virtual" in error for error in exc_info.value.detail)
    assert any("invalid backend" in error for error in exc_info.value.detail)
    assert any("unknown curve" in error for error in exc_info.value.detail)
    assert saved == []


@pytest.mark.regression
def test_virtual_deletion_is_invalid_while_fan_still_references_it(drive_cases):
    config = _make_config(drive_cases)
    config.virtual_sensors = []

    errors = validate_config(config)

    assert errors == [
        f"Fan '{drive_cases['fan_id']}' references unknown virtual sensor "
        f"'{drive_cases['virtual_id']}'"
    ]


@pytest.mark.regression
def test_empty_duplicate_fans_and_noncanonical_virtual_ids_are_rejected(drive_cases):
    config = _make_config(drive_cases)
    config.virtual_sensors[0].id = "not-prefixed"
    config.fan_configs.extend(
        [
            config.fan_configs[0].model_copy(),
            config.fan_configs[0].model_copy(update={"fan_id": ""}),
        ]
    )

    errors = validate_config(config)

    assert any("beginning with 'virtual/'" in error for error in errors)
    assert any("Duplicate fan config ID" in error for error in errors)
    assert "Fan config has an empty fan ID" in errors
