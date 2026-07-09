import config


try:
    import influxdb_client
    import influxdb_client.client.write_api
except ImportError:
    influxdb_client = None


client = None
write_api = None
query_api = None


def init_influx():
    global client
    global write_api
    global query_api

    if not config.INFLUX_ENABLED:
        print("InfluxDB disabled in config.py")
        return

    if influxdb_client is None:
        print("Missing influxdb-client library")
        print("Install it with: py -3.14 -m pip install influxdb-client")
        return

    client = influxdb_client.InfluxDBClient(
        url=config.INFLUX_URL,
        token=config.INFLUX_TOKEN,
        org=config.INFLUX_ORG
    )

    write_api = client.write_api(
        write_options=influxdb_client.client.write_api.SYNCHRONOUS
    )

    query_api = client.query_api()

    print("Connected to InfluxDB")


def write_measurement(data: dict):
    if not config.INFLUX_ENABLED:
        return

    if write_api is None:
        return

    point = influxdb_client.Point(config.MEASUREMENT_NAME)
    point = point.tag("device", config.DEVICE_NAME)

    for key, value in data.items():
        if isinstance(value, int) or isinstance(value, float):
            point = point.field(key, float(value))

    write_api.write(
        bucket=config.INFLUX_BUCKET,
        org=config.INFLUX_ORG,
        record=point
    )


def write_setpoint(gain_set: float):
    if not config.INFLUX_ENABLED:
        return

    if write_api is None:
        return

    point = influxdb_client.Point(config.SETPOINT_MEASUREMENT_NAME)
    point = point.tag("device", config.DEVICE_NAME)
    point = point.field("gain_set", float(gain_set))

    write_api.write(
        bucket=config.INFLUX_BUCKET,
        org=config.INFLUX_ORG,
        record=point
    )


def get_window_for_range(range_value: str) -> str:
    if range_value == "5m":
        return "1s"

    if range_value == "1h":
        return "10s"

    if range_value == "24h":
        return "1m"

    if range_value == "7d":
        return "10m"

    if range_value == "30d":
        return "30m"

    if range_value == "all":
        return "1h"

    return "1s"


def get_flux_range(range_value: str) -> str:
    if range_value == "5m":
        return "-5m"

    if range_value == "1h":
        return "-1h"

    if range_value == "24h":
        return "-24h"

    if range_value == "7d":
        return "-7d"

    if range_value == "30d":
        return "-30d"

    if range_value == "all":
        return "0"

    return "-5m"


def query_history_from_influx(range_value: str):
    if not config.INFLUX_ENABLED:
        return None

    if query_api is None:
        return None

    flux_range = get_flux_range(range_value)
    window = get_window_for_range(range_value)

    fields = [
        "p_a_in",
        "p_a_out",
        "p_b_in",
        "p_b_out",
        "gain_set",
        "gain_actual",
        "gain_delta",
        "temperature",
    ]

    field_filter = " or ".join([
        f'r._field == "{field}"'
        for field in fields
    ])

    query = f'''
from(bucket: "{config.INFLUX_BUCKET}")
  |> range(start: {flux_range})
  |> filter(fn: (r) => r._measurement == "{config.MEASUREMENT_NAME}")
  |> filter(fn: (r) => r.device == "{config.DEVICE_NAME}")
  |> filter(fn: (r) => {field_filter})
  |> aggregateWindow(every: {window}, fn: mean, createEmpty: false)
  |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
'''

    tables = query_api.query(query, org=config.INFLUX_ORG)

    points = []

    for table in tables:
        for record in table.records:
            point = {
                "time": record.values["_time"].isoformat()
            }

            for field in fields:
                if field in record.values:
                    point[field] = record.values[field]

            points.append(point)

    points.sort(key=lambda item: item["time"])

    return points


def close_influx():
    global client
    global write_api
    global query_api

    if client is not None:
        client.close()

    client = None
    write_api = None
    query_api = None
