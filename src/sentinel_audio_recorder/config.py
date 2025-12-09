import configparser
import logging
import os
import json
from dataclasses import dataclass
from typing import Optional


@dataclass
class AppConfig:
    recording_dir: str = "recordings"
    device_index: Optional[int] = None
    sample_rate: Optional[int] = None
    duration: int = 3600
    loop: bool = False
    trigger: Optional[bool] = None
    threshold: int = 1500
    silence_timeout: int = 20
    upload_endpoint: Optional[str] = None
    upload_token: Optional[str] = None
    background_enabled: bool = False


def _as_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def _get_from_ini(parser: configparser.ConfigParser, key: str):
    if parser.has_option("sentinel", key):
        return parser.get("sentinel", key)
    return None


def load_config(config_path: Optional[str] = None) -> AppConfig:
    """Load configuration from environment variables or an INI file."""
    env = os.environ
    parser = configparser.ConfigParser()

    if config_path is None:
        config_path = env.get("SENTINEL_CONFIG_FILE")

    if config_path and os.path.exists(config_path):
        parser.read(config_path)

    def get_value(env_key: str, ini_key: str, cast, default=None):
        raw = env.get(env_key)
        if raw is None:
            raw = _get_from_ini(parser, ini_key)
        if raw is None:
            return default
        try:
            return cast(raw)
        except ValueError:
            return default

    config = AppConfig(
        recording_dir=get_value("SENTINEL_RECORDING_DIR", "recording_dir", str, "recordings"),
        device_index=get_value("SENTINEL_DEVICE_INDEX", "device_index", int, None),
        sample_rate=get_value("SENTINEL_SAMPLE_RATE", "sample_rate", int, None),
        duration=get_value("SENTINEL_DURATION", "duration", int, 3600),
        loop=_as_bool(get_value("SENTINEL_LOOP", "loop", str, False)),
        trigger=_as_bool(get_value("SENTINEL_TRIGGER", "trigger", str, None), default=None),
        threshold=get_value("SENTINEL_THRESHOLD", "threshold", int, 1500),
        silence_timeout=get_value("SENTINEL_SILENCE_TIMEOUT", "silence_timeout", int, 20),
        upload_endpoint=get_value("SENTINEL_UPLOAD_ENDPOINT", "upload_endpoint", str, None),
        upload_token=get_value("SENTINEL_UPLOAD_TOKEN", "upload_token", str, None),
        background_enabled=_as_bool(get_value("SENTINEL_BACKGROUND_ENABLED", "background_enabled", str, False)),
    )

    return config


class StructuredFormatter(logging.Formatter):
    def format(self, record):
        log_record = {
            "ts": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            log_record["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_record)


def setup_logging(level=logging.INFO):
    """Configure root logger to emit structured log records once per process."""
    root = logging.getLogger()
    if root.handlers:
        return

    handler = logging.StreamHandler()
    handler.setFormatter(StructuredFormatter())
    root.setLevel(level)
    root.addHandler(handler)
