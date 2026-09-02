from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


VALID_TESTS = ("svmotion", "vmotion", "boot")
_TEST_ALIASES = {
    "svmotion": "svmotion",
    "storage_vmotion": "svmotion",
    "storage-vmotion": "svmotion",
    "vmotion": "vmotion",
    "boot": "boot",
}


class ConfigError(Exception):
    """Invalid or incomplete configuration."""


@dataclass
class VCenterConfig:
    host: str
    username: str
    password: str
    port: int = 443
    insecure: bool = True


@dataclass
class InfluxConfig:
    host: str
    port: int = 8086
    username: str = ""
    password: str = ""
    database: str = "performance"
    ssl: bool = False


@dataclass
class AppConfig:
    vcenter: VCenterConfig
    influxdb: InfluxConfig
    vm: str
    tests: list[str] = field(default_factory=list)
    vmotion_hosts: list[str] = field(default_factory=list)
    svmotion_datastores: list[str] = field(default_factory=list)
    boot_lookback_hours: int = 24


def _require(value: Any, name: str) -> Any:
    if value is None or value == "":
        raise ConfigError(f"Missing required config value: {name}")
    return value


def _as_bool(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: Any, default: int) -> int:
    if value is None or value == "":
        return default
    return int(value)


def _as_str_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return [str(item) for item in value]


def parse_tests(value: Any) -> list[str]:
    """Normalize a tests list from YAML or PERF_TESTS. Preserves order, drops duplicates."""
    seen: set[str] = set()
    tests: list[str] = []
    for raw in _as_str_list(value):
        key = raw.strip().lower()
        name = _TEST_ALIASES.get(key)
        if name is None:
            raise ConfigError(
                f"Unknown test {raw!r}. Valid values: {', '.join(VALID_TESTS)}"
            )
        if name not in seen:
            seen.add(name)
            tests.append(name)
    return tests


def _env(name: str, fallback: Any = None) -> Any:
    value = os.environ.get(name)
    if value is None or value == "":
        return fallback
    return value


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load YAML config and overlay secrets / overrides from the environment."""
    load_dotenv()

    config_path = Path(path or _env("CONFIG_PATH", "config.yaml"))
    if not config_path.is_file():
        raise ConfigError(
            f"Config file not found: {config_path}. "
            "Copy config.example.yaml to config.yaml and fill in your environment."
        )

    with config_path.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    vcenter_raw = raw.get("vcenter") or {}
    influx_raw = raw.get("influxdb") or {}
    vmotion_raw = raw.get("vmotion") or {}
    svmotion_raw = raw.get("storage_vmotion") or {}
    boot_raw = raw.get("boot") or {}

    vcenter = VCenterConfig(
        host=str(_require(_env("VCENTER_HOST", vcenter_raw.get("host")), "vcenter.host")),
        username=str(
            _require(
                _env("VCENTER_USERNAME", vcenter_raw.get("username")),
                "vcenter.username",
            )
        ),
        password=str(
            _require(
                _env("VCENTER_PASSWORD", vcenter_raw.get("password")),
                "VCENTER_PASSWORD",
            )
        ),
        port=_as_int(_env("VCENTER_PORT", vcenter_raw.get("port")), 443),
        insecure=_as_bool(_env("VCENTER_INSECURE", vcenter_raw.get("insecure")), True),
    )

    influxdb = InfluxConfig(
        host=str(_require(_env("INFLUX_HOST", influx_raw.get("host")), "influxdb.host")),
        port=_as_int(_env("INFLUX_PORT", influx_raw.get("port")), 8086),
        username=str(_env("INFLUX_USERNAME", influx_raw.get("username") or "") or ""),
        password=str(_env("INFLUX_PASSWORD", influx_raw.get("password") or "") or ""),
        database=str(
            _require(
                _env("INFLUX_DATABASE", influx_raw.get("database")),
                "influxdb.database",
            )
        ),
        ssl=_as_bool(_env("INFLUX_SSL", influx_raw.get("ssl")), False),
    )

    return AppConfig(
        vcenter=vcenter,
        influxdb=influxdb,
        vm=str(_require(_env("PERF_VM", raw.get("vm")), "vm")),
        tests=parse_tests(_env("PERF_TESTS", raw.get("tests"))),
        vmotion_hosts=_as_str_list(
            _env("VMOTION_HOSTS", vmotion_raw.get("hosts"))
        ),
        svmotion_datastores=_as_str_list(
            _env("SVMOTION_DATASTORES", svmotion_raw.get("datastores"))
        ),
        boot_lookback_hours=_as_int(
            _env("BOOT_LOOKBACK_HOURS", boot_raw.get("event_lookback_hours")),
            24,
        ),
    )
