import re
from urllib.parse import quote


_LEGACY_WWID_RE = re.compile(
    r"^(?:smartctl|drivetemp)-wwid-(?P<identity>[^/\r\n]+)/[^\r\n]+$"
)
_LEGACY_SERIAL_RE = re.compile(
    r"^(?:smartctl|drivetemp)-serial-(?P<identity>[^/\r\n]+)/[^/\r\n]+$"
)
_LEGACY_PREFIXES = (
    "smartctl-wwid-",
    "drivetemp-wwid-",
    "smartctl-serial-",
    "drivetemp-serial-",
)


def normalize_drive_identity(kind: str, value: str) -> str:
    """Normalize one stable hardware identifier without using display metadata."""
    if kind not in ("wwid", "serial"):
        raise ValueError(f"Unsupported drive identity kind: {kind!r}")

    normalized = value.strip()
    if not normalized:
        raise ValueError("Drive identity cannot be empty")
    if kind == "wwid":
        normalized = normalized.lower()

    # Keep common identifier punctuation readable while preserving distinctions
    # between spaces, underscores, slashes, percent signs, and control bytes.
    return quote(normalized, safe="._:-")


def make_drive_sensor_id(kind: str, value: str) -> str:
    return f"drive-{kind}-{normalize_drive_identity(kind, value)}"


def migrate_legacy_drive_sensor_id(sensor_id: str) -> str:
    """Convert a safely recognized legacy drive ID to its canonical form."""
    match = _LEGACY_WWID_RE.fullmatch(sensor_id)
    if match:
        identity = match.group("identity")
        # The old emitter collapsed whitespace to underscores, so an underscore
        # cannot be distinguished from one present in the hardware identifier.
        if "_" in identity:
            return sensor_id
        try:
            return make_drive_sensor_id("wwid", identity)
        except ValueError:
            return sensor_id

    # Legacy serial IDs are only unambiguous when there is one slash: old IDs
    # did not escape slashes in either the serial or model component.
    match = _LEGACY_SERIAL_RE.fullmatch(sensor_id)
    if match:
        identity = match.group("identity")
        if "_" in identity:
            return sensor_id
        try:
            return make_drive_sensor_id("serial", identity)
        except ValueError:
            return sensor_id

    return sensor_id


def is_legacy_drive_id_candidate(sensor_id: str) -> bool:
    """Return whether an ID resembles a supported legacy form but may be malformed."""
    return sensor_id.startswith(_LEGACY_PREFIXES)
