import asyncio
import hashlib
import ipaddress
import logging
import math
import os
import re
import secrets
import stat
import time
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable, Mapping

from argon2 import PasswordHasher, extract_parameters
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import ARGON2_VERSION, Type
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

SESSION_COOKIE = "brisa_session"
CSRF_HEADER = b"x-csrf-token"
LOGIN_BODY_LIMIT = 8192
LOGIN_BODY_TIMEOUT_SECONDS = 10
MAX_SESSIONS = 16
MAX_RATE_LIMIT_CLIENTS = 4096
MAX_FORWARDED_HEADER_LENGTH = 1024
MAX_FORWARDED_HOPS = 16

PUBLIC_GET_PATHS = frozenset({
    "/login",
    "/style.css",
    "/login.js",
    "/logo.png",
    "/favicon.png",
    "/favicon.ico",
})
PUBLIC_LOGIN_PATH = "/api/auth/login"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class AuthState(str, Enum):
    DISABLED = "disabled"
    READY = "ready"
    INVALID = "invalid"


class AuthConfigurationError(ValueError):
    pass


class LoginRejected(Exception):
    pass


class LoginRateLimited(Exception):
    def __init__(self, retry_after: int):
        super().__init__("Login rate limited")
        self.retry_after = max(1, retry_after)


class VerificationUnavailable(LoginRateLimited):
    pass


@dataclass(frozen=True)
class AuthSettings:
    enabled: bool
    username: str | None
    password_hash_file: Path | None
    secure_cookies: bool
    session_ttl_seconds: int
    trust_proxy: bool
    trusted_proxy_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]


@dataclass(frozen=True)
class Session:
    username: str
    created_at_monotonic: float
    expires_at_monotonic: float
    csrf_token: str


@dataclass
class LoginRecord:
    failures: deque[float]
    blocked_until: float = 0.0


def _parse_bool(environ: Mapping[str, str], name: str, default: bool) -> bool:
    raw = environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise AuthConfigurationError(f"{name} must be either true or false")


def parse_auth_settings(environ: Mapping[str, str] | None = None) -> AuthSettings:
    environ = os.environ if environ is None else environ
    enabled = _parse_bool(environ, "BRISA_AUTH_ENABLED", False)
    secure_cookies = _parse_bool(environ, "BRISA_SECURE_COOKIES", True)
    trust_proxy = _parse_bool(environ, "BRISA_TRUST_PROXY", False)

    raw_ttl = environ.get("BRISA_SESSION_TTL_SECONDS", "28800").strip()
    try:
        ttl = int(raw_ttl)
    except ValueError as exc:
        raise AuthConfigurationError("BRISA_SESSION_TTL_SECONDS must be an integer") from exc
    if not 300 <= ttl <= 86400:
        raise AuthConfigurationError(
            "BRISA_SESSION_TTL_SECONDS must be between 300 and 86400"
        )

    username = environ.get("BRISA_AUTH_USERNAME")
    hash_path_raw = environ.get("BRISA_PASSWORD_HASH_FILE")
    hash_path = Path(hash_path_raw) if hash_path_raw else None

    if enabled:
        if username is None or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", username):
            raise AuthConfigurationError(
                "BRISA_AUTH_USERNAME must contain 1-64 characters from A-Z, a-z, 0-9, dot, underscore, or hyphen"
            )
        if hash_path is None:
            raise AuthConfigurationError("BRISA_PASSWORD_HASH_FILE is required")
        if not hash_path.is_absolute():
            raise AuthConfigurationError("BRISA_PASSWORD_HASH_FILE must be an absolute path")

    raw_cidrs = environ.get("BRISA_TRUSTED_PROXY_CIDRS", "").strip()
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    if raw_cidrs:
        for value in raw_cidrs.split(","):
            value = value.strip()
            if not value:
                raise AuthConfigurationError("BRISA_TRUSTED_PROXY_CIDRS contains an empty entry")
            try:
                networks.append(ipaddress.ip_network(value, strict=False))
            except ValueError as exc:
                raise AuthConfigurationError(
                    f"BRISA_TRUSTED_PROXY_CIDRS contains an invalid network: {value}"
                ) from exc
    if trust_proxy and not networks:
        raise AuthConfigurationError(
            "BRISA_TRUSTED_PROXY_CIDRS is required when BRISA_TRUST_PROXY=true"
        )

    return AuthSettings(
        enabled=enabled,
        username=username,
        password_hash_file=hash_path,
        secure_cookies=secure_cookies,
        session_ttl_seconds=ttl,
        trust_proxy=trust_proxy,
        trusted_proxy_networks=tuple(networks),
    )


def _load_password_hash(path: Path) -> str:
    try:
        file_stat = path.stat()
    except OSError as exc:
        raise AuthConfigurationError("Password hash file cannot be read") from exc
    if not stat.S_ISREG(file_stat.st_mode):
        raise AuthConfigurationError("Password hash file must be a regular file")
    if file_stat.st_size > 4096:
        raise AuthConfigurationError("Password hash file exceeds 4096 bytes")

    try:
        with path.open("rb") as hash_file:
            raw = hash_file.read(4097)
    except OSError as exc:
        raise AuthConfigurationError("Password hash file cannot be read") from exc
    if len(raw) > 4096:
        raise AuthConfigurationError("Password hash file exceeds 4096 bytes")
    try:
        encoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AuthConfigurationError("Password hash file must contain UTF-8") from exc
    if "\x00" in encoded:
        raise AuthConfigurationError("Password hash file contains a NUL character")
    if encoded.endswith("\r\n"):
        encoded = encoded[:-2]
    elif encoded.endswith("\n"):
        encoded = encoded[:-1]
    if not encoded or "\n" in encoded or "\r" in encoded:
        raise AuthConfigurationError("Password hash file must contain exactly one hash record")

    try:
        parameters = extract_parameters(encoded)
    except InvalidHashError as exc:
        raise AuthConfigurationError("Password hash file does not contain a valid Argon2 hash") from exc
    if parameters.type is not Type.ID:
        raise AuthConfigurationError("Password hash must use Argon2id")
    if parameters.version != ARGON2_VERSION:
        raise AuthConfigurationError(f"Password hash must use Argon2 version {ARGON2_VERSION}")
    if not 19456 <= parameters.memory_cost <= 262144:
        raise AuthConfigurationError("Password hash memory cost is outside the accepted range")
    if not 2 <= parameters.time_cost <= 10:
        raise AuthConfigurationError("Password hash time cost is outside the accepted range")
    if not 1 <= parameters.parallelism <= 8:
        raise AuthConfigurationError("Password hash parallelism is outside the accepted range")
    if not 16 <= parameters.salt_len <= 64:
        raise AuthConfigurationError("Password hash salt length is outside the accepted range")
    if not 16 <= parameters.hash_len <= 64:
        raise AuthConfigurationError("Password hash output length is outside the accepted range")
    return encoded


def _dummy_hash(encoded_hash: str) -> str:
    prefix, digest = encoded_hash.rsplit("$", 1)
    replacement = "A" if digest[0] != "A" else "B"
    return f"{prefix}${replacement}{digest[1:]}"


class PasswordVerifier:
    def __init__(
        self,
        encoded_hash: str,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.encoded_hash = encoded_hash
        self.dummy_hash = _dummy_hash(encoded_hash)
        self._hasher = PasswordHasher()
        self._clock = clock
        self._starts: deque[float] = deque()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="brisa-argon2")
        self._busy = False

    async def verify(self, password: str, use_dummy: bool) -> bool:
        now = self._clock()
        while self._starts and self._starts[0] <= now - 60:
            self._starts.popleft()
        if len(self._starts) >= 30:
            retry = math.ceil(60 - (now - self._starts[0]))
            raise VerificationUnavailable(retry)
        if self._busy:
            raise VerificationUnavailable(1)

        self._busy = True
        self._starts.append(now)
        loop = asyncio.get_running_loop()
        target_hash = self.dummy_hash if use_dummy else self.encoded_hash
        try:
            future = loop.run_in_executor(self._executor, self._verify_sync, target_hash, password)
        except Exception:
            self._busy = False
            raise

        def release(_future: asyncio.Future) -> None:
            self._busy = False

        future.add_done_callback(release)
        return await asyncio.shield(future)

    def _verify_sync(self, encoded_hash: str, password: str) -> bool:
        try:
            return self._hasher.verify(encoded_hash, password)
        except VerifyMismatchError:
            return False

    def needs_rehash(self) -> bool:
        return self._hasher.check_needs_rehash(self.encoded_hash)

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


class SessionStore:
    def __init__(
        self,
        ttl_seconds: int,
        clock: Callable[[], float] = time.monotonic,
        maximum: int = MAX_SESSIONS,
    ):
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._maximum = maximum
        self._sessions: OrderedDict[str, Session] = OrderedDict()

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    def _prune(self, now: float | None = None) -> None:
        now = self._clock() if now is None else now
        expired = [key for key, session in self._sessions.items()
                   if session.expires_at_monotonic <= now]
        for key in expired:
            self._sessions.pop(key, None)

    def create(self, username: str, existing_token: str | None = None) -> tuple[str, Session]:
        now = self._clock()
        self._prune(now)
        if existing_token:
            self.delete(existing_token)
        while len(self._sessions) >= self._maximum:
            self._sessions.popitem(last=False)
        token = secrets.token_urlsafe(32)
        session = Session(
            username=username,
            created_at_monotonic=now,
            expires_at_monotonic=now + self.ttl_seconds,
            csrf_token=secrets.token_urlsafe(32),
        )
        self._sessions[self._key(token)] = session
        return token, session

    def get(self, token: str) -> Session | None:
        self._prune()
        return self._sessions.get(self._key(token))

    def delete(self, token: str) -> None:
        self._sessions.pop(self._key(token), None)

    def clear(self) -> None:
        self._sessions.clear()


class LoginRateLimiter:
    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        maximum: int = MAX_RATE_LIMIT_CLIENTS,
    ):
        self._clock = clock
        self._maximum = maximum
        self._records: OrderedDict[str, LoginRecord] = OrderedDict()

    def _trim_failures(self, record: LoginRecord, now: float) -> None:
        while record.failures and record.failures[0] <= now - 600:
            record.failures.popleft()
        if record.blocked_until <= now:
            record.blocked_until = 0.0

    def _make_room(self, now: float) -> bool:
        for client, record in list(self._records.items()):
            self._trim_failures(record, now)
            if not record.failures and not record.blocked_until:
                self._records.pop(client, None)
        return len(self._records) < self._maximum

    def check(self, client: str) -> None:
        now = self._clock()
        record = self._records.get(client)
        if record is None:
            return
        self._trim_failures(record, now)
        if record.blocked_until > now:
            raise LoginRateLimited(math.ceil(record.blocked_until - now))
        if not record.failures:
            self._records.pop(client, None)

    def failure(self, client: str) -> None:
        now = self._clock()
        record = self._records.get(client)
        if record is None:
            if len(self._records) >= self._maximum and not self._make_room(now):
                raise LoginRateLimited(60)
            record = LoginRecord(failures=deque())
            self._records[client] = record
        self._trim_failures(record, now)
        record.failures.append(now)
        self._records.move_to_end(client)
        if len(record.failures) >= 5:
            record.blocked_until = now + 900
            raise LoginRateLimited(900)

    def success(self, client: str) -> None:
        self._records.pop(client, None)


def _normalize_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    address = ipaddress.ip_address(value.strip())
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _is_trusted(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    return any(address.version == network.version and address in network for network in networks)


def effective_client_ip(scope: Scope, settings: AuthSettings) -> str:
    client = scope.get("client")
    peer_text = client[0] if client else "unknown"
    try:
        peer = _normalize_ip(peer_text)
    except ValueError:
        return peer_text[:128]
    if not settings.trust_proxy or not _is_trusted(peer, settings.trusted_proxy_networks):
        return str(peer)

    forwarded_values = [value for name, value in scope.get("headers", [])
                        if name.lower() == b"x-forwarded-for"]
    if len(forwarded_values) != 1 or len(forwarded_values[0]) > MAX_FORWARDED_HEADER_LENGTH:
        return str(peer)
    try:
        raw_hops = forwarded_values[0].decode("ascii").split(",")
        if not 1 <= len(raw_hops) <= MAX_FORWARDED_HOPS:
            return str(peer)
        hops = [_normalize_ip(value) for value in raw_hops]
    except (UnicodeDecodeError, ValueError):
        return str(peer)

    candidate = peer
    for hop in reversed(hops):
        if not _is_trusted(candidate, settings.trusted_proxy_networks):
            break
        candidate = hop
    return str(candidate)


class AuthManager:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.state = AuthState.INVALID
        self.error = "Authentication has not been initialized"
        self.settings = AuthSettings(False, None, None, True, 28800, False, ())
        self.sessions = SessionStore(self.settings.session_ttl_seconds, clock=clock)
        self.rate_limiter = LoginRateLimiter(clock=clock)
        self.verifier: PasswordVerifier | None = None
        self._clock = clock

    def initialize(self, environ: Mapping[str, str] | None = None) -> None:
        if self.verifier:
            self.verifier.close()
            self.verifier = None
        self.state = AuthState.INVALID
        self.error = "Authentication configuration is invalid"
        try:
            settings = parse_auth_settings(environ)
            self.settings = settings
            self.sessions = SessionStore(settings.session_ttl_seconds, clock=self._clock)
            self.rate_limiter = LoginRateLimiter(clock=self._clock)
            if not settings.enabled:
                self.state = AuthState.DISABLED
                self.error = ""
                logger.warning(
                    "Authentication is disabled. Do not expose Brisa to untrusted networks."
                )
                return

            assert settings.password_hash_file is not None
            encoded_hash = _load_password_hash(settings.password_hash_file)
            verifier = PasswordVerifier(encoded_hash, clock=self._clock)
            self.verifier = verifier
            if verifier.needs_rehash():
                logger.warning("Password hash parameters should be regenerated with current defaults")
            if not settings.secure_cookies:
                logger.warning(
                    "Secure cookies are disabled. Use this only for direct HTTP/LAN testing."
                )
            self.state = AuthState.READY
            self.error = ""
            logger.info("Authentication enabled for administrator %s", settings.username)
        except Exception as exc:
            self.state = AuthState.INVALID
            self.error = str(exc) or exc.__class__.__name__
            logger.error("Authentication configuration invalid: %s", self.error)

    def mark_invalid(self, reason: str) -> None:
        self.state = AuthState.INVALID
        self.error = reason
        logger.error("Authentication unavailable: %s", reason)

    async def authenticate(self, username: str, password: str, client: str) -> bool:
        if self.state is not AuthState.READY or self.verifier is None:
            return False
        self.rate_limiter.check(client)
        expected_username = self.settings.username or ""
        username_matches = secrets.compare_digest(username, expected_username)
        try:
            password_matches = await self.verifier.verify(password, use_dummy=not username_matches)
        except (VerificationError, InvalidHashError) as exc:
            self.mark_invalid("Password verification failed internally")
            raise AuthConfigurationError("Password verification unavailable") from exc
        if username_matches and password_matches:
            self.rate_limiter.success(client)
            return True
        self.rate_limiter.failure(client)
        return False

    def record_malformed_login(self, client: str) -> None:
        if self.state is AuthState.READY:
            self.rate_limiter.check(client)
            self.rate_limiter.failure(client)

    def create_session(self, existing_token: str | None = None) -> tuple[str, Session]:
        assert self.settings.username is not None
        return self.sessions.create(self.settings.username, existing_token)

    def set_session_cookie(self, response: Response, token: str) -> None:
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=self.settings.session_ttl_seconds,
            expires=datetime.now(timezone.utc) + timedelta(seconds=self.settings.session_ttl_seconds),
            path="/",
            secure=self.settings.secure_cookies,
            httponly=True,
            samesite="lax",
        )

    def clear_session_cookie(self, response: Response) -> None:
        response.delete_cookie(
            SESSION_COOKIE,
            path="/",
            secure=self.settings.secure_cookies,
            httponly=True,
            samesite="lax",
        )

    def close(self) -> None:
        if self.verifier:
            self.verifier.close()


def _headers(scope: Scope, name: bytes) -> list[bytes]:
    return [value for key, value in scope.get("headers", []) if key.lower() == name]


def _session_cookie(scope: Scope) -> tuple[str | None, bool]:
    values: list[str] = []
    for raw_header in _headers(scope, b"cookie"):
        try:
            header = raw_header.decode("latin-1")
        except UnicodeDecodeError:
            return None, True
        for item in header.split(";"):
            name, separator, value = item.strip().partition("=")
            if separator and name == SESSION_COOKIE:
                values.append(value)
    if not values:
        return None, False
    if len(values) != 1:
        return None, True
    token = values[0]
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        return None, True
    return token, False


def _canonical_public_path(scope: Scope, path: str) -> bool:
    raw_path = scope.get("raw_path")
    if raw_path is None:
        return True
    try:
        return raw_path == path.encode("ascii")
    except UnicodeEncodeError:
        return False


def _is_public(scope: Scope) -> bool:
    method = scope.get("method", "")
    path = scope.get("path", "")
    if not _canonical_public_path(scope, path):
        return False
    if method in {"GET", "HEAD"} and path in PUBLIC_GET_PATHS:
        return True
    return method == "POST" and path == PUBLIC_LOGIN_PATH


def _is_non_html(path: str) -> bool:
    return (
        path == "/metrics"
        or path == "/openapi.json"
        or path.startswith("/api/")
    )


async def _limited_login_body(
    scope: Scope,
    receive: Receive,
) -> tuple[Receive | None, int | None]:
    lengths = _headers(scope, b"content-length")
    if len(lengths) > 1:
        return None, 400
    if lengths:
        try:
            length = int(lengths[0].decode("ascii"))
        except (UnicodeDecodeError, ValueError):
            return None, 400
        if length < 0:
            return None, 400
        if length > LOGIN_BODY_LIMIT:
            return None, 413

    body = bytearray()
    try:
        async with asyncio.timeout(LOGIN_BODY_TIMEOUT_SECONDS):
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return None, 400
                if message["type"] != "http.request":
                    continue
                chunk = message.get("body", b"")
                if len(body) + len(chunk) > LOGIN_BODY_LIMIT:
                    return None, 413
                body.extend(chunk)
                if not message.get("more_body", False):
                    break
    except TimeoutError:
        return None, 408

    delivered = False

    async def replay() -> Message:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}
        return {"type": "http.disconnect"}

    return replay, None


class AuthMiddleware:
    def __init__(self, app: ASGIApp, manager: AuthManager):
        self.app = app
        self.manager = manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            if self.manager.state is AuthState.DISABLED:
                await self.app(scope, receive, send)
            else:
                await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "")
        public = _is_public(scope)
        sensitive = path not in PUBLIC_GET_PATHS or path == "/login"
        wrapped_send = self._security_send(send, sensitive)

        if method in {"TRACE", "CONNECT"}:
            await self._send_response(
                PlainTextResponse("Method not allowed", status_code=405),
                scope, receive, wrapped_send,
            )
            return

        if self.manager.state is AuthState.INVALID:
            if public and method in {"GET", "HEAD"}:
                await self.app(scope, receive, wrapped_send)
            else:
                await self._unavailable(scope, receive, wrapped_send)
            return

        if path == PUBLIC_LOGIN_PATH and method == "POST" and public:
            if self.manager.state is AuthState.READY:
                fetch_sites = _headers(scope, b"sec-fetch-site")
                if len(fetch_sites) > 1 or (
                    fetch_sites and fetch_sites[0].lower() != b"same-origin"
                ):
                    response = JSONResponse(
                        {"detail": "Cross-origin login is not allowed"}, status_code=403
                    )
                    await self._send_response(response, scope, receive, wrapped_send)
                    return
                content_types = _headers(scope, b"content-type")
                if len(content_types) != 1 or (
                    content_types[0].split(b";", 1)[0].strip().lower()
                    != b"application/json"
                ):
                    response = JSONResponse(
                        {"detail": "Login requires application/json"}, status_code=415
                    )
                    await self._send_response(response, scope, receive, wrapped_send)
                    return
                client = effective_client_ip(scope, self.manager.settings)
                try:
                    self.manager.rate_limiter.check(client)
                except LoginRateLimited as exc:
                    response = JSONResponse(
                        {"detail": "Login temporarily unavailable"}, status_code=429
                    )
                    response.headers["Retry-After"] = str(exc.retry_after)
                    await self._send_response(response, scope, receive, wrapped_send)
                    return
            limited_receive, error_status = await _limited_login_body(scope, receive)
            if error_status is not None:
                if self.manager.state is AuthState.READY:
                    client = effective_client_ip(scope, self.manager.settings)
                    try:
                        self.manager.record_malformed_login(client)
                    except LoginRateLimited:
                        pass
                response = JSONResponse(
                    {
                        "detail": (
                            "Login request too large" if error_status == 413
                            else "Login request timed out" if error_status == 408
                            else "Invalid request"
                        )
                    },
                    status_code=error_status,
                )
                await self._send_response(response, scope, receive, wrapped_send)
                return
            assert limited_receive is not None
            if self.manager.state is AuthState.READY:
                self._attach_session(scope)
            await self.app(scope, limited_receive, wrapped_send)
            return

        if self.manager.state is AuthState.DISABLED:
            await self.app(scope, receive, wrapped_send)
            return

        if public:
            await self.app(scope, receive, wrapped_send)
            return

        session, token, invalid_cookie = self._attach_session(scope)
        if session is None:
            await self._unauthorized(scope, receive, wrapped_send, invalid_cookie)
            return

        if method not in SAFE_METHODS:
            csrf_values = _headers(scope, CSRF_HEADER)
            if len(csrf_values) != 1:
                await self._forbidden(scope, receive, wrapped_send)
                return
            try:
                supplied_csrf = csrf_values[0].decode("ascii")
            except UnicodeDecodeError:
                await self._forbidden(scope, receive, wrapped_send)
                return
            if not secrets.compare_digest(supplied_csrf, session.csrf_token):
                await self._forbidden(scope, receive, wrapped_send)
                return

        scope.setdefault("state", {})["auth_session"] = session
        scope["state"]["auth_session_token"] = token
        await self.app(scope, receive, wrapped_send)

    def _attach_session(self, scope: Scope) -> tuple[Session | None, str | None, bool]:
        token, invalid_cookie = _session_cookie(scope)
        session = self.manager.sessions.get(token) if token else None
        if token and session is None:
            invalid_cookie = True
        state = scope.setdefault("state", {})
        state["auth_session"] = session
        state["auth_session_token"] = token if session else None
        state["invalid_session_cookie"] = invalid_cookie
        return session, token if session else None, invalid_cookie

    def _security_send(self, send: Send, sensitive: bool) -> Send:
        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                names = {name.lower() for name, _ in headers}
                security_headers = (
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-frame-options", b"DENY"),
                    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
                    (b"content-security-policy", b"frame-ancestors 'none'; object-src 'none'; base-uri 'self'"),
                )
                for name, value in security_headers:
                    if name not in names:
                        headers.append((name, value))
                if sensitive and b"cache-control" not in names:
                    headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)
        return wrapped

    async def _send_response(
        self,
        response: Response,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        await response(scope, receive, send)

    async def _unauthorized(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        clear_cookie: bool,
    ) -> None:
        path = scope.get("path", "")
        method = scope.get("method", "")
        if method in {"GET", "HEAD"} and not _is_non_html(path):
            response: Response = RedirectResponse("/login", status_code=303)
        else:
            response = JSONResponse({"detail": "Authentication required"}, status_code=401)
        if clear_cookie:
            self.manager.clear_session_cookie(response)
        await response(scope, receive, send)

    async def _forbidden(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse({"detail": "CSRF validation failed"}, status_code=403)
        await response(scope, receive, send)

    async def _unavailable(self, scope: Scope, receive: Receive, send: Send) -> None:
        if _is_non_html(scope.get("path", "")):
            response: Response = JSONResponse(
                {"detail": "Management interface unavailable."}, status_code=503
            )
        else:
            response = PlainTextResponse("Management interface unavailable.", status_code=503)
        await response(scope, receive, send)
