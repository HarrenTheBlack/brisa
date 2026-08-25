from pathlib import Path
import re

import pytest


STATIC_ROOT = Path(__file__).resolve().parents[1] / "brisa" / "app" / "static"
HOSTILE_PAYLOAD = '\"><img src=x onerror=alert(1)>'
XSS_TARGETS = (
    "app.js",
    "index.html",
    "devices.html",
    "curves.html",
    "fanconfig.html",
    "history.html",
    "settings.html",
    "login.html",
    "login.js",
)


@pytest.mark.regression
def test_virtual_editor_preserves_and_labels_missing_sources():
    app_js = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")
    devices = (STATIC_ROOT / "devices.html").read_text(encoding="utf-8")

    assert "buildPhysicalSensorChoices" in app_js
    assert "retainedIds || []" in app_js
    assert "Missing sensor —" in devices
    assert "selectedIds.includes(sensor.id)" in devices
    assert "Unavailable" in devices
    assert "sensorLabel.textContent" in devices
    assert "checkbox.value = sensor.id" in devices
    assert "list.replaceChildren(...options)" in devices


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
    assert "appendFanCell(row, sensorDisplayName(fc.sensor_id)" in fanconfig
    assert "span.textContent = text" in fanconfig
    assert "option.textContent = label" in fanconfig


@pytest.mark.regression
def test_dynamic_frontend_rendering_has_no_html_or_inline_event_sinks():
    sources = {
        name: (STATIC_ROOT / name).read_text(encoding="utf-8")
        for name in XSS_TARGETS
    }

    for name, source in sources.items():
        assert "insertAdjacentHTML" not in source, name
        assert "document.write" not in source, name
        assert not re.search(r"\beval\s*\(", source), name
        assert not re.search(r"\bnew\s+Function\b", source), name
        assert not re.search(r"set(?:Timeout|Interval)\s*\(\s*['\"]", source), name
        assert not re.search(r"\.setAttribute\(\s*['\"]on[a-z]+", source), name
        assert not re.search(r"\.on[a-z]+\s*=", source), name
        assert not re.search(r"<[^>]+\son(?:click|change|error|load)\s*=", source), name

    assert "sidebar.innerHTML = SIDEBAR_HTML" in sources["app.js"]
    assert len(re.findall(r"\.innerHTML\s*=", sources["app.js"])) == 1
    for name in XSS_TARGETS[1:]:
        assert not re.search(r"\.innerHTML\s*=", sources[name]), name


@pytest.mark.regression
def test_sidebar_html_sink_is_a_static_local_template():
    """The one intentional innerHTML assignment is safe only while its
    template is source-defined, has no runtime interpolation, and pulls no
    remote content. Keep those constraints explicit instead of relying on
    review memory as the sidebar evolves."""
    app_js = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")
    match = re.search(r"const SIDEBAR_HTML = `(?P<html>.*?)`;", app_js, re.DOTALL)
    assert match is not None
    sidebar_html = match.group("html")

    assert "${" not in sidebar_html
    assert not re.search(r"\son[a-z]+\s*=", sidebar_html, re.IGNORECASE)
    assert not re.search(r"\bsrc=[\"']https?://", sidebar_html, re.IGNORECASE)
    assert 'src="/logo.png"' in sidebar_html
    assert 'href="/settings.html"' in sidebar_html
    assert 'href="https://github.com/HarrenTheBlack/brisa"' in sidebar_html


@pytest.mark.regression
def test_hostile_sensor_fan_alias_virtual_curve_and_group_data_uses_dom_properties():
    # Keep the exact exploit-shaped input visible in this regression suite.
    assert HOSTILE_PAYLOAD == '\"><img src=x onerror=alert(1)>'

    app_js = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")
    index = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
    devices = (STATIC_ROOT / "devices.html").read_text(encoding="utf-8")
    curves = (STATIC_ROOT / "curves.html").read_text(encoding="utf-8")
    fanconfig = (STATIC_ROOT / "fanconfig.html").read_text(encoding="utf-8")
    login = (STATIC_ROOT / "login.js").read_text(encoding="utf-8")

    assert "username.textContent = state.username" in app_js
    assert "mobileUsername.textContent = state.username" in app_js

    assert "title.textContent = name" in index
    assert "label.textContent = fan.label || fan.id" in index
    assert "labelElement.textContent = label" in index

    assert "name.textContent = displayLabel" in devices
    assert "appendTextCell(row, vs.label)" in devices
    assert "title.append(document.createTextNode(grp.name))" in devices
    assert "checkbox.value = value" in devices

    assert "name.value = curve.name" in curves
    assert "curve.textContent = curveName" in curves
    assert "fans.textContent = usedBy.join(', ')" in curves

    assert "span.textContent = text" in fanconfig
    assert "option.value = value" in fanconfig
    assert "option.textContent = label" in fanconfig

    assert "error.textContent" in login
    assert "innerHTML" not in login


@pytest.mark.regression
def test_auth_version_storage_and_logout_source_invariants():
    app_js = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")
    login_js = (STATIC_ROOT / "login.js").read_text(encoding="utf-8")
    all_frontend = "\n".join(
        path.read_text(encoding="utf-8")
        for path in STATIC_ROOT.iterdir()
        if path.suffix in {".html", ".js"}
    )

    assert "'/api/auth/login'" in login_js
    assert "JSON.stringify({ username, password })" in login_js
    assert "credentials: 'same-origin'" in login_js
    assert "1.0.1" not in all_frontend
    assert "version.textContent = displayVersion(state.version)" in app_js

    assert "sessionStorage" not in all_frontend
    assert "indexedDB" not in all_frontend
    assert "IndexedDB" not in all_frontend
    assert not re.search(
        r"localStorage\.(?:getItem|setItem)\([^\n]*(?:password|csrf|token|username)",
        all_frontend,
        re.IGNORECASE,
    )

    assert "await api('POST', '/auth/logout')" in app_js
    assert "data-logout>Log out</button>" in app_js
    assert "mobileLogout.dataset.logout = ''" in app_js
    assert "button.addEventListener('click', logout)" in app_js
