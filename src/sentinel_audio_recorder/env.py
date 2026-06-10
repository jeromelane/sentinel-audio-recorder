import os
from pathlib import Path


DEFAULT_ENV_PATH = Path(__file__).resolve().parent.parent.parent / ".env"


def load_env_file(path=DEFAULT_ENV_PATH):
    path = Path(path)
    if not path.is_file():
        return

    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or key in os.environ:
            continue

        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]

        os.environ[key] = value
