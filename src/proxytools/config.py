"""Strict loading and validation for CLI values and ``proxytools.conf``."""

import argparse
from dataclasses import dataclass
from pathlib import Path
import shlex
from urllib.parse import urlparse

from proxytools.paths import tool_home

MAX_WORKERS = 100


def web_url(value: str) -> str:
    """Accept only absolute HTTP(S) URLs suitable for network health checks."""
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise argparse.ArgumentTypeError("must be an absolute http:// or https:// URL")
    return value


def positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def nonnegative_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def probability(value: str) -> float:
    parsed = nonnegative_float(value)
    if parsed > 1:
        raise argparse.ArgumentTypeError("must be between 0 and 1")
    return parsed


def worker_count(value: str) -> int:
    parsed = positive_int(value)
    if parsed > MAX_WORKERS:
        raise argparse.ArgumentTypeError(f"must be between 1 and {MAX_WORKERS}")
    return parsed


def sample_count(value: str) -> int:
    parsed = positive_int(value)
    if parsed > 5:
        raise argparse.ArgumentTypeError("must be between 1 and 5")
    return parsed


class ConfigError(ValueError):
    """Raised for missing, unknown, duplicate, or invalid config entries."""


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    timeout: float
    workers: int
    samples: int
    max_latency: float
    url: str | None
    refresh_interval: float
    history_size: int
    min_checks: int
    min_success_rate: float
    min_success_streak: int
    min_alive_time: float
    max_jitter: float
    alive_failure_tolerance: int
    degraded_after: float
    retention_time: float
    browser: str


def _optional_url(value):
    return web_url(value) if value else None


def _browser(value):
    if value not in {"auto", "chrome", "firefox"}:
        raise argparse.ArgumentTypeError("must be auto, chrome, or firefox")
    return value


CONFIG_KEYS = {
    "TIMEOUT": ("timeout", positive_float),
    "WORKERS": ("workers", worker_count),
    "SAMPLES": ("samples", sample_count),
    "MAX_LATENCY": ("max_latency", positive_float),
    "URL": ("url", _optional_url),
    "MONITOR_REFRESH_INTERVAL": ("refresh_interval", positive_float),
    "MONITOR_HISTORY_SIZE": ("history_size", positive_int),
    "MONITOR_MIN_CHECKS": ("min_checks", positive_int),
    "MONITOR_MIN_SUCCESS_RATE": ("min_success_rate", probability),
    "MONITOR_MIN_SUCCESS_STREAK": ("min_success_streak", positive_int),
    "MONITOR_MIN_ALIVE_TIME": ("min_alive_time", nonnegative_float),
    "MONITOR_MAX_JITTER": ("max_jitter", nonnegative_float),
    "MONITOR_ALIVE_FAILURE_TOLERANCE": ("alive_failure_tolerance", nonnegative_int),
    "MONITOR_DEGRADED_AFTER": ("degraded_after", nonnegative_float),
    "MONITOR_RETENTION_TIME": ("retention_time", positive_float),
    "MONITOR_BROWSER": ("browser", _browser),
}


def config_path() -> Path:
    return tool_home() / "proxytools.conf"


def load_config(path: Path | None = None) -> RuntimeConfig:
    """Parse the complete flat KEY=value config without executing shell code."""
    path = path or config_path()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ConfigError(f"cannot read {path}: {error}") from error
    raw = {}
    for line_number, line in enumerate(lines, 1):
        lexer = shlex.shlex(line, posix=True)
        lexer.whitespace_split = True
        lexer.commenters = "#"
        tokens = list(lexer)
        if not tokens:
            continue
        if len(tokens) != 1 or "=" not in tokens[0]:
            raise ConfigError(f"{path}:{line_number}: expected KEY=value")
        key, value = tokens[0].split("=", 1)
        if key not in CONFIG_KEYS:
            raise ConfigError(f"{path}:{line_number}: unknown key {key!r}")
        if key in raw:
            raise ConfigError(f"{path}:{line_number}: duplicate key {key!r}")
        raw[key] = (value, line_number)
    missing = CONFIG_KEYS.keys() - raw.keys()
    if missing:
        raise ConfigError(f"{path}: missing key(s): {', '.join(sorted(missing))}")
    values = {}
    for key, (field, converter) in CONFIG_KEYS.items():
        value, line_number = raw[key]
        try:
            values[field] = converter(value)
        except argparse.ArgumentTypeError as error:
            raise ConfigError(f"{path}:{line_number}: invalid {key}: {error}") from error
    if values["min_checks"] > values["history_size"]:
        raise ConfigError("MONITOR_MIN_CHECKS cannot exceed MONITOR_HISTORY_SIZE")
    if values["min_success_streak"] > values["history_size"]:
        raise ConfigError("MONITOR_MIN_SUCCESS_STREAK cannot exceed MONITOR_HISTORY_SIZE")
    return RuntimeConfig(**values)
