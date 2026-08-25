import asyncio
import importlib
import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from fastapi import FastAPI, WebSocket
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api.auth_routes import router as auth_router
from app import auth as auth_module
from app.auth import (
    LOGIN_BODY_LIMIT,
    PUBLIC_GET_PATHS,
    AuthManager,
    AuthMiddleware,
    AuthState,
)
from app.models import AppConfig
from app.version import __version__


PASSWORD = "correct horse battery staple"
SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "x-frame-options": "DENY",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
    "content-security-policy": "frame-ancestors 'none'; object-src 'none'; base-uri 'self'",
}


@pytest.fixture
def password_hash_file(tmp_path):
    path = tmp_path / "password.hash"
    encoded = PasswordHasher(
        memory_cost=19456,
        time_cost=2,
        parallelism=1,
        salt_len=16,
        hash_len=16,
    ).hash(PASSWORD)
    path.write_text(encoded, encoding="utf-8")
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


@dataclass
class AuthHarness:
    app: AuthMiddleware
    api: FastAPI
    manager: AuthManager
    runtime: dict


@pytest.fixture
def app_factory(password_hash_file, tmp_path):
    managers = []
    sequence = 0

    def create(state=AuthState.READY, secure=True):
        nonlocal sequence
        sequence += 1
        manager = AuthManager()
        managers.append(manager)
        if state is AuthState.READY:
            manager.initialize({
                "BRISA_AUTH_ENABLED": "true",
                "BRISA_AUTH_USERNAME": "admin",
                "BRISA_PASSWORD_HASH_FILE": str(password_hash_file),
                "BRISA_SECURE_COOKIES": str(secure).lower(),
                "BRISA_SESSION_TTL_SECONDS": "300",
            })
        elif state is AuthState.DISABLED:
            manager.initialize({
                "BRISA_AUTH_ENABLED": "false",
                "BRISA_SECURE_COOKIES": str(secure).lower(),
                "BRISA_SESSION_TTL_SECONDS": "300",
            })
        else:
            manager.initialize({
                "BRISA_AUTH_ENABLED": "true",
                "BRISA_AUTH_USERNAME": "admin",
                "BRISA_PASSWORD_HASH_FILE": str(tmp_path / "missing.hash"),
                "BRISA_SECURE_COOKIES": str(secure).lower(),
                "BRISA_SESSION_TTL_SECONDS": "300",
            })
        assert manager.state is state

        api = FastAPI(title="Auth integration harness")
        api.state.auth = manager
        api.include_router(auth_router, prefix="/api/auth")
        runtime = {
            "config": {"profile": "unchanged"},
            "controller": {"cycles": 7},
            "state_calls": 0,
            "apply_calls": 0,
        }

        @api.get("/api/state")
        async def state_route():
            runtime["state_calls"] += 1
            return {"status": "stable"}

        @api.api_route(
            "/api/apply",
            methods=["POST", "PUT", "PATCH", "DELETE"],
            include_in_schema=False,
        )
        async def apply_route():
            runtime["apply_calls"] += 1
            return {"status": "ok"}

        @api.get("/metrics")
        async def root_metrics():
            return PlainTextResponse("root_metric 1\n")

        @api.get("/api/metrics")
        async def api_metrics():
            return PlainTextResponse("api_metric 1\n")

        @api.websocket("/ws")
        async def websocket_route(websocket: WebSocket):
            await websocket.accept()
            await websocket.send_text("accepted")
            await websocket.close()

        static_dir = tmp_path / f"static-{sequence}"
        static_dir.mkdir()
        (static_dir / "index.html").write_text("private static", encoding="ascii")
        api.mount("/private-static", StaticFiles(directory=static_dir), name="private-static")

        @api.api_route(
            "/{path:path}",
            methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            include_in_schema=False,
        )
        async def catch_all(path: str):
            return JSONResponse({"path": f"/{path}"})

        return AuthHarness(AuthMiddleware(api, manager), api, manager, runtime)

    try:
        yield create
    finally:
        for manager in managers:
            manager.close()


def login(client, password=PASSWORD):
    return client.post(
        "/api/auth/login",
        json={"username": "admin", "password": password},
    )


def csrf_token(client):
    response = client.get("/api/auth/me")
    assert response.status_code == 200
    return response.json()["csrf_token"]


async def _asgi_exchange_async(app, *, path, method="GET", raw_path=None, headers=None,
                                chunks=None, query_string=b""):
    messages = []
    incoming = list(chunks or [(b"", False)])
    receive_calls = 0

    async def receive():
        nonlocal receive_calls
        receive_calls += 1
        if incoming:
            body, more_body = incoming.pop(0)
            return {"type": "http.request", "body": body, "more_body": more_body}
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "scheme": "https",
        "method": method,
        "path": path,
        "raw_path": raw_path if raw_path is not None else path.encode("ascii"),
        "query_string": query_string,
        "root_path": "",
        "headers": list(headers or []),
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 443),
    }
    await app(scope, receive, send)
    start = next(message for message in messages if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    response_headers = {}
    for name, value in start.get("headers", []):
        response_headers.setdefault(name.decode("latin-1").lower(), []).append(
            value.decode("latin-1")
        )
    return start["status"], response_headers, body, receive_calls


def asgi_exchange(app, *, path, method="GET", raw_path=None, headers=None, chunks=None,
                  query_string=b""):
    return asyncio.run(
        _asgi_exchange_async(
            app, path=path, method=method, raw_path=raw_path, headers=headers,
            chunks=chunks, query_string=query_string,
        )
    )


@pytest.mark.parametrize("path", sorted(PUBLIC_GET_PATHS))
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_public_allowlist_contains_only_exact_login_assets(app_factory, path, method):
    harness = app_factory()
    status, _headers, _body, _calls = asgi_exchange(
        harness.app, path=path, method=method
    )
    assert status == 200


def test_public_allowlist_matches_independently_reviewed_literal_set():
    assert PUBLIC_GET_PATHS == frozenset({
        "/login",
        "/style.css",
        "/login.js",
        "/logo.png",
        "/favicon.png",
        "/favicon.ico",
    })


@pytest.mark.parametrize(
    ("path", "raw_path"),
    [
        ("/login/", b"/login/"),
        ("/login.css", b"/login.css"),
        ("//login", b"//login"),
        ("/login", b"/%6cogin"),
        ("/login", b"/%6Cogin"),
        ("/login", b"/public/../login"),
        ("/Login", b"/Login"),
        ("/login/", b"/login%2f"),
        ("/login", b"/./login"),
        ("/foo/../login", b"/foo/../login"),
        ("/login\x00", b"/login%00"),
        ("/login.js/anything", b"/login.js/anything"),
    ],
)
def test_similar_noncanonical_public_paths_are_private(app_factory, path, raw_path):
    harness = app_factory()
    status, headers, _body, _calls = asgi_exchange(
        harness.app, path=path, raw_path=raw_path
    )
    assert status == 303
    assert headers["location"] == ["/login"]


@pytest.mark.parametrize(
    ("path", "raw_path"),
    [
        ("/api/auth/login/", b"/api/auth/login/"),
        ("/api//auth/login", b"/api//auth/login"),
        ("/api/auth/login", b"/api/auth/%6cogin"),
        ("//api/auth/login", b"//api/auth/login"),
        ("/api/auth/login\x00", b"/api/auth/login%00"),
    ],
)
def test_similar_noncanonical_login_endpoint_paths_never_reach_login_logic(
    app_factory, path, raw_path
):
    """Even with fully correct credentials, encoded/duplicated/traversal
    variants of the login path must be rejected by the outer allowlist
    check before the request body is read or credentials are verified."""
    harness = app_factory()
    encoded = json.dumps({"username": "admin", "password": PASSWORD}).encode()
    status, _headers, body, receive_calls = asgi_exchange(
        harness.app,
        path=path,
        raw_path=raw_path,
        method="POST",
        headers=[
            (b"content-type", b"application/json"),
            (b"content-length", str(len(encoded)).encode()),
            (b"sec-fetch-site", b"same-origin"),
        ],
        chunks=[(encoded, False)],
    )
    assert status == 401
    assert receive_calls == 0
    assert json.loads(body) == {"detail": "Authentication required"}


def test_exact_login_endpoint_with_correct_credentials_and_query_string_succeeds(
    app_factory,
):
    """Positive control proving the reachability check above is meaningful:
    the literal path with a query string still reaches real login logic."""
    harness = app_factory()
    encoded = json.dumps({"username": "admin", "password": PASSWORD}).encode()
    status, headers, body, receive_calls = asgi_exchange(
        harness.app,
        path="/api/auth/login",
        raw_path=b"/api/auth/login",
        query_string=b"x=1",
        method="POST",
        headers=[
            (b"content-type", b"application/json"),
            (b"content-length", str(len(encoded)).encode()),
            (b"sec-fetch-site", b"same-origin"),
        ],
        chunks=[(encoded, False)],
    )
    assert status == 200
    assert receive_calls == 1
    assert json.loads(body) == {"authenticated": True, "username": "admin"}
    assert headers["set-cookie"]


def test_query_does_not_change_exact_public_path(app_factory):
    harness = app_factory()
    status, _headers, _body, _calls = asgi_exchange(
        harness.app,
        path="/login",
        raw_path=b"/login",
        query_string=b"next=%2Fapi%2Fstate",
    )
    assert status == 200


@pytest.mark.parametrize("path", sorted(PUBLIC_GET_PATHS))
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def test_public_assets_are_not_public_for_wrong_methods(app_factory, path, method):
    harness = app_factory()
    status, _headers, body, _calls = asgi_exchange(
        harness.app, path=path, method=method
    )
    assert status == 401
    assert json.loads(body) == {"detail": "Authentication required"}


def test_ready_unauthenticated_api_browser_docs_and_metrics(app_factory):
    harness = app_factory()
    with TestClient(
        harness.app, base_url="https://testserver", follow_redirects=False
    ) as client:
        for path in ["/api/state", "/api/auth/me", "/metrics", "/api/metrics"]:
            response = client.get(path)
            assert response.status_code == 401, path
            assert response.json() == {"detail": "Authentication required"}

        for path in ["/", "/settings.html", "/docs", "/docs/oauth2-redirect", "/redoc"]:
            response = client.get(path)
            assert response.status_code == 303, path
            assert response.headers["location"] == "/login"

        response = client.get("/openapi.json")
        assert response.status_code == 401
        assert response.json() == {"detail": "Authentication required"}


@pytest.mark.parametrize(
    "host_header",
    [
        b"attacker.example",
        b"attacker.example:9999",
        b"attacker.example/evil-path",
        b"",
    ],
)
def test_hostile_host_header_cannot_control_redirect_or_bypass_auth(
    app_factory, host_header
):
    """The login redirect must remain a hardcoded relative path regardless
    of the Host header, and API authorization must be unaffected by it."""
    harness = app_factory()

    status, headers, _body, _calls = asgi_exchange(
        harness.app,
        path="/",
        raw_path=b"/",
        headers=[(b"host", host_header)],
    )
    assert status == 303
    assert headers["location"] == ["/login"]

    status, _headers, body, _calls = asgi_exchange(
        harness.app,
        path="/api/state",
        raw_path=b"/api/state",
        headers=[(b"host", host_header)],
    )
    assert status == 401
    assert json.loads(body) == {"detail": "Authentication required"}


def test_missing_host_header_does_not_bypass_auth(app_factory):
    harness = app_factory()
    status, _headers, body, _calls = asgi_exchange(
        harness.app,
        path="/api/state",
        raw_path=b"/api/state",
        headers=[],
    )
    assert status == 401
    assert json.loads(body) == {"detail": "Authentication required"}


def test_authenticated_static_docs_openapi_and_both_metrics(app_factory):
    harness = app_factory()
    with TestClient(harness.app, base_url="https://testserver") as client:
        assert login(client).status_code == 200
        expected = {
            "/private-static/index.html": "private static",
            "/docs": None,
            "/docs/oauth2-redirect": None,
            "/redoc": None,
            "/openapi.json": None,
            "/metrics": "root_metric 1\n",
            "/api/metrics": "api_metric 1\n",
        }
        for path, body in expected.items():
            response = client.get(path)
            assert response.status_code == 200, path
            if body is not None:
                assert response.text == body


def test_websocket_is_denied_by_default(app_factory):
    harness = app_factory()
    with TestClient(harness.app, base_url="https://testserver") as client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/ws"):
                pass
    assert exc_info.value.code == 1008


@pytest.mark.parametrize("secure", [True, False])
def test_session_cookie_has_complete_attributes_and_no_domain(app_factory, secure):
    harness = app_factory(secure=secure)
    before = datetime.now(timezone.utc)
    with TestClient(harness.app, base_url="https://testserver") as client:
        response = login(client)
    after = datetime.now(timezone.utc)

    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert re.search(r"^brisa_session=[A-Za-z0-9_-]{43};", cookie)
    assert "HttpOnly" in cookie
    assert "Max-Age=300" in cookie
    assert "Path=/" in cookie
    assert "SameSite=lax" in cookie
    assert ("; Secure" in cookie) is secure
    assert "domain=" not in cookie.lower()
    expires = re.search(r"expires=([^;]+)", cookie, re.IGNORECASE)
    assert expires is not None
    expiry = parsedate_to_datetime(expires.group(1))
    assert before.timestamp() + 298 <= expiry.timestamp() <= after.timestamp() + 302


@pytest.mark.parametrize("secure", [True, False])
@pytest.mark.parametrize(
    "cookie_header",
    [
        "brisa_session=short; brisa_session=also-short",
        f"brisa_session={'x' * 43}",
    ],
)
def test_duplicate_or_unknown_session_cookie_is_cleared(
    app_factory, secure, cookie_header
):
    harness = app_factory(secure=secure)
    with TestClient(
        harness.app, base_url="https://testserver", follow_redirects=False
    ) as client:
        response = client.get("/api/state", headers={"Cookie": cookie_header})

    assert response.status_code == 401
    cookie = response.headers["set-cookie"]
    assert cookie.startswith('brisa_session="";')
    assert "HttpOnly" in cookie
    assert "Max-Age=0" in cookie
    assert "Path=/" in cookie
    assert "SameSite=lax" in cookie
    assert ("; Secure" in cookie) is secure
    assert "domain=" not in cookie.lower()
    expiry = parsedate_to_datetime(re.search(r"expires=([^;]+)", cookie, re.I).group(1))
    assert expiry <= datetime.now(timezone.utc)


@pytest.mark.parametrize("order", ["real_first", "real_second"])
def test_duplicate_cookie_never_selects_the_genuinely_valid_session(
    app_factory, order
):
    """Even when one of two duplicate `brisa_session` cookies is a real,
    currently-valid session token, the ambiguous pair must fail closed
    rather than an attacker being able to smuggle a decoy alongside (or
    instead of) the victim's real cookie and have the server arbitrarily
    pick either one."""
    harness = app_factory()
    with TestClient(
        harness.app, base_url="https://testserver", follow_redirects=False
    ) as client:
        logged_in = login(client)
        assert logged_in.status_code == 200
        real_cookie = client.cookies.get("brisa_session")
        assert real_cookie

        decoy = "y" * 43
        if order == "real_first":
            cookie_header = f"brisa_session={real_cookie}; brisa_session={decoy}"
        else:
            cookie_header = f"brisa_session={decoy}; brisa_session={real_cookie}"

    # Use a raw ASGI exchange (rather than the TestClient's cookie jar) so
    # exactly the crafted duplicate-cookie header is sent, precisely as an
    # attacker-controlled request would present it.
    status, _headers, body, _calls = asgi_exchange(
        harness.app,
        path="/api/state",
        raw_path=b"/api/state",
        headers=[(b"cookie", cookie_header.encode("ascii"))],
    )

    assert status == 401
    assert json.loads(body) == {"detail": "Authentication required"}
    # The real session must still exist server-side; this proves the
    # rejection was a client-presentation decision, not silent revocation.
    assert harness.manager.sessions.get(real_cookie) is not None


def test_unrelated_cookie_is_not_cleared(app_factory):
    harness = app_factory()
    with TestClient(harness.app, base_url="https://testserver") as client:
        response = client.get("/api/state", headers={"Cookie": "theme=dark"})
    assert response.status_code == 401
    assert "set-cookie" not in response.headers


def test_login_me_logout_flow(app_factory):
    harness = app_factory()
    with TestClient(harness.app, base_url="https://testserver") as client:
        rejected = login(client, "wrong password")
        assert rejected.status_code == 401
        assert rejected.json() == {"detail": "Invalid username or password"}
        assert rejected.headers["cache-control"] == "no-store"
        assert rejected.headers["pragma"] == "no-cache"

        logged_in = login(client)
        assert logged_in.status_code == 200
        assert logged_in.json() == {"authenticated": True, "username": "admin"}

        me = client.get("/api/auth/me")
        assert me.status_code == 200
        payload = me.json()
        assert payload["auth_enabled"] is True
        assert payload["authenticated"] is True
        assert payload["username"] == "admin"
        assert re.fullmatch(r"[A-Za-z0-9_-]{43}", payload["csrf_token"])
        assert payload["version"] == __version__ == "1.1.0"

        logged_out = client.post(
            "/api/auth/logout", headers={"X-CSRF-Token": payload["csrf_token"]}
        )
        assert logged_out.status_code == 204
        logout_cookie = logged_out.headers["set-cookie"]
        assert logout_cookie.startswith('brisa_session="";')
        assert "Max-Age=0" in logout_cookie
        assert "HttpOnly" in logout_cookie
        assert "Path=/" in logout_cookie
        assert "SameSite=lax" in logout_cookie
        assert "; Secure" in logout_cookie  # harness default is secure=True
        assert "domain=" not in logout_cookie.lower()
        assert client.get("/api/auth/me").status_code == 401


def test_wrong_username_and_wrong_password_are_externally_indistinguishable(app_factory):
    harness = app_factory()
    with TestClient(harness.app, base_url="https://testserver") as client:
        wrong_username = client.post(
            "/api/auth/login",
            json={"username": "not-admin", "password": PASSWORD},
        )
        wrong_password = login(client, "not the password")

    assert wrong_username.status_code == wrong_password.status_code == 401
    assert wrong_username.json() == wrong_password.json() == {
        "detail": "Invalid username or password"
    }


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_all_unsafe_methods_require_csrf(app_factory, method):
    harness = app_factory()
    with TestClient(harness.app, base_url="https://testserver") as client:
        assert login(client).status_code == 200
        token = csrf_token(client)
        for headers in [{}, {"X-CSRF-Token": "wrong"}]:
            response = client.request(method, "/api/apply", headers=headers)
            assert response.status_code == 403
            assert response.json() == {"detail": "CSRF validation failed"}
        assert client.request(
            method, "/api/apply", headers={"X-CSRF-Token": token}
        ).status_code == 200


def test_login_is_exempt_from_csrf(app_factory):
    harness = app_factory()
    with TestClient(harness.app, base_url="https://testserver") as client:
        response = login(client)
    assert response.status_code == 200


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_csrf_is_enforced_on_a_completely_novel_route_never_registered_by_name(
    app_factory, method
):
    """CSRF enforcement must be a blanket, method-based middleware policy,
    not something wired up per known route (such as /api/apply). This uses
    a path that no route explicitly declares, reaching only the harness's
    generic catch-all, to prove the protection is not accidentally scoped
    to a fixed list of known endpoints."""
    harness = app_factory()
    novel_path = "/api/this-route-was-never-explicitly-registered-anywhere"
    with TestClient(harness.app, base_url="https://testserver") as client:
        assert login(client).status_code == 200
        token = csrf_token(client)

        for headers in [{}, {"X-CSRF-Token": "wrong"}]:
            response = client.request(method, novel_path, headers=headers)
            assert response.status_code == 403
            assert response.json() == {"detail": "CSRF validation failed"}

        response = client.request(method, novel_path, headers={"X-CSRF-Token": token})
        assert response.status_code == 200
        assert response.json() == {"path": novel_path}


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_safe_methods_never_require_csrf_even_on_a_novel_route(app_factory, method):
    harness = app_factory()
    novel_path = "/api/this-route-was-never-explicitly-registered-anywhere"
    with TestClient(harness.app, base_url="https://testserver") as client:
        assert login(client).status_code == 200
        response = client.request(method, novel_path)
        assert response.status_code == 200


@pytest.mark.parametrize("state", [AuthState.DISABLED, AuthState.READY, AuthState.INVALID])
@pytest.mark.parametrize("method", ["TRACE", "CONNECT"])
def test_trace_and_connect_always_rejected_regardless_of_auth_state(
    app_factory, state, method
):
    harness = app_factory(state)
    status, _headers, body, _calls = asgi_exchange(
        harness.app, path="/login", raw_path=b"/login", method=method
    )
    assert status == 405
    assert body == b"Method not allowed"


def test_csrf_token_from_a_different_active_session_is_rejected(app_factory):
    """A CSRF token that is genuinely valid, but for a *different* active
    session than the one identified by the presented cookie, must not be
    accepted. CSRF validation must be scoped to the session tied to the
    request's own cookie, not merely 'is this any currently-valid token'."""
    harness = app_factory()

    with TestClient(harness.app, base_url="https://testserver") as client_a:
        assert login(client_a).status_code == 200
        cookie_a = client_a.cookies.get("brisa_session")
        assert cookie_a

    with TestClient(harness.app, base_url="https://testserver") as client_b:
        assert login(client_b).status_code == 200
        token_b = csrf_token(client_b)

    # Both sessions coexist independently (login does not revoke unrelated
    # sessions belonging to other cookies).
    assert harness.manager.sessions.get(cookie_a) is not None

    status, _headers, body, _calls = asgi_exchange(
        harness.app,
        path="/api/apply",
        raw_path=b"/api/apply",
        method="POST",
        headers=[
            (b"cookie", f"brisa_session={cookie_a}".encode("ascii")),
            (b"x-csrf-token", token_b.encode("ascii")),
        ],
    )
    assert status == 403
    assert json.loads(body) == {"detail": "CSRF validation failed"}

    # The legitimate CSRF token for session A must still work normally.
    session_a = harness.manager.sessions.get(cookie_a)
    status2, _headers2, body2, _calls2 = asgi_exchange(
        harness.app,
        path="/api/apply",
        raw_path=b"/api/apply",
        method="POST",
        headers=[
            (b"cookie", f"brisa_session={cookie_a}".encode("ascii")),
            (b"x-csrf-token", session_a.csrf_token.encode("ascii")),
        ],
    )
    assert status2 == 200


def _login_scope_kwargs(password):
    encoded = json.dumps({"username": "admin", "password": password}).encode()
    return dict(
        path="/api/auth/login",
        raw_path=b"/api/auth/login",
        method="POST",
        headers=[
            (b"content-type", b"application/json"),
            (b"content-length", str(len(encoded)).encode()),
            (b"sec-fetch-site", b"same-origin"),
        ],
        chunks=[(encoded, False)],
    )


def test_forty_concurrent_logins_admit_at_most_one_argon2_verification(app_factory):
    """Fire 40 genuinely concurrent login attempts (real Argon2, real
    ThreadPoolExecutor, scheduled on one asyncio event loop) and prove:
    - never more than one physical verification runs at a time;
    - the executor never accumulates a backlog of queued work;
    - excess concurrent attempts fail fast with 429, not by queueing;
    - exactly one admitted attempt reaches the real login outcome."""
    harness = app_factory()
    verifier = harness.manager.verifier
    real_verify_sync = verifier._verify_sync

    concurrent_count = 0
    max_concurrent_seen = 0
    max_queue_seen = 0
    lock = threading.Lock()

    def instrumented_verify_sync(encoded_hash, password):
        nonlocal concurrent_count, max_concurrent_seen, max_queue_seen
        with lock:
            concurrent_count += 1
            max_concurrent_seen = max(max_concurrent_seen, concurrent_count)
            max_queue_seen = max(
                max_queue_seen, verifier._executor._work_queue.qsize()
            )
        try:
            return real_verify_sync(encoded_hash, password)
        finally:
            with lock:
                concurrent_count -= 1

    verifier._verify_sync = instrumented_verify_sync

    async def fire_all():
        tasks = [
            asyncio.create_task(
                _asgi_exchange_async(harness.app, **_login_scope_kwargs(f"wrong-{i}"))
            )
            for i in range(40)
        ]
        return await asyncio.gather(*tasks)

    results = asyncio.run(fire_all())

    statuses = [status for status, _headers, _body, _calls in results]
    assert max_concurrent_seen == 1
    assert max_queue_seen == 0
    assert statuses.count(401) == 1  # exactly one reached real credential checking
    assert statuses.count(429) == 39
    assert set(statuses) == {401, 429}
    assert verifier._busy is False  # slot fully released after all work finished


def test_forty_concurrent_logins_one_correct_password_still_succeeds(app_factory):
    """Same 40-way concurrency burst, but one request carries the correct
    password, proving admission/rejection never corrupts a legitimate
    login and the slot is still released correctly afterward."""
    harness = app_factory()
    verifier = harness.manager.verifier

    async def fire_all():
        # Task creation order is deterministically preserved across the
        # identical await-point structure of every request in a
        # single-threaded event loop with no real timers, so index 0
        # reliably wins the single admission slot (independently confirmed
        # by test_forty_concurrent_logins_admit_at_most_one_argon2_verification
        # above, where index 0's "wrong-0" password was the sole admission).
        passwords = [PASSWORD] + ["wrong"] * 39
        tasks = [
            asyncio.create_task(
                _asgi_exchange_async(harness.app, **_login_scope_kwargs(pw))
            )
            for pw in passwords
        ]
        return await asyncio.gather(*tasks)

    results = asyncio.run(fire_all())
    statuses = [status for status, _headers, _body, _calls in results]
    assert statuses.count(200) == 1
    assert statuses.count(429) == 39
    assert verifier._busy is False

    # The executor must still accept new work afterward (not left in a
    # stuck/half-released state).
    async def one_more():
        return await _asgi_exchange_async(harness.app, **_login_scope_kwargs("wrong"))

    status, _headers, body, _calls = asyncio.run(one_more())
    assert status == 401
    assert json.loads(body) == {"detail": "Invalid username or password"}


def test_admission_slot_releases_even_when_verification_raises(app_factory):
    harness = app_factory()
    verifier = harness.manager.verifier

    def raising_verify_sync(_encoded_hash, _password):
        raise RuntimeError("simulated internal Argon2 failure")

    verifier._verify_sync = raising_verify_sync

    async def exercise():
        with pytest.raises(RuntimeError):
            await verifier.verify("whatever", use_dummy=False)
        assert verifier._busy is False
        # A fresh, working verification must be admitted immediately after.
        verifier._verify_sync = lambda _h, _p: True
        assert await verifier.verify(PASSWORD, use_dummy=False) is True

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("headers", "expected_status"),
    [
        ([(b"content-length", str(LOGIN_BODY_LIMIT + 1).encode())], 413),
        ([(b"content-length", b"-1")], 400),
        ([(b"content-length", b"not-a-number")], 400),
        ([(b"content-length", b"1"), (b"content-length", b"1")], 400),
        # Genuinely conflicting (not merely duplicated) Content-Length values.
        ([(b"content-length", b"10"), (b"content-length", b"20")], 400),
    ],
)
def test_login_content_length_is_rejected_before_body_or_json(
    app_factory, headers, expected_status
):
    harness = app_factory()
    status, _headers, body, receive_calls = asgi_exchange(
        harness.app,
        path="/api/auth/login",
        method="POST",
        headers=[(b"content-type", b"application/json"), *headers],
        chunks=[(b"this must not be consumed", False)],
    )
    assert status == expected_status
    assert receive_calls == 0
    expected_detail = "Login request too large" if expected_status == 413 else "Invalid request"
    assert json.loads(body) == {"detail": expected_detail}


def test_login_body_exactly_at_the_limit_is_accepted_not_rejected_for_size(
    app_factory,
):
    """A body of exactly LOGIN_BODY_LIMIT bytes must not be rejected purely
    for size; only strictly-over-the-limit bodies are 413."""
    harness = app_factory()

    # Grow the (wrong) password field until the serialized JSON is exactly
    # LOGIN_BODY_LIMIT bytes, so this exercises the true byte boundary.
    pad = 0
    while True:
        candidate = json.dumps({"username": "admin", "password": "x" * pad}).encode()
        if len(candidate) >= LOGIN_BODY_LIMIT:
            break
        pad += 1
    encoded = candidate
    assert len(encoded) == LOGIN_BODY_LIMIT

    status, _headers, body, receive_calls = asgi_exchange(
        harness.app,
        path="/api/auth/login",
        method="POST",
        headers=[
            (b"content-type", b"application/json"),
            (b"content-length", str(len(encoded)).encode()),
            (b"sec-fetch-site", b"same-origin"),
        ],
        chunks=[(encoded, False)],
    )
    # Rejected for being the wrong password (padded), never for size.
    assert status == 401
    assert receive_calls == 1
    assert json.loads(body) == {"detail": "Invalid username or password"}


def test_login_content_length_exactly_at_limit_plus_one_byte_body_is_413(app_factory):
    harness = app_factory()
    encoded = b"{" + b"x" * (LOGIN_BODY_LIMIT - 1)
    assert len(encoded) == LOGIN_BODY_LIMIT
    over_by_one = encoded + b"x"
    assert len(over_by_one) == LOGIN_BODY_LIMIT + 1

    status, _headers, body, receive_calls = asgi_exchange(
        harness.app,
        path="/api/auth/login",
        method="POST",
        headers=[
            (b"content-type", b"application/json"),
            (b"content-length", str(len(over_by_one)).encode()),
        ],
        chunks=[(over_by_one, False)],
    )
    assert status == 413
    assert receive_calls == 0
    assert json.loads(body) == {"detail": "Login request too large"}


def test_client_disconnect_mid_body_is_rejected_without_corrupting_state(app_factory):
    harness = app_factory()
    status, _headers, body, receive_calls = asgi_exchange(
        harness.app,
        path="/api/auth/login",
        method="POST",
        headers=[(b"content-type", b"application/json")],
        chunks=[(b'{"username":"a', True)],  # more_body=True, then stream ends
    )
    assert status == 400
    assert json.loads(body) == {"detail": "Invalid request"}

    # The verifier/rate-limiter must remain fully usable afterward.
    status2, _headers2, body2, _calls2 = asgi_exchange(
        harness.app, **_login_scope_kwargs(PASSWORD)
    )
    assert status2 == 200
    assert json.loads(body2) == {"authenticated": True, "username": "admin"}


@pytest.mark.parametrize(
    ("headers", "expected_status", "expected_detail"),
    [
        ([(b"content-type", b"text/plain")], 415, "Login requires application/json"),
        (
            [(b"content-type", b"application/json"), (b"sec-fetch-site", b"cross-site")],
            403,
            "Cross-origin login is not allowed",
        ),
        (
            [(b"content-type", b"application/json"), (b"sec-fetch-site", b"same-site")],
            403,
            "Cross-origin login is not allowed",
        ),
    ],
)
def test_cross_origin_or_non_json_login_is_rejected_before_body_and_rate_limit(
    app_factory, headers, expected_status, expected_detail
):
    harness = app_factory()
    status, _headers, body, receive_calls = asgi_exchange(
        harness.app,
        path="/api/auth/login",
        method="POST",
        headers=headers,
        chunks=[(b"body must not be consumed", False)],
    )
    assert status == expected_status
    assert receive_calls == 0
    assert json.loads(body) == {"detail": expected_detail}
    assert not harness.manager.rate_limiter._records


def test_same_origin_json_login_is_accepted(app_factory):
    harness = app_factory()
    encoded = json.dumps({"username": "admin", "password": PASSWORD}).encode()
    status, _headers, _body, receive_calls = asgi_exchange(
        harness.app,
        path="/api/auth/login",
        method="POST",
        headers=[
            (b"content-type", b"application/json; charset=utf-8"),
            (b"sec-fetch-site", b"same-origin"),
        ],
        chunks=[(encoded, False)],
    )
    assert status == 200
    assert receive_calls == 1


def test_streamed_login_is_limited_before_json_parsing(app_factory):
    harness = app_factory()
    status, _headers, body, receive_calls = asgi_exchange(
        harness.app,
        path="/api/auth/login",
        method="POST",
        headers=[(b"content-type", b"application/json")],
        chunks=[(b"{" + b"x" * 4999, True), (b"y" * 4000, False)],
    )
    assert status == 413
    assert receive_calls == 2
    assert json.loads(body) == {"detail": "Login request too large"}


def test_streamed_login_body_is_replayed_to_json_parser(app_factory):
    harness = app_factory()
    encoded = json.dumps({"username": "admin", "password": PASSWORD}).encode()
    split = len(encoded) // 2
    status, headers, body, receive_calls = asgi_exchange(
        harness.app,
        path="/api/auth/login",
        method="POST",
        headers=[(b"content-type", b"application/json")],
        chunks=[(encoded[:split], True), (encoded[split:], False)],
    )
    assert status == 200
    assert receive_calls == 2
    assert json.loads(body) == {"authenticated": True, "username": "admin"}
    assert headers["set-cookie"]


def test_incomplete_login_body_hits_bounded_whole_body_deadline(
    app_factory, monkeypatch
):
    harness = app_factory()
    monkeypatch.setattr(auth_module, "LOGIN_BODY_TIMEOUT_SECONDS", 0.01)
    messages = []

    async def receive():
        await asyncio.sleep(1)
        return {"type": "http.request", "body": b"", "more_body": True}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "scheme": "https",
        "method": "POST",
        "path": "/api/auth/login",
        "raw_path": b"/api/auth/login",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 443),
    }
    asyncio.run(harness.app(scope, receive, send))

    start = next(message for message in messages if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    assert start["status"] == 408
    assert json.loads(body) == {"detail": "Login request timed out"}


def test_blocked_client_is_rejected_before_login_body_is_read(app_factory):
    harness = app_factory()
    for _ in range(4):
        harness.manager.rate_limiter.failure("127.0.0.1")
    with pytest.raises(auth_module.LoginRateLimited):
        harness.manager.rate_limiter.failure("127.0.0.1")

    status, headers, body, receive_calls = asgi_exchange(
        harness.app,
        path="/api/auth/login",
        method="POST",
        headers=[(b"content-type", b"application/json")],
        chunks=[(b"body must remain unread", False)],
    )
    assert status == 429
    assert receive_calls == 0
    assert headers["retry-after"] == ["900"]
    assert json.loads(body) == {"detail": "Login temporarily unavailable"}


def test_disabled_auth_passes_http_and_websocket_without_csrf(app_factory):
    harness = app_factory(AuthState.DISABLED)
    with TestClient(harness.app, base_url="https://testserver") as client:
        assert client.get("/api/state").status_code == 200
        assert client.post("/api/apply").status_code == 200
        me = client.get("/api/auth/me")
        assert me.status_code == 200
        assert me.json()["auth_enabled"] is False
        assert login(client).status_code == 409
        assert client.post("/api/auth/logout").status_code == 204
        with client.websocket_connect("/ws") as websocket:
            assert websocket.receive_text() == "accepted"


def test_invalid_auth_serves_only_public_login_shell(app_factory):
    harness = app_factory(AuthState.INVALID)
    with TestClient(
        harness.app, base_url="https://testserver", follow_redirects=False
    ) as client:
        for path in sorted(PUBLIC_GET_PATHS):
            assert client.get(path).status_code == 200, path
        for path in ["/", "/docs", "/redoc"]:
            response = client.get(path)
            assert response.status_code == 503, path
            assert response.text == "Management interface unavailable."
        for path in ["/api/state", "/openapi.json", "/metrics", "/api/metrics"]:
            response = client.get(path)
            assert response.status_code == 503, path
            assert response.json() == {"detail": "Management interface unavailable."}
        response = login(client)
        assert response.status_code == 503
        assert response.json() == {"detail": "Management interface unavailable."}


@pytest.mark.parametrize(
    "path",
    ["/login", "/style.css", "/api/state", "/", "/docs", "/openapi.json"],
)
def test_security_headers_are_consistent_and_hsts_is_not_injected(app_factory, path):
    harness = app_factory()
    with TestClient(
        harness.app, base_url="https://testserver", follow_redirects=False
    ) as client:
        response = client.get(path)
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value
    assert "strict-transport-security" not in response.headers


def test_sensitive_responses_are_no_store_but_public_assets_remain_cacheable(app_factory):
    harness = app_factory()
    with TestClient(
        harness.app, base_url="https://testserver", follow_redirects=False
    ) as client:
        assert client.get("/login").headers["cache-control"] == "no-store"
        assert client.get("/api/state").headers["cache-control"] == "no-store"
        assert client.get("/").headers["cache-control"] == "no-store"
        assert "cache-control" not in client.get("/style.css").headers


@pytest.mark.parametrize("ready", [True, False], ids=["ready", "invalid"])
def test_main_lifespan_keeps_controller_task_running_and_auth_is_state_isolated(
    monkeypatch, tmp_path, password_hash_file, ready
):
    brisa_root = Path(__file__).resolve().parents[1] / "brisa"
    monkeypatch.chdir(brisa_root)
    main = importlib.import_module("app.main")
    controller = importlib.import_module("app.controller")
    hwmon_pwm = importlib.import_module("app.hwmon_pwm")

    manager = AuthManager()
    assert main.api.version == __version__ == "1.1.0"
    monkeypatch.setattr(main, "auth_manager", manager)
    monkeypatch.setattr(main.api.state, "auth", manager)
    monkeypatch.setattr(main, "_config", None)
    monkeypatch.setattr(main, "_loop_task", None)

    config = AppConfig()
    calls = []
    controller_state = {"fan-1": 42}
    takeover_state = {"fan-1"}
    monkeypatch.setattr(main, "load_config", lambda: calls.append("load_config") or config)
    monkeypatch.setattr(main, "init_db", lambda: calls.append("init_db"))
    monkeypatch.setattr(controller, "_last_applied", controller_state)
    monkeypatch.setattr(controller, "_pwm_taken_over", takeover_state)

    async def fake_loop():
        calls.append("controller_loop")
        try:
            await asyncio.Event().wait()
        finally:
            calls.append("controller_cancelled")

    monkeypatch.setattr(controller, "loop", fake_loop)
    monkeypatch.setattr(hwmon_pwm, "release_all", lambda: calls.append("release_all"))
    monkeypatch.setenv("BRISA_AUTH_ENABLED", "true")
    monkeypatch.setenv("BRISA_AUTH_USERNAME", "admin")
    monkeypatch.setenv("BRISA_SESSION_TTL_SECONDS", "300")
    monkeypatch.setenv("BRISA_SECURE_COOKIES", "true")
    monkeypatch.setenv("BRISA_TRUST_PROXY", "false")
    hash_path = password_hash_file if ready else tmp_path / "does-not-exist.hash"
    monkeypatch.setenv("BRISA_PASSWORD_HASH_FILE", str(hash_path))

    wrapped = AuthMiddleware(main.api, manager)
    with TestClient(wrapped, base_url="https://testserver") as client:
        assert calls[:3] == ["load_config", "init_db", "controller_loop"]
        assert main._loop_task is not None and not main._loop_task.done()
        assert manager.state is (AuthState.READY if ready else AuthState.INVALID)
        assert main.get_config() is config

        if ready:
            assert client.head("/login").status_code == 200
            assert login(client).status_code == 200
            token = csrf_token(client)
            assert client.post(
                "/api/auth/logout", headers={"X-CSRF-Token": token}
            ).status_code == 204
        else:
            assert client.get("/api/auth/me").status_code == 503

        assert main.get_config() is config
        assert controller._last_applied == {"fan-1": 42}
        assert controller._pwm_taken_over == {"fan-1"}

    assert calls[-2:] == ["release_all", "controller_cancelled"]


@pytest.mark.parametrize(
    "misconfiguration",
    [
        "missing_hash_file",
        "malformed_hash_content",
        "invalid_trusted_proxy_cidr",
        "invalid_auth_enabled_boolean",
    ],
)
def test_every_distinct_invalid_auth_cause_still_allows_controller_startup(
    monkeypatch, tmp_path, password_hash_file, misconfiguration
):
    """Each of four independent, realistic misconfiguration causes must
    leave the fan controller running and only fail the management surface
    closed; none may raise out of the ASGI lifespan startup."""
    brisa_root = Path(__file__).resolve().parents[1] / "brisa"
    monkeypatch.chdir(brisa_root)
    main = importlib.import_module("app.main")
    controller = importlib.import_module("app.controller")
    hwmon_pwm = importlib.import_module("app.hwmon_pwm")

    manager = AuthManager()
    monkeypatch.setattr(main, "auth_manager", manager)
    monkeypatch.setattr(main.api.state, "auth", manager)
    monkeypatch.setattr(main, "_config", None)
    monkeypatch.setattr(main, "_loop_task", None)

    config = AppConfig()
    calls = []
    monkeypatch.setattr(main, "load_config", lambda: calls.append("load_config") or config)
    monkeypatch.setattr(main, "init_db", lambda: calls.append("init_db"))

    async def fake_loop():
        calls.append("controller_loop")
        try:
            await asyncio.Event().wait()
        finally:
            calls.append("controller_cancelled")

    monkeypatch.setattr(controller, "loop", fake_loop)
    monkeypatch.setattr(hwmon_pwm, "release_all", lambda: calls.append("release_all"))

    monkeypatch.setenv("BRISA_AUTH_ENABLED", "true")
    monkeypatch.setenv("BRISA_AUTH_USERNAME", "admin")
    monkeypatch.setenv("BRISA_SESSION_TTL_SECONDS", "300")
    monkeypatch.setenv("BRISA_SECURE_COOKIES", "true")
    monkeypatch.setenv("BRISA_TRUST_PROXY", "false")
    monkeypatch.setenv("BRISA_PASSWORD_HASH_FILE", str(password_hash_file))

    if misconfiguration == "missing_hash_file":
        monkeypatch.setenv(
            "BRISA_PASSWORD_HASH_FILE", str(tmp_path / "does-not-exist.hash")
        )
    elif misconfiguration == "malformed_hash_content":
        malformed = tmp_path / "malformed.hash"
        malformed.write_text("this is not an argon2 hash at all", encoding="utf-8")
        monkeypatch.setenv("BRISA_PASSWORD_HASH_FILE", str(malformed))
    elif misconfiguration == "invalid_trusted_proxy_cidr":
        monkeypatch.setenv("BRISA_TRUST_PROXY", "true")
        monkeypatch.setenv("BRISA_TRUSTED_PROXY_CIDRS", "not-a-valid-cidr")
    elif misconfiguration == "invalid_auth_enabled_boolean":
        monkeypatch.setenv("BRISA_AUTH_ENABLED", "maybe")
    else:  # pragma: no cover - guards against a typo in the parametrize list
        raise AssertionError(misconfiguration)

    wrapped = AuthMiddleware(main.api, manager)
    # Entering the TestClient context runs the real ASGI lifespan startup.
    # If any exception escaped lifespan startup, this "with" block itself
    # would raise here, so simply completing it is part of the proof.
    with TestClient(wrapped, base_url="https://testserver") as client:
        # Controller task creation happens unconditionally, before the
        # auth-initialization worker thread even starts.
        assert calls[:3] == ["load_config", "init_db", "controller_loop"]
        assert main._loop_task is not None and not main._loop_task.done()

        assert manager.state is AuthState.INVALID, misconfiguration
        assert manager.error, "INVALID state must record an actionable reason"

        # The application is demonstrably still alive and serving traffic.
        me = client.get("/api/auth/me")
        assert me.status_code == 503
        assert me.json() == {"detail": "Management interface unavailable."}

        state_response = client.get("/api/state")
        assert state_response.status_code == 503

        login_page = client.get("/login")
        assert login_page.status_code == 200

    assert calls[-2:] == ["release_all", "controller_cancelled"]


def test_manual_smoke_flow_with_stub_state_and_apply(app_factory):
    harness = app_factory()
    config_before = dict(harness.runtime["config"])
    controller_before = dict(harness.runtime["controller"])
    with TestClient(harness.app, base_url="https://testserver") as client:
        assert client.get("/api/state").status_code == 401
        assert login(client).status_code == 200
        token = csrf_token(client)
        state = client.get("/api/state")
        assert state.status_code == 200
        assert state.json() == {"status": "stable"}
        assert client.post("/api/apply").status_code == 403
        applied = client.post("/api/apply", headers={"X-CSRF-Token": token})
        assert applied.status_code == 200
        assert applied.json() == {"status": "ok"}
        assert client.post(
            "/api/auth/logout", headers={"X-CSRF-Token": token}
        ).status_code == 204
        assert client.get("/api/state").status_code == 401

    assert harness.runtime["state_calls"] == 1
    assert harness.runtime["apply_calls"] == 1
    assert harness.runtime["config"] == config_before
    assert harness.runtime["controller"] == controller_before
