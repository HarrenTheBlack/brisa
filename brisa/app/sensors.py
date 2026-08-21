import json
import logging
import os
import subprocess
from urllib.parse import quote

from app.sensor_ids import make_drive_sensor_id

logger = logging.getLogger(__name__)

_smartctl_available: bool | None = None

HWMON_PATH = "/sys/class/hwmon"
BLOCK_PATH = "/sys/class/block"


def _read_file(path: str) -> str | None:
    """Read a sysfs file and return stripped content, or None on failure."""
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except OSError:
        return None


def _fallback_drive_sensor_id(backend: str, value: str) -> str:
    """Build an explicitly unstable ID when no stable hardware ID is available."""
    return f"drive-fallback-{backend}-{quote(value.strip(), safe='._:-')}"


def _build_drivetemp_map() -> dict[str, dict]:
    """
    Build a mapping from resolved hwmon device path to drive metadata.

    Identity prefers WWID, then serial. If neither is available, an explicitly
    unstable hwmon fallback is retained so the sensor remains observable.

    human_label includes the block device letter for display:
        "sda — WDC WD120EFGX-68"

    model is display metadata and is not part of the sensor ID.
    """
    mapping: dict[str, dict] = {}

    try:
        block_devs = os.listdir(BLOCK_PATH)
    except OSError as e:
        logger.warning("Cannot read %s: %s", BLOCK_PATH, e)
        return mapping

    for dev in sorted(block_devs):
        dev_path = os.path.join(BLOCK_PATH, dev)

        # Skip partitions
        if os.path.exists(os.path.join(dev_path, "partition")):
            continue

        block_real = os.path.realpath(dev_path)
        hwmon_sub = os.path.join(block_real, "device", "hwmon")

        if not os.path.isdir(hwmon_sub):
            continue

        try:
            hwmon_entries = os.listdir(hwmon_sub)
        except OSError:
            continue

        model_raw = _read_file(os.path.join(block_real, "device", "model"))
        model = model_raw.strip() if model_raw else None

        wwid_raw = _read_file(os.path.join(block_real, "device", "wwid"))
        serial_raw = _read_file(os.path.join(block_real, "device", "serial"))

        label = f"{dev} \u2014 {model}" if model else dev

        for hwmon_entry in hwmon_entries:
            hwmon_real = os.path.realpath(os.path.join(hwmon_sub, hwmon_entry))
            if wwid_raw:
                sensor_id = make_drive_sensor_id("wwid", wwid_raw)
            elif serial_raw:
                sensor_id = make_drive_sensor_id("serial", serial_raw)
            else:
                sensor_id = _fallback_drive_sensor_id("drivetemp", hwmon_entry)
                logger.warning(
                    "Drive %s has no WWID or serial; using unstable sensor ID %s",
                    dev,
                    sensor_id,
                )
            mapping[hwmon_real] = {
                "sensor_id": sensor_id,
                "label": label,
                "model": model or dev,
                "block_device": dev,
            }

    return mapping


def _smartctl_read_drive(
    dev_path: str,
    sysfs_wwid: str | None = None,
    sysfs_serial: str | None = None,
) -> dict | None:
    """Run smartctl on a block device and return temperature info, or None."""
    global _smartctl_available
    if _smartctl_available is False:
        return None

    try:
        result = subprocess.run(
            ["smartctl", "--json=c", "-a", dev_path],
            capture_output=True, timeout=10,
        )
    except FileNotFoundError:
        if _smartctl_available is None:
            logger.info("smartctl not installed, SAS/SCSI temperature detection disabled")
        _smartctl_available = False
        return None
    except subprocess.TimeoutExpired:
        logger.warning("smartctl timed out for %s", dev_path)
        return None

    _smartctl_available = True

    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return None

    temp = data.get("temperature", {}).get("current")
    if temp is None:
        return None

    model = data.get("model_name", "")
    serial = data.get("serial_number", "")

    wwn = data.get("wwn")
    if sysfs_wwid:
        identity_kind = "wwid"
        identity = sysfs_wwid
    elif wwn and "naa" in wwn and "id" in wwn:
        oui = wwn.get("oui", 0)
        identity_kind = "wwid"
        identity = f"naa.{wwn['naa']:x}{oui:06x}{wwn['id']:09x}"
    elif sysfs_serial or serial:
        identity_kind = "serial"
        identity = sysfs_serial or serial
    else:
        identity_kind = None
        identity = None

    return {
        "temp": float(temp),
        "model": model,
        "sensor_id": (
            make_drive_sensor_id(identity_kind, identity)
            if identity_kind and identity
            else None
        ),
    }


def _detect_smartctl_sensors() -> list[dict]:
    """
    Find drives with SMART temperature data but no hwmon temperature entry.
    Covers SAS drives that the drivetemp kernel module doesn't support.
    """
    sensors = []

    try:
        block_devs = os.listdir(BLOCK_PATH)
    except OSError:
        return sensors

    for dev in sorted(block_devs):
        if not dev.startswith("sd"):
            continue

        dev_path = os.path.join(BLOCK_PATH, dev)

        if os.path.exists(os.path.join(dev_path, "partition")):
            continue

        block_real = os.path.realpath(dev_path)
        hwmon_sub = os.path.join(block_real, "device", "hwmon")

        # Skip drives already covered by drivetemp/hwmon
        if os.path.isdir(hwmon_sub):
            try:
                if os.listdir(hwmon_sub):
                    continue
            except OSError:
                pass

        sysfs_wwid = _read_file(os.path.join(block_real, "device", "wwid"))
        sysfs_serial = _read_file(os.path.join(block_real, "device", "serial"))
        info = _smartctl_read_drive(
            f"/dev/{dev}",
            sysfs_wwid=sysfs_wwid,
            sysfs_serial=sysfs_serial,
        )
        if info is None:
            continue

        model = info["model"] or dev
        label = f"{dev} — {model}" if info["model"] else dev
        sensor_id = info["sensor_id"]
        if sensor_id is None:
            sensor_id = _fallback_drive_sensor_id("smartctl", dev)
            logger.warning(
                "Drive %s has no WWID or serial; using unstable sensor ID %s",
                dev,
                sensor_id,
            )

        sensors.append({
            "id": sensor_id,
            "driver": "smartctl",
            "label": label,
            "model": info["model"] or None,
            "block_device": dev,
            "current_temp": info["temp"],
        })

    return sensors


def _deduplicate_drive_sensors(sensors: list[dict]) -> list[dict]:
    """Deduplicate canonical drive IDs, preferring drivetemp over smartctl."""
    result: list[dict] = []
    positions: dict[str, int] = {}
    preference = {"smartctl": 1, "drivetemp": 2}

    for sensor in sensors:
        sensor_id = sensor["id"]
        if not sensor_id.startswith("drive-") or sensor_id not in positions:
            if sensor_id.startswith("drive-"):
                positions[sensor_id] = len(result)
            result.append(sensor)
            continue

        index = positions[sensor_id]
        existing = result[index]
        if preference.get(sensor["driver"], 0) > preference.get(existing["driver"], 0):
            result[index] = sensor
        logger.warning(
            "Duplicate drive sensor %s detected via %s and %s; using %s",
            sensor_id,
            existing["driver"],
            sensor["driver"],
            result[index]["driver"],
        )

    return result


def detect_sensors() -> list[dict]:
    """
    Scan /sys/class/hwmon and return all available temperature sensors.

    Returns a list of dicts:
        {
            "id": "coretemp-hwmon4/Package id 0",
            "driver": "coretemp",
            "label": "Package id 0",
            "current_temp": 38.0
        }

    Drive sensor IDs use only a stable WWID or serial when available:
        "drive-wwid-naa.50014ee2c1c21634"
    The label still includes the block device letter for display:
        "sda — WDC WD120EFGX-68"
    Falls back to serial, then an explicitly unstable backend-local ID.
    """
    sensors = []
    drivetemp_map = _build_drivetemp_map()

    try:
        hwmon_dirs = sorted(os.listdir(HWMON_PATH))
    except OSError as e:
        logger.error("Cannot read %s: %s", HWMON_PATH, e)
        return sensors

    for hwmon_dir in hwmon_dirs:
        hwmon_full = os.path.join(HWMON_PATH, hwmon_dir)

        try:
            device_path = os.path.realpath(hwmon_full)
        except OSError:
            device_path = hwmon_full

        driver = _read_file(os.path.join(device_path, "name")) or hwmon_dir

        try:
            entries = os.listdir(device_path)
        except OSError as e:
            logger.warning("Cannot list %s: %s", device_path, e)
            continue

        temp_inputs = sorted(
            e for e in entries if e.startswith("temp") and e.endswith("_input")
        )

        for temp_input in temp_inputs:
            n = temp_input[len("temp"):-len("_input")]

            raw = _read_file(os.path.join(device_path, temp_input))
            if raw is None:
                continue

            try:
                current_temp = int(raw) / 1000.0
            except ValueError:
                logger.warning("Cannot parse temp value '%s' from %s", raw, temp_input)
                continue

            if driver == "drivetemp" and device_path in drivetemp_map:
                drive_info = drivetemp_map[device_path]
                sensor_id = drive_info["sensor_id"]
                label = drive_info["label"]
            else:
                label_raw = _read_file(os.path.join(device_path, f"temp{n}_label"))
                label = label_raw if label_raw else f"temp{n}"
                sensor_id = f"{driver}-{hwmon_dir}/{label}"

            sensor = {
                "id": sensor_id,
                "driver": driver,
                "label": label,
                "current_temp": current_temp,
            }
            if driver == "drivetemp" and device_path in drivetemp_map:
                sensor["model"] = drive_info["model"]
                sensor["block_device"] = drive_info["block_device"]
            sensors.append(sensor)

    # Fallback: detect SAS/SCSI drives via smartctl (no hwmon coverage)
    smartctl_sensors = _detect_smartctl_sensors()
    if smartctl_sensors:
        logger.info("Detected %d additional sensor(s) via smartctl", len(smartctl_sensors))
        sensors.extend(smartctl_sensors)

    sensors = _deduplicate_drive_sensors(sensors)

    logger.info("Detected %d temperature sensor(s)", len(sensors))
    return sensors


def read_temp(sensor_id: str) -> float:
    """
    Read current temperature for a given sensor_id.
    Raises ValueError if sensor_id is not found.
    """
    sensors = detect_sensors()
    for sensor in sensors:
        if sensor["id"] == sensor_id:
            return sensor["current_temp"]
    raise ValueError(f"Sensor not found: {sensor_id!r}")
