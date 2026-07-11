import contextlib
import datetime
import threading
import snmp_service

import fastapi
import fastapi.staticfiles
import fastapi.templating
import fastapi.responses
import pydantic
import starlette.requests
import starlette.middleware.sessions

import config
import influx_service
import serial_reader
import state


class GainSetRequest(pydantic.BaseModel):
    gain_set: float


class DashboardSettingsRequest(pydantic.BaseModel):
    gain_tolerance: float | None = None
    warn_limits: dict[str, dict[str, float | None]] | None = None


class AccessUserCreateRequest(pydantic.BaseModel):
    username: str
    password: str
    role: str = "Operator"
    active: bool = True


class AccessUserUpdateRequest(pydantic.BaseModel):
    password: str | None = None
    role: str | None = None
    active: bool | None = None

class SnmpSettingsUpdateRequest(pydantic.BaseModel):
    enabled: bool
    port: int
    community: str
    trap_host: str
    trap_port: int


def find_access_user(username: str) -> dict | None:
    for user in state.access_users:
        if user["username"] == username:
            return user

    return None


def normalize_username(username: str) -> str:
    value = username.strip()

    if not value:
        raise fastapi.HTTPException(status_code=400, detail="Username is required")

    return value


def parse_memory_range(range_value: str):
    now = datetime.datetime.now(datetime.timezone.utc)

    if range_value == "5m":
        return now - datetime.timedelta(minutes=5)

    if range_value == "1h":
        return now - datetime.timedelta(hours=1)

    if range_value == "24h":
        return now - datetime.timedelta(hours=24)

    if range_value == "7d":
        return now - datetime.timedelta(days=7)

    if range_value == "30d":
        return now - datetime.timedelta(days=30)

    return None


def query_history_from_memory(range_value: str):
    start_time = parse_memory_range(range_value)

    with state.state_lock:
        points = list(state.history_buffer)

    if start_time is None:
        return points

    filtered_points = []

    for point in points:
        point_time = datetime.datetime.fromisoformat(point["time"])

        if point_time >= start_time:
            filtered_points.append(point)

    return filtered_points


@contextlib.asynccontextmanager
async def lifespan(app: fastapi.FastAPI):
    influx_service.init_influx()
    snmp_service.init_snmp() # INICJALIZACJA SNMP

    state.stop_event.clear()

    thread = threading.Thread(
        target=serial_reader.serial_reader_loop,
        daemon=True
    )

    thread.start()

    yield

    print("Shutting down application...")

    state.stop_event.set()
    thread.join(timeout=2)

    influx_service.close_influx()
    snmp_service.close_snmp() # ZAMKNIĘCIE SNMP


app = fastapi.FastAPI(lifespan=lifespan)

PUBLIC_PATHS = {"/login"}


@app.middleware("http")
async def auth_guard(request: starlette.requests.Request, call_next):
    path = request.url.path
    logged_in = bool(request.session.get("username"))

    if path.startswith("/static"):
        return await call_next(request)

    if path in PUBLIC_PATHS:
        return await call_next(request)

    if not logged_in:
        if path.startswith("/api"):
            return fastapi.responses.JSONResponse({"detail": "Not authenticated"}, status_code=401)
        return fastapi.responses.RedirectResponse("/login")

    return await call_next(request)


app.add_middleware(
    starlette.middleware.sessions.SessionMiddleware,
    secret_key=config.SESSION_SECRET_KEY,
    session_cookie="amp_session",
)


app.mount(
    "/static",
    fastapi.staticfiles.StaticFiles(directory="static"),
    name="static"
)

templates = fastapi.templating.Jinja2Templates(directory="templates")


@app.get("/login")
def login_page(request: starlette.requests.Request):
    if request.session.get("username"):
        return fastapi.responses.RedirectResponse("/")

    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": None}
    )


@app.post("/login")
def login_submit(
    request: starlette.requests.Request,
    username: str = fastapi.Form(...),
    password: str = fastapi.Form(...),
):
    user = find_access_user(username.strip())

    if user is None or not user.get("active", True) or not state.verify_password(user, password):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Nieprawidłowy login lub hasło"},
            status_code=401,
        )

    request.session["username"] = user["username"]
    request.session["role"] = user["role"]

    return fastapi.responses.RedirectResponse("/", status_code=303)


@app.get("/logout")
def logout(request: starlette.requests.Request):
    request.session.clear()
    return fastapi.responses.RedirectResponse("/login")


@app.get("/")
def home(request: starlette.requests.Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"username": request.session.get("username")}
    )


@app.get("/api/latest")
def latest():
    with state.state_lock:
        return {
            "connected": state.serial_connected,
            "error": state.serial_error,
            "last_update": state.last_update,
            "last_command_response": state.last_command_response,
            "last_known_gain_set": state.last_known_gain_set,
            "data": state.latest_data
        }


@app.get("/api/status")
def status():
    with state.state_lock:
        return {
            "device": config.DEVICE_NAME,
            "serial_port": config.SERIAL_PORT,
            "serial_connected": state.serial_connected,
            "serial_error": state.serial_error,
            "influx_enabled": config.INFLUX_ENABLED
        }

@app.get("/api/snmp/live_data")
def get_snmp_live_data():
    with state.state_lock:
        return getattr(state, "latest_snmp_data", {})
    
@app.get("/api/settings")
def get_settings():
    with state.state_lock:
        return state.dashboard_settings


@app.get("/api/access/users")
def get_access_users():
    with state.state_lock:
        return {
            "users": [state.access_user_public(user) for user in state.access_users]
        }


@app.post("/api/access/users")
def create_access_user(request: AccessUserCreateRequest):
    username = normalize_username(request.username)

    if not request.password:
        raise fastapi.HTTPException(status_code=400, detail="Password is required")

    with state.state_lock:
        if find_access_user(username) is not None:
            raise fastapi.HTTPException(status_code=409, detail="User already exists")

        password_hash, password_salt = state.hash_password(request.password)
        user = {
            "username": username,
            "role": request.role.strip() or "Operator",
            "active": bool(request.active),
            "password_hash": password_hash,
            "password_salt": password_salt,
        }
        state.access_users.append(user)
        state.save_persisted_access_users()

        return state.access_user_public(user)


@app.put("/api/access/users/{username}")
def update_access_user(username: str, request: AccessUserUpdateRequest):
    username = normalize_username(username)

    with state.state_lock:
        user = find_access_user(username)

        if user is None:
            raise fastapi.HTTPException(status_code=404, detail="User not found")

        if request.role is not None:
            user["role"] = request.role.strip() or "Operator"

        if request.active is not None:
            user["active"] = bool(request.active)

        if request.password:
            user["password_hash"], user["password_salt"] = state.hash_password(request.password)

        state.save_persisted_access_users()

        return state.access_user_public(user)


@app.delete("/api/access/users/{username}")
def delete_access_user(username: str):
    username = normalize_username(username)

    with state.state_lock:
        user = find_access_user(username)

        if user is None:
            raise fastapi.HTTPException(status_code=404, detail="User not found")

        if len(state.access_users) == 1:
            raise fastapi.HTTPException(status_code=400, detail="At least one user is required")

        state.access_users.remove(user)
        state.save_persisted_access_users()

    return {
        "status": "ok"
    }


@app.post("/api/settings")
def update_settings(request: DashboardSettingsRequest):
    with state.state_lock:
        if request.gain_tolerance is not None:
            state.dashboard_settings["gain_tolerance"] = float(request.gain_tolerance)

        if request.warn_limits is not None:
            for key, limits in request.warn_limits.items():
                if key not in state.dashboard_settings["warn_limits"]:
                    continue

                for side in ("min", "max"):
                    if side in limits:
                        value = limits[side]
                        state.dashboard_settings["warn_limits"][key][side] = None if value is None else float(value)

        state.save_persisted_dashboard_settings()

        return state.dashboard_settings


@app.get("/api/errors")
def get_errors():
    with state.state_lock:
        return {
            "errors": list(state.error_buffer)
        }


@app.post("/api/errors/clear")
def clear_errors():
    with state.state_lock:
        state.error_buffer.clear()

    return {
        "errors": []
    }
@app.get("/api/snmp/settings")
def get_snmp_settings():
    with state.state_lock:
        return state.snmp_settings

@app.post("/api/snmp/settings")
def update_snmp_settings(request: SnmpSettingsUpdateRequest):
    import snmp_service  # import wewnątrz, żeby uniknąć zapętlonych importów
    with state.state_lock:
        state.snmp_settings = {
            "enabled": request.enabled,
            "port": request.port,
            "community": request.community,
            "trap_host": request.trap_host,
            "trap_port": request.trap_port
        }
        state.save_persisted_state()
    
    # Ważne: restartujemy usługę SNMP z nowymi parametrami!
    snmp_service.close_snmp()
    if request.enabled:
        snmp_service.init_snmp()
        
    return state.snmp_settings

@app.get("/api/history")
def history(range: str = "5m"):
    try:
        influx_points = influx_service.query_history_from_influx(range)
    except Exception as e:
        print("InfluxDB history query failed:", e)
        influx_points = None

    if influx_points is not None:
        return {
            "source": "influx",
            "range": range,
            "points": influx_points
        }

    memory_points = query_history_from_memory(range)

    return {
        "source": "memory",
        "range": range,
        "points": memory_points
    }


@app.post("/api/set_gain")
def set_gain(request: GainSetRequest):
    try:
        serial_reader.send_gain_set(request.gain_set)
    except RuntimeError as e:
        raise fastapi.HTTPException(
            status_code=503,
            detail=str(e)
        )

    return {
        "status": "ok",
        "gain_set": request.gain_set
    }