from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .config import InfluxConfig

logger = logging.getLogger(__name__)


class InfluxWriter:
    def __init__(self, cfg: InfluxConfig) -> None:
        self.cfg = cfg
        self.client: Any = None

    def connect(self) -> None:
        from influxdb import InfluxDBClient

        logger.info(
            "Connecting to InfluxDB %s:%s db=%s",
            self.cfg.host,
            self.cfg.port,
            self.cfg.database,
        )
        self.client = InfluxDBClient(
            host=self.cfg.host,
            port=self.cfg.port,
            username=self.cfg.username or None,
            password=self.cfg.password or None,
            database=self.cfg.database,
            ssl=self.cfg.ssl,
            verify_ssl=self.cfg.ssl,
        )

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None

    def __enter__(self) -> InfluxWriter:
        self.connect()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def write(
        self,
        measurement: str,
        tags: dict[str, Any],
        fields: dict[str, float],
        time: datetime | None = None,
    ) -> None:
        if self.client is None:
            raise RuntimeError("InfluxDB client is not connected")
        point = {
            "measurement": measurement,
            "tags": {key: str(value) for key, value in tags.items() if value is not None},
            "fields": {key: float(value) for key, value in fields.items()},
            "time": time or datetime.now(timezone.utc),
        }
        logger.info(
            "Writing %s fields=%s tags=%s",
            measurement,
            point["fields"],
            point["tags"],
        )
        self.client.write_points([point])
