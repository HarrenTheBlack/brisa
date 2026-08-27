<p align="center">
  <img src="brisa/app/static/logo_text.png" width="250">
</p>

*v1.2.0*

Brisa is a self-contained Docker service for controlling fans on TrueNAS SCALE (and any other Linux host where you can run Docker but can't install packages directly).

Supports USB fan controllers via [liquidctl](https://github.com/liquidctl/liquidctl), motherboard PWM fan headers via sysfs, and any temperature source exposed via `/sys/class/hwmon`.

---

## Features

- **Docker-only** — no host installs required
- **TrueNAS SCALE** primary target, works on any Linux host with Docker
- **Two fan control backends:**
  - **liquidctl** — USB fan controllers (tested: Aquacomputer Quadro)
  - **hwmon-pwm** — motherboard PWM fan headers via sysfs (tested: Nuvoton NCT6687)
- **hwmon temperature sources** — CPU, NVMe, drives (drivetemp), network adapters, anything the kernel exposes
- **Fan curves** — configurable temperature→speed curves per fan
- **Manual override** — bypass curve control and hold a fixed speed per fan for testing
- **Virtual sensors** — create computed sensors from groups of real sensors using avg, min, or max aggregation; usable in fan configs like any real sensor
- **Sensor aliases** — assign human-readable names to sensors without changing how they're referenced internally
- **Stable drive identification** — drivetemp and smartctl share backend-independent IDs based on WWID, with stable serial as a fallback; device name and model remain display metadata
- **Dashboard groups** — organize sensors and fans into named groups on the dashboard with configurable order
- **Card accent colors** — assign colors to individual sensor and fan cards from a curated palette
- **Web UI** — dashboard, curve editor, fan config, history charts, settings
- **REST API** with auto-generated OpenAPI docs at `/docs`
- **Prometheus metrics** at `/api/metrics` (includes virtual sensors)
- **SQLite history** with configurable retention

---

## Screenshots

| Dashboard | Sensors & Fans |
|:-:|:-:|
| ![Dashboard](https://imgur.com/gjAFiGj.png) | ![Sensors & Fans](https://imgur.com/R1qigY9.png) |

| Curves | History |
|:-:|:-:|
| ![Curves](https://imgur.com/kHOx54W.png) | ![History](https://imgur.com/1YeoXkg.png) |

---

## Disclaimer on AI Usage

I built this project to solve a specific problem in my own TrueNAS SCALE homelab (controlling fans through Docker + liquidctl).

I'm a software engineer, but not very experienced with Python, so I used AI tools to help write part of the code. Everything was reviewed, tested, and adjusted by me before being included here.

I'm sharing this in case it helps someone else with a similar setup. This note is included purely for transparency. It's not meant as a philosophical statement on AI usage or to start any discussion around it.

---

## Requirements

- Docker with `privileged: true`
- At least one of:
  - A USB fan controller supported by liquidctl (tested: Aquacomputer Quadro)
  - Motherboard PWM fan headers with a supported kernel driver (tested: Nuvoton NCT6687; also supports nct6775, it87, w83627ehf, and other Super I/O chips)
- Temperature sensors accessible via `/sys/class/hwmon`

Hardware access is required — there is no simulation mode.

Either backend works independently — you don't need a USB controller to use hwmon-pwm fans, and vice versa.

---

## Quick Start

Brisa publishes a Docker image to GitHub Container Registry:

```text
ghcr.io/harrentheblack/brisa:1.2.0
```

Brisa `v1.2.0` includes mobile navigation and password visibility improvements while retaining the reviewed authentication and security hardening. Use the immutable `ghcr.io/harrentheblack/brisa:1.2.0` image tag for production deployments rather than `latest`. Authentication remains optional for trusted LAN/backwards-compatible deployments, but public exposure requires authentication and network controls. `v1.0.2` remains the stable-drive-ID release.

Generate an Argon2id password hash. `getpass` reads the password from the terminal, and only the encoded hash is written to disk; the plaintext password is never placed in Compose, shell history, or the secret file.

```bash
umask 077
mkdir -p secrets
python3 -m venv .brisa-hash-venv
.brisa-hash-venv/bin/python -m pip install 'argon2-cffi==25.1.0'
.brisa-hash-venv/bin/python - <<'PY' > secrets/password_hash
from getpass import getpass
from argon2 import PasswordHasher

password = getpass("New Brisa password: ")
if not password:
    raise SystemExit("Password must not be empty")
if password != getpass("Confirm password: "):
    raise SystemExit("Passwords do not match")
print(PasswordHasher().hash(password))
PY
rm -rf .brisa-hash-venv
```

Create a `docker-compose.yml` file. This example is for final HTTPS deployment and keeps secure cookies enabled. Set `BRISA_SECURE_COOKIES=false` only for temporary direct HTTP testing on a trusted LAN.

```yaml
services:
  brisa:
    image: ghcr.io/harrentheblack/brisa:1.2.0
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

Replace `/some/data/path` and `/some/secrets/path` with host paths for Brisa data and secrets. Place the generated file at `/some/secrets/path/password_hash`; the directory is mounted read-only and the file contains an Argon2id hash, never the password. This example is for final HTTPS deployment: leave secure cookies enabled. For temporary direct HTTP testing on a trusted LAN only, set `BRISA_SECURE_COOKIES` to `"false"`. Keep `BRISA_TRUST_PROXY=false` until the actual NPM peer is measured. Start the reviewed release with:

```bash
docker compose up -d
```

The login page is available at `https://<host>` through the configured HTTPS proxy with username `admin` and the password entered during hash generation.

On first run, a default `config.json` is created at your `/data` volume path. No fans will be controlled until you configure curves and fan assignments through the UI.

---

## Configuration

Everything is configured through the web UI:

1. **Sensors & Fans** — see all detected hardware; set aliases, card colors, create virtual sensors, and manage dashboard groups
2. **Curves** — define temperature→speed curves
3. **Fan Config** — assign each fan a sensor (real or virtual) and a curve
4. **Settings** — adjust poll interval, history retention, safety floor

The config is stored as `/data/config.json` on your mounted volume.

### Drive Identity and Replacement

Physical drives use `drive-wwid-<normalized-wwid>` when a WWID is available, or `drive-serial-<normalized-serial>` otherwise. The detection backend (`drivetemp` or `smartctl`), `/dev/sdX` assignment, hwmon number, model, and display label are not part of a stable drive ID. Drives with neither WWID nor serial receive an explicitly unstable fallback ID and should not be assumed stable across reboots.

Legacy smartctl and drivetemp WWID/serial IDs are migrated automatically in `config.json`, without requiring the drive to be online. Matching IDs in `history.db` are also migrated transactionally at startup so history remains associated with the same physical drive.

Configured drives may remain visible as unavailable while offline. They do not prevent config edits, and virtual sensors continue using the sources that remain available. If a disk is physically replaced, its different WWID or serial creates a new sensor. Brisa never substitutes it automatically: explicitly remove the missing old drive and select the replacement in the virtual sensor or fan configuration.

---

## Virtual Sensors

Virtual sensors let you create a single computed temperature from a group of real sensors. Useful for controlling fans based on the average, maximum, or minimum temperature across a set of drives, CPU cores, or any other sensors.

- **Aggregation modes:** average, minimum, maximum
- **Resilient:** if some source sensors are unavailable, the virtual sensor computes from whatever is available; only skips if all sources are missing
- **Editable while offline:** configured missing sources remain visible and selected until explicitly removed or replaced
- **Usable everywhere:** virtual sensors appear in the fan config sensor selector and can be pinned to the dashboard just like real sensors
- **No nesting:** virtual sensors can only reference physical sensors, including hwmon and smartctl sources, not other virtual sensors

Virtual sensors are created and managed on the **Sensors & Fans** page.

---

## Dashboard Groups

The dashboard organizes fans and sensors into named groups displayed in order. Groups are configured on the **Sensors & Fans** page.

- **Sensor groups** and **fan groups** are separate (a group contains only sensors or only fans)
- Groups are displayed in the order you set, with ▲/▼ reordering
- Items not assigned to any group appear in an "Other" section at the bottom
- If no groups are defined, all configured fans and their associated sensors are shown (backward compatible)

---

## Card Colors

Each sensor or fan can be assigned an accent color from a curated palette: teal, blue, purple, pink, amber, orange, red, or slate. The color appears as a left border on the dashboard card. Colors are set on the **Sensors & Fans** page.

---

## Volume Layout

```
/data/
  config.json    ← curves, fan assignments, settings, aliases, virtual sensors, dashboard groups
  history.db     ← SQLite time-series database
```

The password hash is mounted separately under `/run/secrets` and is not stored in `/data`.

Recognized legacy drive IDs in `history.db` are re-keyed to canonical drive IDs during database initialization. Timestamps and temperatures are preserved; unrelated sensor and fan history is not modified.

---

## TrueNAS SCALE Notes

- Deploy via `docker compose` only — do not use the TrueNAS Apps UI
- `privileged: true` is required for USB access and sysfs PWM writes
- Mount `/data` to a path on your NVMe pool — SQLite does not perform well on spinning rust
- Many NAS-specific boards (e.g. Topton N22) lack a Super I/O chip with a Linux kernel driver — on these systems, hwmon-pwm fans will not be detected and only liquidctl (USB) fans are available

Use the authenticated `docker-compose.yml` from Quick Start. Keep `BRISA_SECURE_COOKIES=false` only when connecting directly over trusted LAN HTTP; use HTTPS and secure cookies for access through a reverse proxy.

---

## Podman

Podman runs rootless by default, which means `--privileged` does not grant real host root. Sysfs writes for hwmon-pwm fans will fail silently in rootless mode.

For hwmon-pwm fan control with Podman, run as real root with `/sys` mounted:

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
  ghcr.io/harrentheblack/brisa:1.2.0
```

This Podman command is for HTTPS deployment. For temporary direct HTTP testing on a trusted LAN only, set `BRISA_SECURE_COOKIES=false`. Put the generated `password_hash` under `/path/to/secrets`; it contains only the encoded hash and is mounted read-only.

If only using liquidctl (USB) fans, rootless Podman may work, but the invoking user still needs host USB-device permissions. Device ACLs, supplementary groups, and SELinux policy remain host-specific.

---

## Security

Brisa runs with `privileged: true`, which gives the container effectively root access to the host. This is required for USB device access and sysfs PWM writes — there is no way around it for hardware fan control from a container.

What this means in practice: the container can access all host devices, write to any sysfs path, and mount filesystems. Brisa only writes to `/sys/class/hwmon/hwmonN/pwmN` and `pwmN_enable` files, but the capability is broader than what the application uses.

### Authentication compatibility

Authentication is disabled by default so existing deployments continue to start. Brisa logs a warning in this mode. Disabled authentication is a compatibility mode, not a secure deployment choice: every UI page and API endpoint is available to any client that can reach port 9595. Set `BRISA_AUTH_ENABLED=true` and provide both the administrator username and hash file for normal deployments. Invalid enabled-auth configuration fails the management interface closed while the independently scheduled fan-control loop continues.

The authentication settings are:

| Variable | Default | Purpose |
|----------|---------|---------|
| `BRISA_AUTH_ENABLED` | `false` | Enables the single administrator login. |
| `BRISA_AUTH_USERNAME` | none | Administrator name; required when auth is enabled. |
| `BRISA_PASSWORD_HASH_FILE` | none | Absolute path to a one-record Argon2id hash file; required when auth is enabled. |
| `BRISA_SECURE_COOKIES` | `true` | Adds the browser cookie `Secure` attribute. |
| `BRISA_SESSION_TTL_SECONDS` | `28800` | Absolute session lifetime; allowed range is 300-86400 seconds. |
| `BRISA_TRUST_PROXY` | `false` | Allows validated `X-Forwarded-For` use for login rate-limit attribution. |
| `BRISA_TRUSTED_PROXY_CIDRS` | none | Comma-separated explicit proxy networks; required when proxy trust is enabled. |

Sessions are opaque and stored only in memory. The default lifetime is an absolute eight hours from login and is not extended by activity. A container restart invalidates every session; logout invalidates the current session immediately. Cookies are `HttpOnly` and `SameSite=Lax`. State-changing requests also require the per-session CSRF token in `X-CSRF-Token`; the frontend obtains it from `/api/auth/me` and adds it to requests.

Login failures are tracked by effective client address. Five credential failures within ten minutes block that client for 15 minutes. Argon2 verification is additionally limited to one concurrent verification and 30 starts per minute; those throttled responses use HTTP 429 and `Retry-After`. Login bodies are bounded to 8192 bytes and must complete within ten seconds; malformed framing and oversized bodies are rejected before JSON or Argon2 processing. These controls are why proxy client-address attribution must not accept spoofable forwarding headers.

There is no password reset, recovery email, or recovery token. To replace a forgotten or compromised password, generate a new Argon2id hash with the Quick Start procedure, atomically replace the configured hash file, and restart the container. The restart loads the replacement and invalidates existing sessions. Never put a plaintext password in an environment variable, Compose file, or mounted file.

### Cookies and TLS

Set `BRISA_SECURE_COOKIES=false` only for direct HTTP access on a trusted LAN. Browsers do not send secure cookies over HTTP. For any public, untrusted-network, or reverse-proxy deployment, terminate HTTPS at the proxy and leave `BRISA_SECURE_COOKIES=true`; this remains correct when the proxy-to-Brisa hop itself uses HTTP. Prefer a VPN or network access controls even with authentication because compromise of this privileged service has host-level impact.

### Trusted proxies

`BRISA_TRUST_PROXY=false` is the safe default and uses the immediate TCP peer for login rate limits. Uvicorn is launched with `--no-proxy-headers` so it cannot rewrite that peer from unvalidated forwarded headers before Brisa evaluates it.

If a reverse proxy is used, set `BRISA_TRUST_PROXY=true` and set `BRISA_TRUSTED_PROXY_CIDRS` only to the actual proxy peers that can connect to Brisa. Brisa walks `X-Forwarded-For` from right to left through trusted hops; forwarding headers from any other peer are ignored. Do not trust all private address space or a broad Docker range.

For Nginx Proxy Manager (NPM), measure the address on the network it actually shares with Brisa rather than copying an address from somebody else's deployment:

```bash
docker inspect --format '{{range $name, $network := .NetworkSettings.Networks}}{{println $name $network.IPAddress}}{{end}}' <npm-container>
```

Identify the shared network and either assign NPM a stable address and trust that exact IPv4 `/32` (or IPv6 `/128`), or trust the smallest explicit shared-network CIDR after reviewing what other containers can join it. For example, a measured peer of `172.30.0.10` is configured as `BRISA_TRUSTED_PROXY_CIDRS=172.30.0.10/32`; this address is only an illustration, not a universal NPM value.

### Post-NPM follow-up

- [ ] **TrueNAS Application Info version:** after authentication and Nginx Proxy Manager deployment are complete, determine where TrueNAS Custom Apps obtains the version shown in Application Info (custom-app metadata, Compose metadata, Docker labels, image metadata, an `app_version` field, or another TrueNAS-specific source). Make it match the actual Brisa release without introducing a second manually maintained version when it can be derived from the release version.
- [ ] **History storage architecture:** after authentication and Nginx Proxy Manager deployment are complete, review the actual `history.db` workload before changing databases. Evaluate the schema and indexes, write frequency and reading volume, expected growth, `history_days` retention and pruning performance, query patterns, concurrency and transaction behavior, integrity/corruption recovery, backup/restore, SQLite WAL mode, batching and one-transaction-per-reading overhead, indexes, aggregation/downsampling, long-term retention, and migration strategy. Compare keeping SQLite largely as-is, optimizing SQLite, SQLite WAL plus batching/index improvements, PostgreSQL, PostgreSQL plus TimescaleDB, and another time-series approach only if justified. Do not assume PostgreSQL is better: Brisa is a small self-contained appliance-like service, and another database container must be justified by measured workload and operational value.

### Release-time version checklist

For future releases, update the release version deliberately. Do not blindly replace historical `1.0.x` references.

- [ ] Update `brisa/app/version.py`.
- [ ] Verify FastAPI's application version and `/api/auth/me` use the canonical value.
- [ ] Verify the frontend-displayed version uses the API-reported value.
- [ ] Update current-release references in `README.md`, `ARCHITECTURE.md`, `CHANGELOG.md`, and `docker-compose.yml.example`, while preserving historical changelog entries.
- [ ] Update Docker/GHCR image examples, install instructions containing image tags, and version badges if present.
- [ ] Review Docker labels, image metadata, and any TrueNAS-specific metadata if those version-bearing fields exist.
- [ ] Update tests asserting the application version.
- [ ] Confirm immutable release tags are used in deployment documentation and examples.

### Protected endpoints

When authentication is enabled, protection includes the UI, REST API, `/docs`, `/openapi.json`, and Prometheus endpoints `/metrics` and `/api/metrics`. Monitoring clients must maintain a valid Brisa session; there is no separate metrics token or unauthenticated metrics exception.

### Process model

Brisa must run as one Uvicorn worker and one container replica. Sessions, rate limits, and controller state are process-local, and every worker would also start a fan-control loop. The image therefore specifies `--workers 1`, disables Uvicorn proxy-header rewriting, and caps concurrent connections/tasks. Do not override the worker count or horizontally scale this service.

**Recommendations:**
- Enable authentication and restrict port 9595 at the firewall or reverse proxy.
- Use `restart: unless-stopped` to ensure fans are re-managed after a crash.
- Review the container image contents if running on a sensitive system.

Authentication reduces network exposure but does not reduce the privilege of a successful compromise. Treat Brisa as a host-privileged management service.

---

## Known Limitations

**hwmon-pwm fans on container crash:** if the container is killed without a graceful shutdown (OOM, `kill -9`, power loss), hwmon-pwm fans stay at their last-written speed until the system is rebooted. On graceful shutdown (`docker stop`, `docker compose down`), Brisa restores the original firmware control mode automatically. liquidctl (USB) fans are not affected — USB controllers like the Quadro have their own firmware.

**No Super I/O driver:** boards without a supported kernel driver for their fan controller chip (common on embedded NAS boards) will show zero hwmon-pwm fans. This is a kernel limitation. Check `ls /sys/class/hwmon/` and inspect the `name` files to see if a Super I/O driver is loaded (e.g. `nct6775`, `nct6687`, `it87`).

---

## Safety Floor

The safety floor (`safety_floor_percent`, default 30%) is applied when a configured sensor cannot be read. It is a failure fallback, not a minimum speed policy — it does not apply to fans in manual override mode.

For virtual sensors, the safety floor triggers only when all source sensors are unavailable.

---

## API

Full OpenAPI docs are at `http://<host>:9595/docs`. They require a valid session when authentication is enabled.

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/auth/login` | Log in and create a session |
| GET | `/api/auth/me` | Current session, CSRF token, and version |
| POST | `/api/auth/logout` | Invalidate the current session |
| GET | `/api/state` | Grouped dashboard data: fan groups, sensor groups, ungrouped items |
| GET | `/api/history` | Time series (`?hours=24`) |
| GET | `/api/config` | Full config |
| POST | `/api/config` | Save new config (structural validation; offline physical references are allowed) |
| GET | `/api/devices` | Detected sensors, virtual sensors, and fans |
| POST | `/api/apply` | Trigger immediate control loop iteration |
| GET | `/api/metrics` | Prometheus metrics (includes virtual sensors) |
| GET | `/metrics` | Prometheus metrics without virtual-sensor enrichment |

---

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for a full description of the design, data model, controller loop, and project structure.

---

## Development

For local development, build the image from source:

```bash
git clone https://github.com/HarrenTheBlack/brisa.git
cd brisa
cp docker-compose.yml.example docker-compose.yml
```

Add a local build override:

```bash
cat > docker-compose.override.yml <<'EOF'
services:
  brisa:
    image: brisa:local
    build: brisa/
EOF
```

Then start Brisa:

```bash
docker compose up -d --build
```

Logs:

```bash
docker logs -f brisa
```
