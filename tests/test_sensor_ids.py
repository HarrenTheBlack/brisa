import pytest

from app.sensor_ids import (
    make_drive_sensor_id,
    migrate_legacy_drive_sensor_id,
)


@pytest.mark.regression
@pytest.mark.parametrize(
    ("legacy_id", "canonical_id"),
    [
        (
            "smartctl-wwid-naa.5000039c88d0351a/TOSHIBA MG09ACA18TE",
            "drive-wwid-naa.5000039c88d0351a",
        ),
        (
            "drivetemp-wwid-naa.5000039c88d0351a/TOSHIBA MG09ACA1",
            "drive-wwid-naa.5000039c88d0351a",
        ),
        (
            "drivetemp-wwid-naa.5000039c88d0351a/sda — TOSHIBA MG09ACA1",
            "drive-wwid-naa.5000039c88d0351a",
        ),
        (
            "smartctl-serial-SERIAL-001/Drive Model",
            "drive-serial-SERIAL-001",
        ),
    ],
)
def test_supported_legacy_patterns(legacy_id, canonical_id):
    assert migrate_legacy_drive_sensor_id(legacy_id) == canonical_id


@pytest.mark.regression
def test_already_canonical_and_unrelated_ids_are_unchanged():
    ids = [
        "drive-wwid-naa.5000039c88d0351a",
        "coretemp-hwmon4/Package id 0",
        "nvme-hwmon2/Composite",
        "virtual/drive-array",
        "smartctl-sda/Unknown",
    ]

    assert [migrate_legacy_drive_sensor_id(value) for value in ids] == ids


@pytest.mark.regression
def test_migration_is_idempotent():
    old = "smartctl-wwid-NAA.5000039C88D0351A/Model"
    once = migrate_legacy_drive_sensor_id(old)

    assert once == "drive-wwid-naa.5000039c88d0351a"
    assert migrate_legacy_drive_sensor_id(once) == once


@pytest.mark.regression
def test_ambiguous_legacy_serial_is_not_migrated():
    ambiguous = [
        "smartctl-serial-SERIAL/SECTION/Drive Model",
        "smartctl-serial-SERIAL_01/Drive Model",
        "drivetemp-wwid-naa.5000_1234/Drive Model",
        "smartctl-wwid- /Drive Model",
    ]

    assert [migrate_legacy_drive_sensor_id(value) for value in ambiguous] == ambiguous


@pytest.mark.regression
def test_normalization_preserves_distinct_identifiers():
    assert make_drive_sensor_id("serial", "SERIAL 1") == "drive-serial-SERIAL%201"
    assert make_drive_sensor_id("serial", "SERIAL_1") == "drive-serial-SERIAL_1"
    assert make_drive_sensor_id("serial", "SERIAL/1") == "drive-serial-SERIAL%2F1"
    assert make_drive_sensor_id("wwid", " NAA.ABC ") == "drive-wwid-naa.abc"


@pytest.mark.regression
def test_migration_never_changes_one_wwid_into_another():
    first = migrate_legacy_drive_sensor_id("smartctl-wwid-naa.111/Model")
    second = migrate_legacy_drive_sensor_id("drivetemp-wwid-naa.222/Model")

    assert first == "drive-wwid-naa.111"
    assert second == "drive-wwid-naa.222"
    assert first != second
