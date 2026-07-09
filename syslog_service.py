import datetime
import socket

import config


FACILITY_LOCAL0 = config.SYSLOG_FACILITY
SEVERITY_WARNING = 4


def send_warning(message: str) -> None:
    if not config.SYSLOG_ENABLED:
        return

    priority = FACILITY_LOCAL0 * 8 + SEVERITY_WARNING
    timestamp = datetime.datetime.now().strftime("%b %d %H:%M:%S")
    hostname = socket.gethostname()
    payload = f"<{priority}>{timestamp} {hostname} {config.SYSLOG_APP_NAME}: {message}"

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(payload.encode("utf-8"), (config.SYSLOG_HOST, config.SYSLOG_PORT))
    except OSError as error:
        print(f"Syslog send failed: {error}")
