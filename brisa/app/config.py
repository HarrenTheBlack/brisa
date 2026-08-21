import json
import logging
from pathlib import Path

from app.models import AppConfig
from app.sensor_ids import (
    is_legacy_drive_id_candidate,
    migrate_legacy_drive_sensor_id,
)

logger = logging.getLogger(__name__)

CONFIG_PATH = Path("/data/config.json")

DEFAULT_CONFIG = AppConfig()


def _migrate_id(sensor_id: str, context: str) -> str:
    migrated = migrate_legacy_drive_sensor_id(sensor_id)
    if migrated != sensor_id:
        logger.info("Migrated %s: %s -> %s", context, sensor_id, migrated)
    elif is_legacy_drive_id_candidate(sensor_id):
        logger.warning("Could not safely migrate %s: %s", context, sensor_id)
    return migrated


def _migrate_mapping(
    values: dict[str, str], context: str
) -> tuple[dict[str, str], int]:
    migrated_entries: dict[str, list[tuple[str, str]]] = {}
    count = 0

    for old_id, value in sorted(values.items()):
        new_id = _migrate_id(old_id, f"{context} key")
        if new_id != old_id:
            count += 1
        migrated_entries.setdefault(new_id, []).append((old_id, value))

    if count == 0:
        return values, 0

    result: dict[str, str] = {}
    for new_id in sorted(migrated_entries):
        entries = migrated_entries[new_id]
        canonical = next((entry for entry in entries if entry[0] == new_id), None)
        winner = canonical or entries[0]
        conflicting = [entry for entry in entries if entry[1] != winner[1]]
        if conflicting:
            logger.warning(
                "%s keys %s collapse to %s with conflicting values; preserving value from %s",
                context,
                [entry[0] for entry in entries],
                new_id,
                winner[0],
            )
        result[new_id] = winner[1]

    return result, count


def _migrate_list(values: list[str], context: str) -> tuple[list[str], int]:
    result: list[str] = []
    seen: set[str] = set()
    count = 0

    for old_id in values:
        new_id = _migrate_id(old_id, context)
        if new_id != old_id:
            count += 1
        if new_id in seen:
            count += 1
            logger.warning("Removed duplicate %s after migration: %s", context, new_id)
            continue
        seen.add(new_id)
        result.append(new_id)

    return result, count


def migrate_sensor_ids(config: AppConfig) -> tuple[AppConfig, int]:
    """Syntactically migrate recognized drive IDs in every config reference."""
    count = 0

    config.sensor_aliases, migrated = _migrate_mapping(
        config.sensor_aliases, "sensor alias"
    )
    count += migrated

    for virtual in config.virtual_sensors:
        virtual.source_sensor_ids, migrated = _migrate_list(
            virtual.source_sensor_ids, f"virtual sensor '{virtual.id}' source"
        )
        count += migrated

    for fan in config.fan_configs:
        migrated_id = _migrate_id(
            fan.sensor_id, f"fan config '{fan.fan_id}' sensor"
        )
        if migrated_id != fan.sensor_id:
            count += 1
            fan.sensor_id = migrated_id

    for group in config.dashboard_groups:
        group.item_ids, migrated = _migrate_list(
            group.item_ids, f"dashboard group '{group.name}' item"
        )
        count += migrated

    config.card_colors, migrated = _migrate_mapping(
        config.card_colors, "card color"
    )
    count += migrated

    return config, count


def load_config() -> AppConfig:
    """
    Load config from CONFIG_PATH.
    If the file doesn't exist, write defaults and return them.
    Raises ValueError if the file exists but is invalid.
    """
    if not CONFIG_PATH.exists():
        logger.info("No config file found at %s, writing defaults", CONFIG_PATH)
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG.model_copy(deep=True)

    try:
        raw = CONFIG_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
        config = AppConfig.model_validate(data)
        logger.info("Loaded config from %s", CONFIG_PATH)
    except json.JSONDecodeError as e:
        raise ValueError(f"Config file is not valid JSON: {e}") from e
    except Exception as e:
        raise ValueError(f"Config file failed validation: {e}") from e

    # Migrate hwmon-pwm backend field: fan IDs starting with "hwmon-pwm-"
    # must use the "hwmon-pwm" backend, not the default "liquidctl".
    backend_fixed = 0
    for fc in config.fan_configs:
        if fc.fan_id.startswith("hwmon-pwm-") and fc.backend != "hwmon-pwm":
            logger.warning("Fixing backend for '%s': %s -> hwmon-pwm", fc.fan_id, fc.backend)
            fc.backend = "hwmon-pwm"
            backend_fixed += 1
    config, migrated = migrate_sensor_ids(config)
    if backend_fixed or migrated:
        if migrated:
            logger.warning("Migrated %d legacy sensor reference(s) in config", migrated)
        save_config(config)
        logger.info("Config saved after startup migration")

    return config


def save_config(config: AppConfig) -> None:
    """
    Write config to CONFIG_PATH as pretty-printed JSON.
    Writes atomically via a temp file to avoid corruption on crash.
    """
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

    tmp_path = CONFIG_PATH.with_suffix(".json.tmp")
    try:
        tmp_path.write_text(
            json.dumps(config.model_dump(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp_path.replace(CONFIG_PATH)
        logger.info("Saved config to %s", CONFIG_PATH)
    except OSError as e:
        logger.error("Failed to save config: %s", e)
        raise


# Curated card color keys — must match frontend CARD_COLORS map
VALID_CARD_COLORS = {"teal", "blue", "purple", "pink", "amber", "orange", "red", "slate"}


def validate_config(config: AppConfig) -> list[str]:
    """
    Validate structural relationships without requiring currently available hardware.
    Returns a list of error strings. Empty list means valid.
    """
    errors = []
    curve_names = {c.name for c in config.curves}

    virtual_sensor_ids = {vs.id for vs in config.virtual_sensors}

    # Validate virtual sensors
    for vs in config.virtual_sensors:
        if not vs.id:
            errors.append("Virtual sensor has empty ID")
        elif not vs.id.startswith("virtual/"):
            errors.append(
                f"Virtual sensor '{vs.id}' must use an ID beginning with 'virtual/'"
            )
        if vs.aggregation not in ("avg", "min", "max"):
            errors.append(
                f"Virtual sensor '{vs.id}' has invalid aggregation '{vs.aggregation}' (must be avg, min, or max)"
            )
        if len(set(vs.source_sensor_ids)) < 2:
            errors.append(
                f"Virtual sensor '{vs.id}' must reference at least 2 distinct source sensors"
            )
        for src_id in vs.source_sensor_ids:
            if src_id in virtual_sensor_ids or src_id.startswith("virtual/"):
                errors.append(
                    f"Virtual sensor '{vs.id}' cannot reference another virtual sensor '{src_id}'"
                )

    # Check for duplicate virtual sensor IDs
    seen_vs_ids = set()
    for vs in config.virtual_sensors:
        if vs.id in seen_vs_ids:
            errors.append(f"Duplicate virtual sensor ID '{vs.id}'")
        seen_vs_ids.add(vs.id)

    # Validate fan configs — sensor_id can now be a virtual sensor
    seen_fan_ids = set()
    for fan_cfg in config.fan_configs:
        if not fan_cfg.fan_id:
            errors.append("Fan config has an empty fan ID")
        elif fan_cfg.fan_id in seen_fan_ids:
            errors.append(f"Duplicate fan config ID '{fan_cfg.fan_id}'")
        seen_fan_ids.add(fan_cfg.fan_id)
        if fan_cfg.backend not in ("liquidctl", "hwmon-pwm"):
            errors.append(
                f"Fan '{fan_cfg.fan_id}' has invalid backend '{fan_cfg.backend}' (must be liquidctl or hwmon-pwm)"
            )
        if fan_cfg.curve_name not in curve_names:
            errors.append(
                f"Fan '{fan_cfg.fan_id}' references unknown curve '{fan_cfg.curve_name}'"
            )
        if not fan_cfg.sensor_id:
            errors.append(
                f"Fan '{fan_cfg.fan_id}' has an empty sensor reference"
            )
        elif (
            fan_cfg.sensor_id.startswith("virtual/")
            and fan_cfg.sensor_id not in virtual_sensor_ids
        ):
            errors.append(
                f"Fan '{fan_cfg.fan_id}' references unknown virtual sensor '{fan_cfg.sensor_id}'"
            )

    for curve in config.curves:
        if len(curve.points) < 2:
            errors.append(
                f"Curve '{curve.name}' must have at least 2 points"
            )
        else:
            temps = [p.temp for p in curve.points]
            if temps != sorted(temps):
                errors.append(
                    f"Curve '{curve.name}' points must be in ascending temperature order"
                )

    # Validate dashboard groups
    seen_group_ids = set()
    for grp in config.dashboard_groups:
        if grp.id in seen_group_ids:
            errors.append(f"Duplicate dashboard group ID '{grp.id}'")
        seen_group_ids.add(grp.id)
        if grp.type not in ("sensor", "fan"):
            errors.append(
                f"Dashboard group '{grp.name}' has invalid type '{grp.type}' (must be sensor or fan)"
            )

    # Validate card colors
    for item_id, color in config.card_colors.items():
        if color not in VALID_CARD_COLORS:
            errors.append(
                f"Card color '{color}' for '{item_id}' is not valid (must be one of {VALID_CARD_COLORS})"
            )

    return errors
