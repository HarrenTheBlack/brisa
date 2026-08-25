import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api.auth_routes import router as auth_router
from app.api.routes import router
from app.auth import AuthMiddleware, AuthState, AuthManager
from app.config import load_config
from app.database import init_db
from app.models import AppConfig
from app.version import __version__

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_config: AppConfig | None = None
_loop_task: asyncio.Task | None = None
auth_manager = AuthManager()


def get_config() -> AppConfig:
    if _config is None:
        raise RuntimeError("Config not loaded")
    return _config


def set_config(config: AppConfig) -> None:
    global _config
    _config = config


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _loop_task

    logger.info("Brisa starting up")

    config = load_config()
    set_config(config)
    logger.info("Config loaded: %d curve(s), %d fan config(s)",
                len(config.curves), len(config.fan_configs))

    init_db()

    from app.controller import loop
    _loop_task = asyncio.create_task(loop())
    await asyncio.sleep(0)

    # Authentication must never prevent the independently scheduled fan loop.
    try:
        await asyncio.to_thread(auth_manager.initialize)
    except Exception as exc:
        auth_manager.mark_invalid(f"Unexpected authentication initialization error: {exc}")

    yield

    logger.info("Brisa shutting down")

    # Restore hwmon-pwm fans to their original auto/firmware mode
    from app import hwmon_pwm
    hwmon_pwm.release_all()

    if _loop_task:
        _loop_task.cancel()
        try:
            await _loop_task
        except asyncio.CancelledError:
            pass

    auth_manager.close()


api = FastAPI(
    title="Brisa",
    description="Docker-based fan control service",
    version=__version__,
    lifespan=lifespan,
)
api.state.auth = auth_manager

api.include_router(auth_router, prefix="/api/auth")
api.include_router(router, prefix="/api")


@api.api_route("/login", methods=["GET", "HEAD"], include_in_schema=False)
async def login_page():
    if auth_manager.state is AuthState.DISABLED:
        return RedirectResponse("/", status_code=303)
    static_dir = Path(__file__).resolve().parent / "static"
    return FileResponse(static_dir / "login.html")


@api.get("/metrics", include_in_schema=False)
async def metrics():
    from app.sensors import detect_sensors
    from app.liquidctl_wrapper import get_fan_status
    from app import hwmon_pwm

    sensors = detect_sensors()

    fans = []
    try:
        fans.extend(get_fan_status())
    except Exception:
        pass

    # Add hwmon-pwm fans with RPM readings
    config = get_config()
    for fc in config.fan_configs:
        if fc.backend == "hwmon-pwm":
            rpm = hwmon_pwm.get_fan_rpm(fc.fan_id)
            if rpm is not None:
                fans.append({"id": fc.fan_id, "label": fc.fan_label, "current_rpm": rpm})

    lines = []

    lines.append("# HELP brisa_temperature_celsius Current temperature reading")
    lines.append("# TYPE brisa_temperature_celsius gauge")
    for s in sensors:
        label = s["label"].replace('"', '\\"')
        driver = s["driver"].replace('"', '\\"')
        lines.append(
            f'brisa_temperature_celsius{{sensor="{driver}",label="{label}"}} {s["current_temp"]}'
        )

    lines.append("# HELP brisa_fan_rpm Current fan RPM")
    lines.append("# TYPE brisa_fan_rpm gauge")
    for f in fans:
        fan_id = f["id"].replace('"', '\\"')
        fan_label = f["label"].replace('"', '\\"')
        lines.append(
            f'brisa_fan_rpm{{fan="{fan_id}",label="{fan_label}"}} {f["current_rpm"]}'
        )

    return PlainTextResponse("\n".join(lines) + "\n")


api.mount("/", StaticFiles(directory="app/static", html=True), name="static")

# This wrapper is deliberately outermost so docs, API routes, metrics, errors,
# and the root StaticFiles mount share one enforcement boundary.
app = AuthMiddleware(api, auth_manager)
