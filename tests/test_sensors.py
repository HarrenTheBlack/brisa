import json
from types import SimpleNamespace

import pytest

from app import sensors


def _make_drivetemp_tree(
    tmp_path,
    monkeypatch,
    *,
    dev,
    model,
    tag,
    wwid=None,
    serial=None,
    smartctl_sensors=None,
):
    root = tmp_path / tag
    block_root = root / "class" / "block"
    hwmon_root = root / "class" / "hwmon"
    drive_root = root / "devices" / dev
    drive_hwmon = drive_root / "device" / "hwmon" / "hwmon0"

    block_root.mkdir(parents=True)
    hwmon_root.mkdir(parents=True)
    drive_hwmon.mkdir(parents=True)
    (drive_root / "device" / "model").write_text(model, encoding="utf-8")
    if wwid is not None:
        (drive_root / "device" / "wwid").write_text(wwid, encoding="utf-8")
    if serial is not None:
        (drive_root / "device" / "serial").write_text(serial, encoding="utf-8")
    (drive_hwmon / "name").write_text("drivetemp", encoding="utf-8")
    (drive_hwmon / "temp1_input").write_text("41000", encoding="utf-8")

    (block_root / dev).symlink_to(drive_root, target_is_directory=True)
    (hwmon_root / "hwmon0").symlink_to(drive_hwmon, target_is_directory=True)
    monkeypatch.setattr(sensors, "BLOCK_PATH", str(block_root))
    monkeypatch.setattr(sensors, "HWMON_PATH", str(hwmon_root))
    monkeypatch.setattr(sensors, "_smartctl_available", None)
    if smartctl_sensors is not None:
        monkeypatch.setattr(
            sensors, "_detect_smartctl_sensors", lambda: smartctl_sensors
        )

    return sensors.detect_sensors()


def _make_smartctl_tree(
    tmp_path,
    monkeypatch,
    *,
    dev,
    model,
    tag,
    wwn=None,
    serial="TEST-SERIAL",
    sysfs_wwid=None,
    sysfs_serial=None,
):
    root = tmp_path / tag
    block_root = root / "class" / "block"
    hwmon_root = root / "class" / "hwmon"
    drive_root = root / "devices" / dev

    block_root.mkdir(parents=True)
    hwmon_root.mkdir(parents=True)
    (drive_root / "device").mkdir(parents=True)
    if sysfs_wwid is not None:
        (drive_root / "device" / "wwid").write_text(sysfs_wwid, encoding="utf-8")
    if sysfs_serial is not None:
        (drive_root / "device" / "serial").write_text(sysfs_serial, encoding="utf-8")
    (block_root / dev).symlink_to(drive_root, target_is_directory=True)

    smartctl_output = {
        "temperature": {"current": 42},
        "model_name": model,
        "serial_number": serial,
    }
    if wwn is not None:
        smartctl_output["wwn"] = wwn

    def fake_run(*_args, **_kwargs):
        return SimpleNamespace(stdout=json.dumps(smartctl_output).encode("utf-8"))

    monkeypatch.setattr(sensors, "BLOCK_PATH", str(block_root))
    monkeypatch.setattr(sensors, "HWMON_PATH", str(hwmon_root))
    monkeypatch.setattr(sensors.subprocess, "run", fake_run)
    monkeypatch.setattr(sensors, "_smartctl_available", None)

    return sensors.detect_sensors()[0]


@pytest.fixture
def reported_wwn():
    return {"naa": 5, "oui": 0x39, "id": 0xC88D0351A}


@pytest.mark.regression
def test_smartctl_emits_canonical_wwid_id(
    tmp_path, monkeypatch, drive_cases, reported_wwn
):
    detected = _make_smartctl_tree(
        tmp_path,
        monkeypatch,
        dev="sda",
        model=drive_cases["smartctl_model"],
        tag="smartctl-reported",
        wwn=reported_wwn,
    )

    assert detected == {
        "id": drive_cases["canonical_id"],
        "driver": "smartctl",
        "label": "sda — TOSHIBA MG09ACA18TE",
        "model": "TOSHIBA MG09ACA18TE",
        "block_device": "sda",
        "current_temp": 42.0,
    }


@pytest.mark.regression
def test_drivetemp_emits_canonical_wwid_id(tmp_path, monkeypatch, drive_cases):
    detected = _make_drivetemp_tree(
        tmp_path,
        monkeypatch,
        dev="sdb",
        wwid=drive_cases["wwid"],
        model=drive_cases["drivetemp_model"],
        tag="drivetemp-reported",
    )[0]

    assert detected == {
        "id": drive_cases["canonical_id"],
        "driver": "drivetemp",
        "label": "sdb — TOSHIBA MG09ACA1",
        "model": "TOSHIBA MG09ACA1",
        "block_device": "sdb",
        "current_temp": 41.0,
    }


@pytest.mark.regression
def test_same_wwid_has_same_id_across_backends(
    tmp_path, monkeypatch, drive_cases, reported_wwn
):
    smartctl_drive = _make_smartctl_tree(
        tmp_path,
        monkeypatch,
        dev="sda",
        model=drive_cases["smartctl_model"],
        tag="backend-smartctl",
        wwn=reported_wwn,
    )
    drivetemp_drive = _make_drivetemp_tree(
        tmp_path,
        monkeypatch,
        dev="sdh",
        wwid=drive_cases["wwid"],
        model=drive_cases["drivetemp_model"],
        tag="backend-drivetemp",
    )[0]

    assert smartctl_drive["id"] == drivetemp_drive["id"]
    assert smartctl_drive["id"] == drive_cases["canonical_id"]


@pytest.mark.regression
def test_same_wwid_ignores_model_text(tmp_path, monkeypatch, reported_wwn):
    full_model = _make_smartctl_tree(
        tmp_path,
        monkeypatch,
        dev="sda",
        model="TOSHIBA MG09ACA18TE",
        tag="full-model",
        wwn=reported_wwn,
    )
    short_model = _make_smartctl_tree(
        tmp_path,
        monkeypatch,
        dev="sda",
        model="TOSHIBA MG09ACA1",
        tag="short-model",
        wwn=reported_wwn,
    )

    assert full_model["id"] == short_model["id"]
    assert full_model["label"] != short_model["label"]


@pytest.mark.regression
def test_same_wwid_ignores_sd_assignment(tmp_path, monkeypatch, drive_cases):
    first = _make_drivetemp_tree(
        tmp_path,
        monkeypatch,
        dev="sda",
        wwid=drive_cases["wwid"],
        model=drive_cases["drivetemp_model"],
        tag="assignment-a",
    )[0]
    second = _make_drivetemp_tree(
        tmp_path,
        monkeypatch,
        dev="sdh",
        wwid=drive_cases["wwid"],
        model=drive_cases["drivetemp_model"],
        tag="assignment-h",
    )[0]

    assert first["id"] == second["id"]
    assert first["block_device"] != second["block_device"]


@pytest.mark.regression
def test_replacement_wwid_gets_different_id(tmp_path, monkeypatch, drive_cases):
    original = _make_drivetemp_tree(
        tmp_path,
        monkeypatch,
        dev="sda",
        wwid=drive_cases["wwid"],
        model=drive_cases["drivetemp_model"],
        tag="original",
    )[0]
    replacement = _make_drivetemp_tree(
        tmp_path,
        monkeypatch,
        dev="sda",
        wwid=drive_cases["replacement_wwid"],
        model=drive_cases["drivetemp_model"],
        tag="replacement",
    )[0]

    assert original["id"] != replacement["id"]
    assert replacement["id"] == f"drive-wwid-{drive_cases['replacement_wwid']}"


@pytest.mark.regression
def test_serial_fallback_is_shared_by_backends(tmp_path, monkeypatch):
    smartctl_drive = _make_smartctl_tree(
        tmp_path,
        monkeypatch,
        dev="sda",
        model="Drive",
        tag="serial-smartctl",
        serial="SERIAL 01/A",
    )
    drivetemp_drive = _make_drivetemp_tree(
        tmp_path,
        monkeypatch,
        dev="sdf",
        serial="SERIAL 01/A",
        model="Drive",
        tag="serial-drivetemp",
    )[0]

    assert smartctl_drive["id"] == "drive-serial-SERIAL%2001%2FA"
    assert smartctl_drive["id"] == drivetemp_drive["id"]


@pytest.mark.regression
def test_smartctl_prefers_sysfs_wwid_over_serial(tmp_path, monkeypatch, drive_cases):
    detected = _make_smartctl_tree(
        tmp_path,
        monkeypatch,
        dev="sda",
        model="Drive",
        tag="sysfs-wwid",
        serial="SMART-SERIAL",
        sysfs_wwid=drive_cases["wwid"],
        sysfs_serial="SYSFS-SERIAL",
    )

    assert detected["id"] == drive_cases["canonical_id"]


@pytest.mark.regression
def test_duplicate_canonical_drive_prefers_drivetemp(
    tmp_path, monkeypatch, drive_cases
):
    smartctl_duplicate = {
        "id": drive_cases["canonical_id"],
        "driver": "smartctl",
        "label": "sda — full model",
        "model": "full model",
        "block_device": "sda",
        "current_temp": 45.0,
    }
    detected = _make_drivetemp_tree(
        tmp_path,
        monkeypatch,
        dev="sda",
        wwid=drive_cases["wwid"],
        model=drive_cases["drivetemp_model"],
        tag="duplicate",
        smartctl_sensors=[smartctl_duplicate],
    )

    matching = [sensor for sensor in detected if sensor["id"] == drive_cases["canonical_id"]]
    assert len(matching) == 1
    assert matching[0]["driver"] == "drivetemp"
    assert matching[0]["current_temp"] == 41.0
