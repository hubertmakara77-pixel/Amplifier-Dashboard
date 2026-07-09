def parse_line(line: str) -> dict:
    data = {}

    for part in line.split(";"):
        if "=" not in part:
            continue

        key, value = part.split("=", 1)

        key = key.strip()
        value = value.strip()

        try:
            value = float(value)
        except ValueError:
            pass

        data[key] = value

    return data