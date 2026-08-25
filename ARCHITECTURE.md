# Fan Control Service — Architecture Document

**Status:** v1.1.0
**Last Updated:** August 25, 2026

---

## Problem Statement

TrueNAS SCALE locks down package management, making it impossible to install fan control software via standard system tools (`apt`, `pip`). Existing solutions like CoolerControl assume a general-purpose Linux environment. USB fan controllers with good Linux support rely on `liquidctl` to operate — which cannot be installed on TrueNAS natively.

This project solves exactly that problem: a self-contained Docker-based fan control service for TrueNAS SCALE, targeting any fan controller supported by liquidctl, any motherboard PWM fan header exposed via hwmon, and any temperature source exposed via hwmon. It is not a general-purpose fan control solution.

---

## Scope

**In scope:**
- Any USB fan controller supported by liquidctl (Aquacomputer Quadro is the primary tested device)
- Motherboard PWM fan headers exposed via `/sys/class/hwmon` (tested: Nuvoton NCT6687 Super I/O chip)
- TrueNAS SCALE as the primary target platform
- Any Linux host where Docker runs with USB access
- Temperature sources: any sensor exposed via `/sys/class/hwmon` (coretemp, drivetemp, nvme, etc.)
- Virtual sensors: computed avg/min/max from groups of real sensors
- Fan control: all output channels on any liquidctl-supported device, plus any writable `pwmN` sysfs channel

**Out of scope:**
- Controllers not supported by liquidctl or hwmon
- General Linux fan control (CoolerControl, fancontrol, etc. solve that problem better on standard distros)
- Non-Docker deployments

---

## Name

**Brisa** — generic, evokes airflow, not tied to any specific hardware or platform.

---

## Architecture Overview

Single Docker container. Three logical components running together:

```
┌─────────────────────────────────────────────────┐
│                  Docker Container               │
│                                                 │
│  ┌─────────────┐   ┌──────────────────────────┐ │
│  │  Controller │   │      FastAPI Server      │ │
│  │    Loop     │   │  (Web UI + REST API)     │ │
│  │             │   │                          │ │
│  │  reads temps│   │  /          → Web UI     │ │
│  │  resolves   │   │  /api/state → grouped    │ │
│  │  virtual    │   │  /api/history → SQLite   │ │
│  │  sensors    │   │  /api/config → R/W JSON  │ │
│  │  applies    │   │  /api/apply → force loop │ │
│  │  curves     │   │  /api/devices → detect   │ │
│  │  routes to  │   │  /api/metrics → Prom.    │ │
│  │  backend:   │   │                          │ │
│  │  liquidctl  │   │                          │ │
│  │  or sysfs   │   │                          │ │
│  └──────┬──────┘   └──────────────────────────┘ │
│         │                                       │
│  ┌──────▼──────────────────────────────────────┐│
│  │              SQLite Database                ││
│  │  (temp history, fan speed history)          ││
│  └─────────────────────────────────────────────┘│
│                                                 │
│  Volumes:                                       │
│    /data/config.json  ← full config             │
│    /data/history.db   ← SQLite                  │
│  Devices:                                       │
│    /dev/bus/usb (privileged)                    │
│    /sys/class/hwmon (read-write for PWM fans)  │
└─────────────────────────────────────────────────┘
```

The controller loop runs as an asyncio background task inside the Uvicorn process — one process, no supervisor. An outer pure-ASGI authentication middleware covers the root `StaticFiles` UI, API, generated docs, metrics, and framework responses from one enforcement boundary.

---

## Technology Stack

| Component | Choice | Reason |
|-----------|--------|--------|
| Language | Python 3.12.13 | Runs liquidctl as subprocess, reads /sys natively, serves web, handles JSON/SQLite — one language for everything |
| Web framework | FastAPI 0.141.1 + Starlette 1.3.1 | Lightweight ASGI routing, protected OpenAPI/Swagger docs, and patched static file serving |
| ASGI server | Uvicorn 0.34.0 | Standard FastAPI deployment, minimal overhead |
| Data validation | Pydantic 2.11.1 | Config validation and serialization |
| Password hashing | argon2-cffi 25.1.0 | Validates and verifies the administrator's Argon2id password hash without storing a plaintext password |
| Database | SQLite (stdlib) | No separate service, file-based, survives container restarts, more than adequate for time-series at minute intervals |
| Fan control | liquidctl 1.13.0 (subprocess) | Only reliable way to control USB fan controllers on Linux; subprocess is intentional — no stable Python API exists |
| Fan control | hwmon sysfs (direct write) | Controls motherboard PWM fan headers via `/sys/class/hwmon/hwmonN/pwmN`; no additional dependencies |
| Frontend | Vanilla JS + Chart.js 4.4.0 | No framework needed for this scope; Chart.js handles all graphing; keeps image small |
| Base image | python:3.12.13-slim | Minimal Debian base; pinned to a current security patch version |

**Why not a compiled language (Go, Rust)?** liquidctl is Python-only. Wrapping it from another language adds complexity with no benefit.

**Why not Node/Bun for the backend?** Reading `/sys/class/hwmon` and shelling out to liquidctl is more natural in Python. Avoiding two runtimes in the image.

**Why subprocess for liquidctl?** No stable Python API exists for liquidctl. Subprocess is explicit and predictable. The `--direct-access` flag is passed on all control commands to suppress kernel driver fallback warnings on the Aquacomputer Quadro.

**Why exactly one Uvicorn worker?** The fan controller loop, opaque sessions, login rate limits, and current controller state are in process memory. Multiple workers would create independent session stores and duplicate fan-control loops. The image therefore explicitly uses `--workers 1`; multiple replicas are unsupported for the same reason.

**Why disable Uvicorn proxy headers?** Brisa must retain the real immediate TCP peer until its own authentication layer decides whether that peer belongs to an explicitly trusted proxy CIDR. Uvicorn runs with `--no-proxy-headers` so generic forwarded-header processing cannot rewrite the peer first.

---

## Authentication Architecture

Authentication is optional and disabled by default for compatibility with existing deployments. Disabled mode logs a warning and leaves the entire management surface reachable, so it is suitable only where network controls provide the security boundary. If authentication is enabled but its configuration or hash is invalid, management endpoints fail closed with HTTP 503 while the independently started fan-control loop continues.

The administrator password is represented only by a one-record Argon2id hash read from the absolute path in `BRISA_PASSWORD_HASH_FILE`. The recommended deployment mounts a secrets directory read-only with the hash as a file inside it. Hashes are generated interactively with Python `getpass` and `argon2.PasswordHasher`; plaintext passwords are never accepted through an environment variable or configuration file.

Successful login creates a random, opaque session token. Sessions are process-local and memory-only, with a maximum of 16 active sessions. Their lifetime is absolute rather than sliding: `BRISA_SESSION_TTL_SECONDS` defaults to `28800` (eight hours), and activity does not extend expiration. Restarting the process logs out every client; explicit logout removes that session immediately. The browser cookie is `HttpOnly`, `SameSite=Lax`, and secure by default.

Authenticated unsafe methods require the session's CSRF token in the `X-CSRF-Token` header. The UI obtains that token from `/api/auth/me`. The middleware protects UI pages, REST routes, `/docs`, `/openapi.json`, `/metrics`, and `/api/metrics`; only login resources and the login submission route are public while auth is ready.

Login failures are keyed by effective client IP. Five credential failures in ten minutes produce a 15-minute block. Argon2 work is serialized and capped at 30 verification starts per minute to bound CPU and memory pressure. Those rate-limited responses return HTTP 429 with `Retry-After`. The login body is limited to 8192 bytes and a ten-second whole-body read deadline before JSON parsing.

By default, the effective client is the immediate peer and forwarded headers are ignored. `BRISA_TRUST_PROXY=true` requires comma-separated explicit networks in `BRISA_TRUSTED_PROXY_CIDRS`. Forwarding is accepted only when the immediate peer is trusted, then evaluated right-to-left through trusted hops. For Nginx Proxy Manager, operators must inspect the NPM container on the network shared with Brisa and trust the measured stable peer as `/32` or `/128`, or the smallest reviewed shared-network CIDR. Deployment-specific hostnames and proxy addresses are examples only, never universal defaults.

Direct trusted-LAN HTTP requires `BRISA_SECURE_COOKIES=false`; public or reverse-proxied HTTPS uses `true`, even when the internal proxy hop is HTTP. There is no reset UI, recovery email, or recovery token. Recovery consists of generating and atomically replacing the hash secret, then restarting Brisa, which also invalidates all sessions.

Frontend rendering builds untrusted dashboard values with DOM properties rather than HTML parsing. The static sidebar is the sole intentional HTML template and has no runtime interpolation. Security responses include `X-Content-Type-Options: nosniff`.

---

## Data Model

### config.json

```json
{
  "settings": {
    "interval_seconds": 60,
    "history_days": 30,
    "safety_floor_percent": 30
  },
  "curves": [
    {
      "name": "silent",
      "points": [
        {"temp": 30, "percent": 20},
        {"temp": 50, "percent": 50},
        {"temp": 70, "percent": 100}
      ]
    }
  ],
  "fan_configs": [
    {
      "fan_id": "fan1",
      "fan_label": "Upper rear left",
      "curve_name": "silent",
      "sensor_id": "virtual/all-drives-max",
      "override_percent": null,
      "backend": "liquidctl"
    },
    {
      "fan_id": "hwmon-pwm-nct6687.2592/pwm1",
      "fan_label": "CPU Fan",
      "curve_name": "silent",
      "sensor_id": "k10temp-hwmon3/Tctl",
      "override_percent": null,
      "backend": "hwmon-pwm"
    }
  ],
  "sensor_aliases": {
    "nvme-hwmon1/Sensor 1": "NVMe Boot Drive",
    "drive-wwid-naa.5000000000000001": "NAS Drive 1"
  },
  "virtual_sensors": [
    {
      "id": "virtual/all-drives-max",
      "name": "All Drives Max",
      "source_sensor_ids": [
        "drive-wwid-naa.5000000000000001",
        "drive-wwid-naa.5000000000000002"
      ],
      "aggregation": "max"
    }
  ],
  "dashboard_groups": [
    {
      "id": "grp-exhaust-m4k1a",
      "name": "Exhaust",
      "type": "fan",
      "item_ids": ["fan1", "fan2"]
    },
    {
      "id": "grp-cpu-b7x2p",
      "name": "CPU",
      "type": "sensor",
      "item_ids": ["coretemp-hwmon0/Core 0", "coretemp-hwmon0/Core 1"]
    }
  ],
  "card_colors": {
    "fan1": "teal",
    "virtual/all-drives-max": "amber"
  }
}
```

**`override_percent`** — when set to an integer, the controller applies that fixed speed to the fan every loop iteration, bypassing the sensor read, curve interpolation, and safety floor entirely. The curve and sensor assignments are preserved so they can be restored by clearing the override.

**`backend`** — determines how the fan is controlled. `"liquidctl"` for USB fan controllers (Aquacomputer Quadro, etc.), `"hwmon-pwm"` for motherboard PWM fan headers controlled via sysfs. The backend dictates which code path handles speed writes, RPM reads, and device lifecycle (initialization, shutdown).

**`sensor_aliases`** — display-only map from canonical sensor ID to a human-readable name. IDs are used internally everywhere; aliases are applied at the UI/API layer only. Aliases for offline sensors remain available to configuration editors.

**`virtual_sensors`** — computed sensors that aggregate multiple physical sensors. Each has a slug-like ID prefixed with `virtual/`, a display name, a list of source sensor IDs, and an aggregation mode (`avg`, `min`, `max`). Virtual sensors can be used as `sensor_id` in `fan_configs` just like physical sensors. Nesting is not allowed. Physical sources may come from hwmon or smartctl. If some source sensors are unavailable, the virtual sensor computes from whatever sources are present; it only fails (triggering safety floor) when all sources are missing.

**`dashboard_groups`** — ordered list of named groups for the dashboard. Each group has a `type` (`sensor` or `fan`) and a list of `item_ids` that belong to it. Groups are displayed in list order. Items not in any group appear in an "Ungrouped" section at the bottom. If no groups are defined, the dashboard falls back to showing all configured fans and their associated sensors (backward compatible).

**`card_colors`** — optional map from sensor or fan ID to a color key. Valid colors: `teal`, `blue`, `purple`, `pink`, `amber`, `orange`, `red`, `slate`. Colors render as a left-border accent on dashboard cards. Items without a color assignment have no accent border.

### SQLite schema

```sql
CREATE TABLE readings (
    ts        INTEGER NOT NULL,  -- unix timestamp
    sensor_id TEXT NOT NULL,
    temp      REAL NOT NULL
);

CREATE TABLE fan_readings (
    ts         INTEGER NOT NULL,  -- unix timestamp
    fan_id     TEXT NOT NULL,
    percent    INTEGER NOT NULL,
    rpm        REAL
);

CREATE INDEX idx_readings_ts ON readings(ts);
CREATE INDEX idx_fan_readings_ts ON fan_readings(ts);
```

Old rows are pruned on each loop iteration based on `history_days` setting.

During database initialization, recognized legacy smartctl/drivetemp IDs in `readings.sensor_id` are transactionally migrated to canonical drive IDs. The migration is syntactic, idempotent, and does not require live hardware. It preserves every timestamp and temperature, does not touch `fan_readings`, and may leave multiple rows with the same canonical ID and timestamp because the schema has no uniqueness constraint.

---

## Virtual Sensors

Virtual sensors are resolved in `controller.py` via `resolve_virtual_sensors()`, called once per loop iteration before curve interpolation.

**Resolution rules:**
- For each virtual sensor, collect temperatures from all source sensors present in the current hwmon scan
- If at least one source has a reading, compute the aggregation (avg/min/max) from available sources
- If all sources are missing, the virtual sensor produces no value — the controller treats this as a missing sensor and applies the safety floor
- Missing individual sources are logged at `debug` level; all-missing is logged at `warning` level

**Validation rules (enforced on `POST /api/config`):**
- At least 2 distinct source sensors required
- Physical sources may be offline; availability is not structural validity
- No referencing other virtual sensors (no nesting)
- No duplicate virtual sensor IDs
- Aggregation must be `avg`, `min`, or `max`

Virtual sensors appear in:
- `/api/devices` response (under `virtual_sensors` key, with computed temps)
- `/api/state` response (in sensor groups or ungrouped sensors, with computed temps)
- `/api/metrics` output (with `sensor="virtual"` label)
- Fan config sensor selector in the UI (grouped separately from physical sensors)

---

## Auto-Detection

On startup (and via `GET /api/devices`), the service detects:

**Temperature sensors** — scan `/sys/class/hwmon/hwmon*`, then use smartctl for uncovered drives:
- Read `name` file to identify driver
- Read available `tempN_input` files
- Read `tempN_label` if present (e.g. "Package id 0", "Core 0")
- For drives, correlate the source with a stable physical identifier and produce `drive-wwid-<normalized-wwid>`, or `drive-serial-<normalized-serial>` when WWID is unavailable
- Both drivetemp and smartctl call the same identity builder; duplicate observations use one canonical ID and prefer drivetemp
- Backend, model, `/dev/sdX`, hwmon number, and label remain metadata only: `{ id, driver, label, model?, block_device?, current_temp }`
- If neither WWID nor serial is exposed, Brisa retains an explicitly unstable backend-local fallback ID and logs a warning; no stability is claimed for that sensor

**Why WWID for drive IDs?** hwmon numbers and block-device letters can change across boots, while model strings may differ between sysfs and smartctl. WWID is therefore preferred for persistent identity. Serial is used only when WWID is absent. A serial-identified drive that later exposes a WWID is not automatically treated as equivalent because Brisa cannot safely infer that relationship without persistent hardware evidence.

**Config migration:** On startup, `load_config()` syntactically recognizes legacy `smartctl-wwid-*`, `drivetemp-wwid-*`, old `/sdX — model` drivetemp forms, and unambiguous serial forms. It rewrites aliases, virtual sources, fan sensor references, dashboard items, and card-color keys without consulting live hardware. Ordered lists are deduplicated after migration. Canonical mapping keys take precedence over legacy keys; otherwise conflicting legacy keys use a deterministic lexical winner and emit a warning. The config is atomically rewritten only when something changed. Ambiguous IDs remain unchanged with a warning.

**Drive replacement:** A different WWID or serial is a different physical sensor. Brisa never substitutes a newly detected drive for an offline configured drive based on model, bay, or device letter. The UI keeps missing references visible so the user can explicitly remove the old source and select the replacement.

**Fans (liquidctl backend)** — query liquidctl:
- Run `liquidctl list --json` to find connected devices
- Run `liquidctl --direct-access status --json` to enumerate fan channels and current RPM
- Parse `Fan N speed` entries from the status output
- Return structured list: `{ id, label, current_rpm, backend: "liquidctl" }`
- If no liquidctl devices are present, returns empty list (no error)

**Fans (hwmon-pwm backend)** — scan `/sys/class/hwmon/hwmon*`:
- For each hwmon device, skip if driver name matches a liquidctl-managed device (blocklist: `quadro`, `octo`, `d5next`, `kraken`, `smart_device`) to avoid duplicate detection
- Check for `pwmN` and `pwmN_enable` files; skip if `pwmN` is not writable
- Build a stable fan ID from the platform device path: `hwmon-pwm-<driver>.<address>/<pwmN>` (e.g. `hwmon-pwm-nct6687.2592/pwm1`). The hwmonN number is not used in the ID because it can change across reboots
- Read `fanN_input` for current RPM, `fanN_label` for driver-provided label
- Return structured list: `{ id, label, current_rpm, backend: "hwmon-pwm" }`

**Why stable IDs for hwmon-pwm fans?** The kernel assigns `hwmonN` numbers at boot based on driver load order. The platform device component (e.g. `nct6687.2592`) is derived from the device's physical bus address and is stable across reboots — same approach as the WWID scheme used for drivetemp sensors.

**Deduplication:** Some USB fan controllers (like the Aquacomputer Quadro) have both a liquidctl interface and a kernel hwmon driver (`aquacomputer_hwmon`). These expose the same fans through both paths. The hwmon-pwm scanner skips any hwmon device whose driver name is in the blocklist, ensuring each fan appears only once and is controlled by the more capable liquidctl backend.

No hardcoded sensor or fan names anywhere in the codebase.

---

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/auth/login` | Validate administrator credentials and create an opaque in-memory session |
| GET | `/api/auth/me` | Return session identity, CSRF token, and version |
| POST | `/api/auth/logout` | Invalidate the current session and clear its cookie |
| GET | `/api/state` | Grouped dashboard data: sensor_groups, fan_groups, ungrouped_sensors, ungrouped_fans — each item includes color |
| GET | `/api/history` | Time series; params: `hours` (default 24) |
| GET | `/api/config` | Full config (curves + fan assignments + settings + aliases + virtual sensors + groups + colors) |
| POST | `/api/config` | Save new config; structurally validated before write, with offline physical references allowed |
| GET | `/api/devices` | All detected sensors (with aliases), virtual sensors (with computed temps), and fans |
| POST | `/api/apply` | Trigger immediate controller loop iteration; does not affect loop timer |
| GET | `/api/metrics` | Prometheus text format (includes virtual sensors with `sensor="virtual"`) |
| GET | `/docs` | Auto-generated OpenAPI docs (FastAPI built-in) |

Except for login resources and `POST /api/auth/login`, the table's endpoints require a valid session when authentication is enabled. Unsafe methods additionally require `X-CSRF-Token`. Metrics and generated documentation are not public exceptions.

### /api/state response structure

```json
{
  "fan_groups": [
    {
      "id": "grp-exhaust-m4k1a",
      "name": "Exhaust",
      "items": [
        {
          "id": "fan1",
          "label": "Upper rear left",
          "current_rpm": 850.0,
          "override_percent": null,
          "last_percent": 45,
          "color": "teal"
        }
      ]
    }
  ],
  "sensor_groups": [
    {
      "id": "grp-cpu-b7x2p",
      "name": "CPU",
      "items": [
        {
          "sensor_id": "coretemp-hwmon0/Core 0",
          "alias": "CPU Core 0",
          "temp": 42.0,
          "virtual": false,
          "color": null
        }
      ]
    }
  ],
  "ungrouped_fans": [],
  "ungrouped_sensors": []
}
```

### Config validation

`POST /api/config` validates structural relationships before writing. Current hardware availability is deliberately not a validity requirement:
- Every `fan_config.curve_name` must exist in `curves`
- A virtual `fan_config.sensor_id` must name a defined virtual sensor; physical sensor references may be offline
- Every `fan_config.backend` must be `"liquidctl"` or `"hwmon-pwm"`
- Every curve must have at least 2 points in ascending temperature order
- Virtual sensors must have at least 2 distinct physical source references; those sources may be offline
- Virtual sensors cannot reference other virtual sensors
- No duplicate virtual sensor IDs or dashboard group IDs
- Card colors must be from the valid set: teal, blue, purple, pink, amber, orange, red, slate
- Dashboard group types must be `sensor` or `fan`

Structural violations are rejected with HTTP 422. Offline physical sensors and fans remain saveable so users can repair configurations while hardware is disconnected. At runtime, missing physical control sensors and virtual sensors with no available members continue to invoke the existing safety floor.

### /api/metrics format (Prometheus)

```
# HELP brisa_temperature_celsius Current temperature reading
# TYPE brisa_temperature_celsius gauge
brisa_temperature_celsius{sensor="coretemp",label="Package id 0"} 38.0
brisa_temperature_celsius{sensor="virtual",label="All Drives Max"} 45.0

# HELP brisa_fan_rpm Current fan RPM
# TYPE brisa_fan_rpm gauge
brisa_fan_rpm{fan="fan1",label="Upper rear left"} 850.0
```

Aliases are applied to labels in the metrics output when set. Virtual sensors use `sensor="virtual"`.

---

## Web UI Pages

### Dashboard
- Organized into **categories** (Fan Speeds, Temperatures) with uppercase section labels and a dividing line
- Within each category, **named groups** are displayed with a teal accent bar and group title
- Items not in any group appear under "Other" at the bottom of each category
- If no groups are defined, falls back to flat display of all configured fans and sensors
- Card accent colors rendered as a left border per card
- `VIRTUAL` badge on virtual sensor cards
- `MANUAL` badge on fan cards when override is active
- Current RPM and applied % per fan; current temperature per sensor
- Live dot + polling every 10 seconds
- "Apply Now" button → POST /api/apply

### Sensors & Fans
- All detected sensors with driver, sensor ID, current temperature
- Inline alias editing per sensor row (click ✎ on the left, type alias, Enter or Save)
- Alias shown as primary label; original sensor ID always visible below it
- **Color picker** per sensor and fan row — 8 color dots + "none"
- **Virtual Sensors** section — create, edit, delete; select aggregation mode and source sensors
- All detected fan channels with current RPM and color picker
- **Dashboard Groups** section at the bottom — create sensor or fan groups, assign items, reorder with ▲/▼

### Curves
- List of defined curves with Chart.js line preview
- Inline editable curve name
- Add / edit / delete points; chart updates on field blur (not on every keystroke)
- Add / remove points without losing scroll position
- Delete blocked if curve is assigned to any fan config
- Explicit Save / Discard flow — no auto-save on edit

### Fan Configuration
- Table of fan assignments with override status column
- Sensor column shows virtual sensor names when applicable
- Add / Edit modal: fan selector, label, sensor selector (real sensors + virtual sensors under separator), curve selector
- Manual override toggle: when enabled, a fixed percent input replaces curve control
- Override bypasses sensor read, curve interpolation, and safety floor entirely

### History
- Chart.js line graphs: temp over time per sensor, fan % over time per fan
- Time range selector: 1h / 6h / 24h / 7d
- Data from GET /api/history

### Settings
- Interval (seconds), history retention (days), safety floor (%)
- Warning displayed if estimated row count exceeds 5M
- Save → POST /api/config

---

## Controller Loop

```
on startup:
  load config.json
  init SQLite database
  run liquidctl --direct-access initialize all (non-fatal if no devices)
  for each fan_config:
    if backend is hwmon-pwm: take over fan (save original pwmN_enable, write 1)
    if override_percent is set: apply override_percent
    else: apply safety_floor_percent
  start asyncio background task

every interval_seconds:
  scan all hwmon sensors once (single pass)
  resolve virtual sensors from real sensor readings:
    for each virtual sensor:
      collect temps from available source sensors
      if at least one source present: compute avg/min/max
      if all sources missing: skip (no value produced)
  merge real + virtual sensor maps
  for each fan_config:
    if override_percent is set:
      apply override_percent (no sensor read, no curve, no safety floor)
    else:
      read temp from merged sensor map
      if sensor not found (real missing, or virtual with all sources missing):
        apply safety_floor_percent
        log warning
        continue
      compute percent via linear interpolation on curve points
      route to backend:
        liquidctl: call liquidctl --direct-access set <fan_id> speed <percent>
        hwmon-pwm: write round(percent * 255 / 100) to /sys/class/hwmon/hwmonN/pwmN
      cache applied percent in memory (_last_applied dict)
  collect RPMs from all backends (liquidctl status + fanN_input reads)
  write sensor readings to SQLite (deduplicated by sensor_id)
  write fan readings to SQLite (percent + RPM)
  prune old rows (> history_days)

on graceful shutdown (SIGTERM / docker stop):
  for each hwmon-pwm fan that was taken over:
    restore original pwmN_enable value (e.g. 99 for nct6687 firmware mode)
  cancel controller loop task
```

**Safety floor semantics:** the safety floor is a fallback for sensor failure only. It is never applied to manually overridden fans. For virtual sensors, it triggers only when all source sensors are unavailable.

**Single sensor scan per iteration:** `detect_sensors()` is called once per loop iteration, not once per fan. The result is shared across all fan configs in that iteration. Virtual sensor resolution also happens once, before the per-fan loop.

Interpolation is linear between each adjacent pair of curve points. Below the first point, the first point's percent is used. Above the last point, the last point's percent is used.

---

## Project Structure

```
brisa/
├── ARCHITECTURE.md
├── README.md
├── LICENSE
├── docker-compose.yml
├── brisa/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app/
│       ├── main.py              ← FastAPI app, lifespan, global config/loop state
│       ├── auth.py              ← Argon2 verification, sessions, rate limits, trusted-proxy/CSRF middleware
│       ├── models.py            ← Pydantic models (AppConfig, FanConfig with backend field, Curve, VirtualSensor, DashboardGroup, etc.)
│       ├── config.py            ← load/save/structurally validate config.json, migrate persisted sensor references
│       ├── sensor_ids.py        ← canonical drive identity builder and shared legacy-ID parser
│       ├── controller.py        ← loop logic, backend routing, interpolation, virtual sensor resolution, _last_applied cache
│       ├── sensors.py           ← /sys/class/hwmon temperature reader + drivetemp enrichment (renamed from hwmon.py)
│       ├── hwmon_pwm.py         ← sysfs PWM fan detection, control, takeover/release lifecycle
│       ├── liquidctl_wrapper.py ← subprocess wrapper for liquidctl
│       ├── database.py          ← SQLite init, history ID migration, read/write, prune
│       ├── api/
│       │   ├── auth_routes.py   ← login, current-session/CSRF metadata, logout
│       │   └── routes.py        ← fan-control API endpoints (grouped state, virtual sensor dicts, card colors)
│       └── static/              ← vanilla JS + HTML pages
│           ├── style.css         ← theme, card colors, dashboard group/category styles
│           ├── app.js
│           ├── login.js
│           ├── logo.png
│           ├── logo_text.png
│           ├── favicon.png
│           ├── favicon.ico
│           ├── index.html        ← Dashboard (grouped layout, card colors)
│           ├── login.html        ← Administrator login
│           ├── devices.html      ← Sensors & Fans (aliases, colors, virtual sensors, dashboard groups)
│           ├── curves.html       ← Curves
│           ├── fanconfig.html    ← Fan Configuration (virtual sensor support in selector)
│           ├── history.html      ← History
│           └── settings.html     ← Settings
└── tests/
```

---

## Deployment

### docker-compose.yml

```yaml
services:
  brisa:
    image: ghcr.io/harrentheblack/brisa:1.1.0
    container_name: brisa
    restart: unless-stopped
    privileged: true
    network_mode: bridge
    ports:
      - "9595:9595"
    environment:
      BRISA_AUTH_ENABLED: "true"
      BRISA_AUTH_USERNAME: "admin"
      BRISA_PASSWORD_HASH_FILE: /run/secrets/brisa/password_hash
      # Use "false" only for temporary direct HTTP testing on a trusted LAN.
      BRISA_SECURE_COOKIES: "true"
      BRISA_SESSION_TTL_SECONDS: "28800"
      # Keep false until the actual NPM peer is measured.
      BRISA_TRUST_PROXY: "false"
      # Required only when BRISA_TRUST_PROXY=true; use the measured proxy CIDR.
      BRISA_TRUSTED_PROXY_CIDRS: ""
    volumes:
      - /some/data/path:/data
      - /some/secrets/path:/run/secrets/brisa:ro
```

The host secrets directory is mounted read-only. Its `password_hash` file contains only the encoded Argon2id hash. Generate it interactively with `getpass` and `argon2.PasswordHasher` as shown in the README; never store a plaintext password. This example is for HTTPS deployments. Temporary direct HTTP testing on a trusted LAN sets `BRISA_SECURE_COOKIES=false`; proxy trust remains disabled until a measured, explicit proxy CIDR is configured.

### Volume layout

```
/data/
  config.json    ← curves, fan assignments, settings, aliases, virtual sensors, groups, colors
  history.db     ← SQLite database
```

### TrueNAS-specific notes

- `privileged: true` required for USB access to the fan controller and sysfs writes for hwmon-pwm fans
- Do not use TrueNAS Apps UI — deploy via `docker compose` only
- `truenas_admin` must be in docker group
- `/data` should be on an NVMe pool, not spinning rust (SQLite = small random I/O)
- hwmon-pwm fans require a Super I/O chip with a loaded kernel driver (e.g. `nct6775`, `it87`, `nct6687`); many NAS-specific boards (e.g. Topton N22) lack these chips

### Podman deployment

Podman runs rootless by default on most Linux distributions. Rootless mode uses a user namespace where `--privileged` does not grant real host root — sysfs writes will fail silently and hwmon-pwm fans will not be controllable.

For hwmon-pwm fan control with Podman, run as real root:

```bash
sudo podman run --privileged \
  -v /sys:/sys \
  -p 9595:9595 \
  -v /path/to/data:/data \
  -v /path/to/secrets:/run/secrets/brisa:ro \
  -e BRISA_AUTH_ENABLED=true \
  -e BRISA_AUTH_USERNAME=admin \
  -e BRISA_PASSWORD_HASH_FILE=/run/secrets/brisa/password_hash \
  -e BRISA_SECURE_COOKIES=true \
  -e BRISA_SESSION_TTL_SECONDS=28800 \
  -e BRISA_TRUST_PROXY=false \
  ghcr.io/harrentheblack/brisa:1.1.0
```

The `-v /sys:/sys` bind mount may be needed with Podman even in rootful mode, as Podman's default sysfs mount can be read-only. Docker does not require this — its `--privileged` flag grants full sysfs access by default.

If only using liquidctl (USB) fans, rootless Podman may work, but host USB ACLs, supplementary groups, and SELinux device policy still apply to the invoking user.

---

## Image Details

The Dockerfile uses a multi-stage build. Build tools (`make`, `gcc`, `libc-dev`) are only present in the builder stage and are not included in the final image.

| Layer | Expected size |
|-------|--------------|
| python:3.12.13-slim base | ~130MB |
| libusb + udev (runtime only) | ~5MB |
| liquidctl + deps (incl. pillow) | ~45MB |
| FastAPI + uvicorn + pydantic | ~15MB |
| App code + static files | ~5MB |
| **Total** | **~195MB** |

---

## What This Is Not

- Not a replacement for CoolerControl, fancontrol, or nbfc
- Not a general-purpose fan control solution — targets TrueNAS SCALE and Docker-based deployments
- Not a full observability platform — use Prometheus + Grafana if you need that; `/api/metrics` gives you the integration point

---

## Security Considerations

### Privileged container

Brisa requires `privileged: true` to access USB devices and sysfs. A privileged container has effectively root access to the host, including:

- Full access to all host devices (`/dev/*`)
- Ability to read and write any sysfs path (not just hwmon — also power management, PCI config, etc.)
- Ability to mount filesystems and load kernel modules
- Effectively equivalent to root on the host

This is an inherent requirement for hardware fan control from within a container. There is no way to control USB fan controllers or write to sysfs PWM files without elevated privileges. The same privilege level is required by any containerized fan control solution.

**Mitigation:** Enable authentication, use HTTPS outside direct trusted-LAN deployments, and restrict port 9595 with a firewall, reverse proxy, or VPN. Generated API docs and metrics are covered by the same session boundary as the UI and API. Authentication reduces unauthorised access but does not sandbox the service: an application compromise can become host compromise because the container is privileged. Public exposure therefore carries materially greater risk than an ordinary web application.

Brisa deliberately runs as one Uvicorn worker and one container replica. Do not increase the worker count or horizontally scale it: doing so duplicates the hardware controller loop and fragments in-memory sessions and rate limits. Uvicorn proxy-header parsing is disabled so Brisa can validate the immediate peer itself, and the container command caps concurrent connections/tasks to reduce slow-client resource pressure.

### hwmon-pwm sysfs writes

The hwmon-pwm backend writes to `/sys/class/hwmon/hwmonN/pwmN` and `/sys/class/hwmon/hwmonN/pwmN_enable`. These writes only affect fan speed and control mode for the specific PWM channel. No other sysfs paths are written to by the application. Adding hwmon-pwm support does not increase the container's privilege level — the `privileged: true` flag already grants full sysfs access regardless of whether the application uses it.

---

## Known Limitations

### Container crash behavior (hwmon-pwm)

If the container is killed without a graceful shutdown (OOM kill, `docker kill -9`, kernel panic, power loss), hwmon-pwm fans remain at their last-written PWM duty cycle and control mode (`pwmN_enable = 1`). The BIOS/firmware fan curves do not resume until:

- The system is rebooted (BIOS re-initializes all Super I/O registers), or
- Another tool writes `pwmN_enable` back to the firmware value (e.g. `99` for nct6687, `2` for most other drivers)

This does not apply to liquidctl fans — USB controllers like the Quadro have their own firmware that continues operating independently.

**Mitigation:** Use `restart: unless-stopped` in `docker-compose.yml`. On container startup, Brisa takes over configured fans (saving the original enable value) and restores them on graceful shutdown. A restart after a crash will re-take-over the fans and resume normal operation.

### Motherboard without Super I/O driver

Many NAS-specific motherboards (e.g. Topton N22 with Intel N100/N305) use minimal embedded controllers for fan management instead of a traditional Super I/O chip. These boards may have BIOS-level fan control but no Linux kernel driver to expose sysfs PWM files. On such systems, hwmon-pwm detection will find zero controllable fans. This is a kernel/hardware limitation, not a Brisa limitation.

### Non-writable PWM channels

Some hwmon devices expose `fanN_input` (RPM reading) without a corresponding writable `pwmN` file. This can occur when the kernel driver supports monitoring but not control, or when the BIOS has locked the PWM registers. Brisa only lists fans where both `pwmN` and `pwmN_enable` exist and `pwmN` is writable.

---

## Open Questions / v3 Backlog

- [ ] Hysteresis support in curves (fans only spin down below X, only spin up above Y)
- [ ] Multi-device support (multiple liquidctl controllers simultaneously)
- [ ] NVMe and other PCI sensor hwmon numbers are stable in practice but not guaranteed — WWID-style stable IDs for those sensors would be a future improvement
- [ ] GPU fan control via amdgpu hwmon (detected but currently skipped — needs testing and safety review)
- [ ] Expand hwmon-pwm deduplication blocklist as more liquidctl-backed devices are reported
