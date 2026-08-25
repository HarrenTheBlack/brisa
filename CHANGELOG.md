# Changelog

## Unreleased

### Added

- Added optional administrator authentication backed by an Argon2id password hash file. Authentication remains disabled by default for compatibility and emits a warning when disabled.
- Added opaque, in-memory sessions with an absolute configurable lifetime, CSRF protection for authenticated state-changing requests except login, login rate limiting, and explicit trusted-proxy CIDRs for client-address attribution.
- Updated FastAPI and pinned patched Starlette static-file handling for the public login surface.
- Protected the web UI, REST API, OpenAPI documentation, and Prometheus metrics when authentication is enabled.

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
