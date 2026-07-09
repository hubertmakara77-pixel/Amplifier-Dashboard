import collections
import base64
import hashlib
import json
import pathlib
import secrets
import threading

import config


DEFAULT_DASHBOARD_SETTINGS = {
    "gain_tolerance": 0.25,
    "warn_limits": {
        "p_a_in": {"min": None, "max": None},
        "p_a_out": {"min": None, "max": None},
        "p_b_in": {"min": None, "max": None},
        "p_b_out": {"min": None, "max": None},
        "temperature": {"min": None, "max": None},
    },
}

DEFAULT_ACCESS_USERS = [
    {
        "username": "admin",
        "role": "Administrator",
        "active": True,
        "password_hash": "",
        "password_salt": "",
    }
]
DEFAULT_SNMP_SETTINGS = {
    "enabled": False,
    "port": 161,
    "community": "public",
    "trap_host": "127.0.0.1",
    "trap_port": 162
}

def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    if salt is None:
        salt_bytes = secrets.token_bytes(16)
        salt = base64.b64encode(salt_bytes).decode("ascii")
    else:
        salt_bytes = base64.b64decode(salt.encode("ascii"))

    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt_bytes,
        120_000,
    )

    return base64.b64encode(digest).decode("ascii"), salt


DEFAULT_ACCESS_USERS[0]["password_hash"], DEFAULT_ACCESS_USERS[0]["password_salt"] = hash_password("admin")


def load_persisted_state() -> dict:
    path = pathlib.Path(config.PERSISTED_STATE_FILE)

    if not path.exists():
        return {}

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def merge_dashboard_settings(saved_settings: dict | None) -> dict:
    settings = json.loads(json.dumps(DEFAULT_DASHBOARD_SETTINGS))

    if not isinstance(saved_settings, dict):
        return settings

    if "gain_tolerance" in saved_settings:
        settings["gain_tolerance"] = float(saved_settings["gain_tolerance"])

    saved_limits = saved_settings.get("warn_limits")
    if isinstance(saved_limits, dict):
        for field, limits in saved_limits.items():
            if field not in settings["warn_limits"] or not isinstance(limits, dict):
                continue

            for side in ("min", "max"):
                if side in limits:
                    value = limits[side]
                    settings["warn_limits"][field][side] = None if value is None else float(value)

    return settings


def access_user_public(user: dict) -> dict:
    return {
        "username": user["username"],
        "role": user["role"],
        "active": bool(user["active"]),
        "password_set": bool(user.get("password_hash")),
    }


def merge_access_users(saved_users: list[dict] | None) -> list[dict]:
    users = json.loads(json.dumps(DEFAULT_ACCESS_USERS))

    if not isinstance(saved_users, list):
        return users

    merged_users = []
    seen_usernames = set()

    for user in saved_users:
        if not isinstance(user, dict):
            continue

        username = str(user.get("username", "")).strip()
        if not username or username in seen_usernames:
            continue

        merged_users.append({
            "username": username,
            "role": str(user.get("role", "Operator")).strip() or "Operator",
            "active": bool(user.get("active", True)),
            "password_hash": str(user.get("password_hash", "")),
            "password_salt": str(user.get("password_salt", "")),
        })
        seen_usernames.add(username)

    return merged_users or users


persisted_state = load_persisted_state()


def save_persisted_state() -> None:
    path = pathlib.Path(config.PERSISTED_STATE_FILE)
    payload = {
        "last_known_gain_set": float(last_known_gain_set),
        "dashboard_settings": dashboard_settings,
        "access_users": access_users,
        "snmp_settings": snmp_settings,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def save_persisted_gain_set(gain_set: float) -> None:
    global last_known_gain_set
    last_known_gain_set = float(gain_set)
    save_persisted_state()


def save_persisted_dashboard_settings() -> None:
    save_persisted_state()


def save_persisted_access_users() -> None:
    save_persisted_state()


latest_data = {}
serial_connected = False
serial_error = None
last_update = None
last_command_response = None
last_known_gain_set = float(persisted_state.get("last_known_gain_set", 15.0))

serial_port = None

state_lock = threading.Lock()
serial_lock = threading.Lock()
stop_event = threading.Event()

history_buffer = collections.deque(maxlen=config.HISTORY_MEMORY_LIMIT)
error_buffer = collections.deque(maxlen=500)
active_warning_keys = set()

dashboard_settings = merge_dashboard_settings(persisted_state.get("dashboard_settings"))
access_users = merge_access_users(persisted_state.get("access_users"))

snmp_settings = persisted_state.get("snmp_settings", DEFAULT_SNMP_SETTINGS.copy())


snmp_settings = persisted_state.get("snmp_settings", {
    "enabled": False,
    "port": 1611,
    "community": "public",
    "trap_host": "127.0.0.1",
    "trap_port": 162
})