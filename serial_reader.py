import datetime
import time
import snmp_service

import serial

import config
import influx_service
import parser
import state
import syslog_service


MEASUREMENT_FIELDS = {"p_a_in", "p_a_out", "p_b_in", "p_b_out"}


def write_gain_command(ser, gain_set: float) -> None:
    command = f"SET_GAIN={gain_set:.2f}\n"
    ser.write(command.encode("utf-8"))
    ser.flush()


def enrich_data(data: dict) -> dict:
    if "gain_actual" not in data:
        required = ["p_a_in", "p_a_out", "p_b_in", "p_b_out"]

        if all(key in data for key in required):
            gain_a = data["p_a_out"] - data["p_a_in"]
            gain_b = data["p_b_out"] - data["p_b_in"]
            data["gain_actual"] = (gain_a + gain_b) / 2.0

    if "gain_delta" not in data:
        if "gain_set" in data and "gain_actual" in data:
            data["gain_delta"] = data["gain_set"] - data["gain_actual"]

    return data


def is_command_response(data: dict) -> bool:
    return "status" in data and not any(field in data for field in MEASUREMENT_FIELDS)


def build_limit_errors(data: dict, now: str) -> list:
    errors = []
    settings = state.dashboard_settings
    warn_limits = settings["warn_limits"]

    if "gain_delta" in data:
        allowed = float(settings["gain_tolerance"])
        delta = float(data["gain_delta"])

        if abs(delta) > allowed:
            errors.append({
                "time": now,
                "field": "gain_actual",
                "kind": "gain_tolerance",
                "label": "Gain actual",
                "value": float(data.get("gain_actual", 0)),
                "target": float(data.get("gain_set", state.last_known_gain_set)),
                "delta": delta,
                "allowed": allowed,
                "message": "Gain outside tolerance",
            })

    for field, limits in warn_limits.items():
        if field not in data:
            continue

        value = float(data[field])
        min_value = limits.get("min")
        max_value = limits.get("max")

        if min_value is not None and value < float(min_value):
            delta = value - float(min_value)
            errors.append({
                "time": now,
                "field": field,
                "kind": "min",
                "label": field,
                "value": value,
                "target": float(min_value),
                "delta": delta,
                "allowed": 0,
                "message": f"{field} below MIN threshold {float(min_value):.2f}",
            })

        if max_value is not None and value > float(max_value):
            delta = value - float(max_value)
            errors.append({
                "time": now,
                "field": field,
                "kind": "max",
                "label": field,
                "value": value,
                "target": float(max_value),
                "delta": delta,
                "allowed": 0,
                "message": f"{field} above MAX threshold {float(max_value):.2f}",
            })

    return errors


def warning_key(error: dict) -> tuple:
    return (error.get("field"), error.get("kind"))


def format_syslog_warning(error: dict) -> str:
    return (
        f'WARNING field={error.get("field")} '
        f'kind={error.get("kind")} '
        f'value={float(error.get("value", 0)):.2f} '
        f'threshold={float(error.get("target", 0)):.2f} '
        f'delta={float(error.get("delta", 0)):.2f} '
        f'message="{error.get("message", "")}"'
    )


def serial_reader_loop():
    try:
        ser = serial.Serial(
            port=config.SERIAL_PORT,
            baudrate=config.SERIAL_BAUDRATE,
            timeout=1
        )

        with state.serial_lock:
            state.serial_port = ser

        with state.state_lock:
            state.serial_connected = True
            state.serial_error = None

        print(f"Connected to serial port {config.SERIAL_PORT}")

        time.sleep(2)

        with state.state_lock:
            restored_gain_set = state.last_known_gain_set

        ser.reset_input_buffer()
        write_gain_command(ser, restored_gain_set)
        print(f"Restored gain_set={restored_gain_set:.2f}")

        while not state.stop_event.is_set():
            line = ser.readline().decode("utf-8", errors="replace").strip()

            if not line:
                continue

            data = parser.parse_line(line)

            if not data:
                continue

            now = datetime.datetime.now(datetime.timezone.utc).isoformat()

            if is_command_response(data):
                with state.state_lock:
                    state.last_command_response = {
                        "time": now,
                        **data
                    }

                    if "gain_set" in data:
                        state.last_known_gain_set = float(data["gain_set"])
                        state.save_persisted_gain_set(state.last_known_gain_set)

                    state.serial_connected = True
                    state.serial_error = None

                print("Command response:", data)
                continue

            data = enrich_data(data)

            if "gain_set" in data:
                state.last_known_gain_set = float(data["gain_set"])
                state.save_persisted_gain_set(state.last_known_gain_set)

            limit_errors = build_limit_errors(data, now)
            current_warning_keys = {warning_key(error) for error in limit_errors}
            syslog_warnings = []

            with state.state_lock:
                state.latest_data = data
                state.last_update = now
                state.serial_connected = True
                state.serial_error = None

                for error in limit_errors:
                    state.error_buffer.appendleft(error)
                    key = warning_key(error)

                    if key not in state.active_warning_keys:
                        syslog_warnings.append(error)

                state.history_buffer.append({
                    "time": now,
                    **data
                })

                state.active_warning_keys = current_warning_keys

            influx_service.write_measurement(data)

            for error in syslog_warnings:
                syslog_service.send_warning(format_syslog_warning(error))
                snmp_service.send_trap(error)
            print("Reading:", data)

    except serial.SerialException as e:
        with state.state_lock:
            state.serial_connected = False
            state.serial_error = str(e)

        print("Serial port error:", e)

    finally:
        with state.serial_lock:
            if state.serial_port is not None:
                try:
                    state.serial_port.close()
                except serial.SerialException:
                    pass

                state.serial_port = None

        with state.state_lock:
            state.serial_connected = False


def send_gain_set(gain_set: float):
    with state.serial_lock:
        if state.serial_port is None:
            raise RuntimeError("Serial port is not open")

        write_gain_command(state.serial_port, gain_set)

    with state.state_lock:
        state.last_known_gain_set = float(gain_set)
        state.save_persisted_gain_set(state.last_known_gain_set)

    influx_service.write_setpoint(gain_set)
