import logging

from fastapi import APIRouter, Request
from pydantic import BaseModel, ValidationError
from starlette.responses import JSONResponse, Response

from app.auth import (
    AuthConfigurationError,
    AuthManager,
    AuthState,
    LoginRateLimited,
    VerificationUnavailable,
    effective_client_ip,
)
from app.version import __version__

logger = logging.getLogger(__name__)
router = APIRouter()


class LoginRequest(BaseModel):
    username: str
    password: str


def _manager(request: Request) -> AuthManager:
    return request.app.state.auth


def _finish(
    manager: AuthManager,
    response: Response,
    request: Request,
    clear_invalid_cookie: bool = True,
) -> Response:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    if clear_invalid_cookie and getattr(request.state, "invalid_session_cookie", False):
        manager.clear_session_cookie(response)
    return response


def _rate_limited(manager: AuthManager, request: Request, retry_after: int) -> Response:
    response = JSONResponse({"detail": "Login temporarily unavailable"}, status_code=429)
    response.headers["Retry-After"] = str(max(1, retry_after))
    return _finish(manager, response, request)


@router.post("/login")
async def login(request: Request):
    manager = _manager(request)
    if manager.state is AuthState.DISABLED:
        return _finish(
            manager,
            JSONResponse({"detail": "Authentication is disabled"}, status_code=409),
            request,
        )
    if manager.state is not AuthState.READY:
        return _finish(
            manager,
            JSONResponse({"detail": "Management interface unavailable."}, status_code=503),
            request,
        )

    client = effective_client_ip(request.scope, manager.settings)
    try:
        payload = LoginRequest.model_validate(await request.json())
    except (ValidationError, ValueError, TypeError):
        try:
            manager.record_malformed_login(client)
        except LoginRateLimited as exc:
            return _rate_limited(manager, request, exc.retry_after)
        return _finish(
            manager,
            JSONResponse({"detail": "Invalid login request"}, status_code=422),
            request,
        )

    if (
        not 1 <= len(payload.username) <= 64
        or not 1 <= len(payload.password) <= 1024
        or not payload.username.isascii()
    ):
        try:
            manager.record_malformed_login(client)
        except LoginRateLimited as exc:
            return _rate_limited(manager, request, exc.retry_after)
        return _finish(
            manager,
            JSONResponse({"detail": "Invalid username or password"}, status_code=401),
            request,
        )

    try:
        authenticated = await manager.authenticate(payload.username, payload.password, client)
    except (LoginRateLimited, VerificationUnavailable) as exc:
        return _rate_limited(manager, request, exc.retry_after)
    except AuthConfigurationError:
        return _finish(
            manager,
            JSONResponse({"detail": "Management interface unavailable."}, status_code=503),
            request,
        )
    if not authenticated:
        return _finish(
            manager,
            JSONResponse({"detail": "Invalid username or password"}, status_code=401),
            request,
        )

    existing_token = getattr(request.state, "auth_session_token", None)
    token, session = manager.create_session(existing_token)
    response = JSONResponse({"authenticated": True, "username": session.username})
    manager.set_session_cookie(response, token)
    return _finish(manager, response, request, clear_invalid_cookie=False)


@router.get("/me")
async def me(request: Request):
    manager = _manager(request)
    if manager.state is AuthState.DISABLED:
        response = JSONResponse({
            "auth_enabled": False,
            "authenticated": False,
            "username": None,
            "csrf_token": None,
            "version": __version__,
        })
        return _finish(manager, response, request)
    if manager.state is not AuthState.READY:
        return _finish(
            manager,
            JSONResponse({"detail": "Management interface unavailable."}, status_code=503),
            request,
        )
    session = getattr(request.state, "auth_session", None)
    if session is None:
        return _finish(
            manager,
            JSONResponse({"detail": "Authentication required"}, status_code=401),
            request,
        )
    response = JSONResponse({
        "auth_enabled": True,
        "authenticated": True,
        "username": session.username,
        "csrf_token": session.csrf_token,
        "version": __version__,
    })
    return _finish(manager, response, request)


@router.post("/logout", status_code=204)
async def logout(request: Request):
    manager = _manager(request)
    if manager.state is AuthState.DISABLED:
        response = Response(status_code=204)
        return _finish(manager, response, request)
    if manager.state is not AuthState.READY:
        return _finish(
            manager,
            JSONResponse({"detail": "Management interface unavailable."}, status_code=503),
            request,
        )
    token = getattr(request.state, "auth_session_token", None)
    if token is None:
        return _finish(
            manager,
            JSONResponse({"detail": "Authentication required"}, status_code=401),
            request,
        )
    manager.sessions.delete(token)
    response = Response(status_code=204)
    manager.clear_session_cookie(response)
    return _finish(manager, response, request)
