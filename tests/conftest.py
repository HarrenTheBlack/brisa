from pathlib import Path
import sys

import pytest


BRISA_ROOT = Path(__file__).resolve().parents[1] / "brisa"
sys.path.insert(0, str(BRISA_ROOT))


@pytest.fixture
def drive_cases():
    return {
        "wwid": "naa.5000039c88d0351a",
        "replacement_wwid": "naa.5000039c88d09999",
        "smartctl_model": "TOSHIBA MG09ACA18TE",
        "drivetemp_model": "TOSHIBA MG09ACA1",
        "legacy_smartctl_id": (
            "smartctl-wwid-naa.5000039c88d0351a/TOSHIBA MG09ACA18TE"
        ),
        "legacy_drivetemp_id": (
            "drivetemp-wwid-naa.5000039c88d0351a/TOSHIBA MG09ACA1"
        ),
        "canonical_id": "drive-wwid-naa.5000039c88d0351a",
        "other_sensor_ids": [
            "drive-wwid-naa.5000039c88d0352b",
            "drive-wwid-naa.5000039c88d0353c",
        ],
        "virtual_id": "virtual/drive-array",
        "fan_id": "hwmon-pwm-test/pwm1",
    }
