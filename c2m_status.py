#!/usr/bin/env python

"""Service status reporting over MQTT for can2mqtt."""

from __future__ import annotations

import json
import logging
import socket
import time
from datetime import datetime, timezone
from typing import Optional

import paho.mqtt.client as mqtt

_logger = logging.getLogger(__name__)


class C2mStatusReporter:
    """
    Publishes service status (starting/running/stopped) as JSON to an MQTT topic.

    Reporting can be disabled entirely via `enabled=False`, in which case
    `publish()` becomes a no-op while `lwt_payload()` / `topic` stay available.
    """

    @staticmethod
    def utc_iso_now() -> str:
        """Return the current UTC time as an ISO-8601 string ending in 'Z'."""
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def format_iso_to_readable(iso_str: str) -> str:
        """Format ISO datetime string to YYYY-MM-DD HH:MM."""
        return iso_str.replace("T", " ")[:16]

    @staticmethod
    def get_local_ip() -> Optional[str]:
        """Get the primary local IPv4 address (interface used for default route)."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                return s.getsockname()[0]
        except OSError:
            return None

    def __init__(
        self,
        client: mqtt.Client,
        topic: str,
        mode: str,
        can_bus: str,
        started: str,
        enabled: bool = True,
        retain: bool = True,
        qos: int = 0,
    ):
        """
        Args:
            client: Connected (or connecting) paho MQTT client.
            topic: MQTT topic to publish status payloads to.
            mode: Operating mode reported in the payload (e.g. the log level).
            can_bus: CAN bus name reported in the payload.
            started: ISO-8601 start timestamp (see `utc_iso_now()`).
            enabled: If False, `publish()` does nothing.
            retain: MQTT retain flag for status messages.
            qos: MQTT QoS for status messages.
        """
        self._client = client
        self._topic = topic
        self._mode = mode
        self._can_bus = can_bus
        self._started = started
        self._enabled = enabled
        self._retain = retain
        self._qos = qos
        if self._enabled:
            _logger.info(
                "Status reporter initialized (topic=%s, bus=%s, mode=%s, qos=%d, retain=%s)",
                self._topic, self._can_bus, self._mode, self._qos, self._retain,
            )
        else:
            _logger.info("Status reporter initialized but disabled")

    @property
    def topic(self) -> str:
        return self._topic

    @property
    def enabled(self) -> bool:
        return self._enabled

    def build_payload(self, status: str, stopped: Optional[str] = None) -> str:
        """Build JSON status payload. stopped is set when status is 'stopping'/'stopped'."""
        payload = {
            "status": status,
            "mode": self._mode,
            "can_bus": self._can_bus,
            "started": self.format_iso_to_readable(self._started),
            "timestamp": int(time.time()),
        }
        if (ip := self.get_local_ip()) is not None:
            payload["ip"] = ip
        if stopped is not None:
            payload["stopped"] = self.format_iso_to_readable(stopped)
        return json.dumps(payload)

    def lwt_payload(self) -> str:
        """Minimal Last Will and Testament payload for unclean disconnects."""
        return json.dumps(
            {"status": "stopped", "started": self.format_iso_to_readable(self._started)}
        )

    def publish(self, status: str, stopped: Optional[str] = None) -> None:
        """Publish a status payload to the configured topic (no-op if disabled)."""
        if not self._enabled:
            return
        try:
            payload = self.build_payload(status=status, stopped=stopped)
            info = self._client.publish(self._topic, payload, qos=self._qos, retain=self._retain)
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                _logger.warning(
                    "Failed to publish '%s' status to %s (rc=%d)",
                    status, self._topic, info.rc,
                )
            else:
                _logger.debug("Published '%s' status to %s", status, self._topic)
        except Exception as e:
            _logger.error(
                "Error publishing '%s' status to %s: %s",
                status, self._topic, e, exc_info=True,
            )
