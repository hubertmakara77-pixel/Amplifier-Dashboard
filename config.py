SERIAL_PORT = "COM4"
SERIAL_BAUDRATE = 9600

DEVICE_NAME = "optical_amp_1"

# Klucz do podpisywania ciasteczek sesji logowania.
# W realnym wdrożeniu wygeneruj losowy, długi ciąg i trzymaj go poza kodem (np. zmienna środowiskowa).
SESSION_SECRET_KEY = "zmien-ten-klucz-na-cos-losowego-i-dlugiego"

INFLUX_ENABLED = True

INFLUX_URL = "http://localhost:8086"
INFLUX_TOKEN = "my-super-token"
INFLUX_ORG = "agh"
INFLUX_BUCKET = "sensors"

MEASUREMENT_NAME = "optical_amp_status"
SETPOINT_MEASUREMENT_NAME = "optical_amp_setpoint"

HISTORY_MEMORY_LIMIT = 10000

PERSISTED_STATE_FILE = "persisted_state.json"

SYSLOG_ENABLED = True
SYSLOG_HOST = "127.0.0.1"
SYSLOG_PORT = 514
SYSLOG_APP_NAME = "amp-dashboard"
SYSLOG_FACILITY = 16