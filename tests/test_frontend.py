from pathlib import Path

import pytest


STATIC_ROOT = Path(__file__).resolve().parents[1] / "brisa" / "app" / "static"


@pytest.mark.regression
def test_virtual_editor_preserves_and_labels_missing_sources():
    app_js = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")
    devices = (STATIC_ROOT / "devices.html").read_text(encoding="utf-8")

    assert "buildPhysicalSensorChoices" in app_js
    assert "retainedIds || []" in app_js
    assert "Missing sensor —" in devices
    assert "selectedIds.includes(sensor.id)" in devices
    assert "Unavailable" in devices
    assert "escapeHtml(sensor.label)" in devices


@pytest.mark.regression
def test_virtual_deletion_keeps_explicit_fan_dependency_block():
    app_js = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")
    devices = (STATIC_ROOT / "devices.html").read_text(encoding="utf-8")

    assert "Cannot delete" in app_js
    assert "It is currently used by" in app_js
    assert "Reassign those fan configurations first" in app_js
    assert "virtualSensorDependencyMessage" in devices


@pytest.mark.regression
def test_fan_editor_retains_unavailable_assignment_without_substitution():
    fanconfig = (STATIC_ROOT / "fanconfig.html").read_text(encoding="utf-8")

    assert "configuredPhysicalSensorIds(config)" in fanconfig
    assert "existing?.sensor_id === sensor.id" in fanconfig
    assert "unavailable physical" in fanconfig
    assert "Select a replacement explicitly" in fanconfig
    assert "escapeHtml(sensorDisplayName(fc.sensor_id))" in fanconfig
