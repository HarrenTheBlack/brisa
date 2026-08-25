# Changelog

## v1.1.0 - 2026-08-25

### Added

- Added optional single-administrator dashboard and API authentication backed by Argon2id password hashes from read-only secret files. Authentication remains disabled by default for compatibility and emits a warning when disabled.
- Added opaque, in-memory sessions with an absolute configurable lifetime, CSRF protection for authenticated state-changing requests except login, login rate limiting, and explicit trusted-proxy CIDRs for client-address attribution.
- Added login/logout UI, centralized backend version reporting, frontend version rendering from `/api/auth/me`, and `X-Content-Type-Options: nosniff` security responses.
- Protected the dashboard, REST API, root static files, OpenAPI documentation, and Prometheus metrics when authentication is enabled.

### Security

- Fail closed with HTTP 503 for invalid enabled-auth configuration while the fan-control loop continues independently.
- Remediated frontend XSS sinks and added regression coverage for hostile dashboard values.
- Bounded login bodies and Argon2 admission, upgraded security-sensitive dependencies, and disabled Uvicorn generic proxy-header rewriting.
- Pinned Chart.js with SRI and expanded CI coverage for Python, frontend JavaScript, inline scripts, and authentication security boundaries.

### Changed

- Updated the example deployment to enable authentication without storing a plaintext password and to mount the password hash from a read-only secrets directory.
- Run Uvicorn as exactly one worker with its generic proxy-header handling disabled so Brisa can validate forwarded client addresses itself and retain a single controller loop and session store.

## v1.0.2 - 2026-08-21

### Changed

- Published fork-owned GHCR images and retained stable physical-drive identities across Linux device-name changes.

### Fixed

- Migrated legacy drive sensor references to canonical WWID/serial IDs without losing virtual-sensor memberships or history associations.

## v1.0.1 - 2026-05-10

### Fixed

- Removed unresolved merge conflict markers from the FastAPI app startup file that caused the published Docker image to fail with a `SyntaxError` on boot.
- Removed unresolved merge conflict markers from the frontend sidebar version display.

## v1.0.0 - 2026-03-21

### Added

- Initial stable release of Brisa.
- Docker-based fan control service for TrueNAS SCALE and Linux hosts.
- Support for liquidctl USB fan controllers and hwmon PWM fan headers.
- Web UI, REST API, Prometheus metrics, virtual sensors, dashboard groups, card colors, and SQLite history.
