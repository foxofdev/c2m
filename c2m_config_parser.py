#!/usr/bin/env python

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional, Union

import yaml

from c2m_signal import C2mSignal, C2mSignalRegistry, C2mSource

# Default config file, located next to this module.
C2M_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "c2m_config.yaml"


class C2mConfigParser:
    """
    Parser for the YAML can2mqtt configuration (c2m_config.yaml).

    Loads the config file and exposes the global settings (CAN bus, MQTT base
    topic, cycle/timeout values, validation flags, ...) as instance attributes.
    Use `build_registry()` to turn the `signals` section into a populated
    C2mSignalRegistry. Every signal must specify its layout/encoding fields
    (can_byteorder, can_scaling, can_offset, is_signed) explicitly.
    """

    def __init__(
        self,
        config_path: Union[str, Path] = C2M_DEFAULT_CONFIG_PATH,
        parse: bool = True,
    ):
        """
        Args:
            config_path: Path to the YAML config file.
            parse: If True (default), parse the file immediately. If False, call
                   `parse()` manually later.
        """
        self.config_path = Path(config_path)
        self._logger = logging.getLogger(__name__ + "." + self.__class__.__name__)
        self._raw: Dict[str, Any] = {}

        # ---- CAN settings (see `can:` section) ----
        self.can_bus: str = "can0"
        self.can_tx_is_cyclic: bool = True
        self.can_message_tx_cycle_default_s: float = 0.5
        self.can_message_tx_timeout_default_s: float = 0.1
        self.can_message_rx_timeout_default_s: float = 5.0
        self.can_rx_is_cyclic: bool = False
        self.can_message_rx_cycle_default_s: float = 0.5

        # ---- MQTT settings (see `mqtt:` section) ----
        self.mqtt_base_topic: str = "c2m"
        self.mqtt_qos_default: int = 0
        self.mqtt_retain_default: bool = False
        self.mqtt_tx_is_cyclic: bool = True
        self.mqtt_rx_is_cyclic: bool = False
        self.mqtt_signal_tx_cycle_s: float = 0.25
        self.mqtt_signal_timeout_s: float = 5.0

        # ---- Signal validation (part of the `mqtt:` section) ----
        self.check_incoming_signal_name: bool = False
        self.signal_name_in_payload: bool = False

        if parse:
            self.parse()

    # -------------------------------------------------
    # Parsing
    # -------------------------------------------------
    def parse(self) -> "C2mConfigParser":
        """Load the YAML file and populate all internal settings.

        Returns:
            self, to allow chaining (e.g. registry = C2mConfigParser(path).parse().build_registry()).

        Raises:
            FileNotFoundError: If the config file does not exist.
            ValueError: If the file is empty or not a YAML mapping.
        """
        if not self.config_path.is_file():
            raise FileNotFoundError(f"Config file not found: {self.config_path}")

        with self.config_path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        if raw is None:
            raise ValueError(f"Config file is empty: {self.config_path}")
        if not isinstance(raw, dict):
            raise ValueError(
                f"Config root must be a mapping, got {type(raw).__name__}: {self.config_path}"
            )

        self._raw = raw
        self._parse_can(raw.get("can", {}) or {})
        self._parse_mqtt(raw.get("mqtt", {}) or {})

        self._logger.info("Parsed config from %s", self.config_path)
        return self

    def _parse_can(self, can: Dict[str, Any]) -> None:
        self.can_bus = can.get("bus", self.can_bus)
        self.can_tx_is_cyclic = bool(can.get("tx_is_cyclic", self.can_tx_is_cyclic))
        self.can_message_tx_cycle_default_s = float(
            can.get("message_tx_cycle_default_s", self.can_message_tx_cycle_default_s)
        )
        self.can_message_tx_timeout_default_s = float(
            can.get("message_tx_timeout_default_s", self.can_message_tx_timeout_default_s)
        )
        self.can_message_rx_timeout_default_s = float(
            can.get("message_rx_timeout_default_s", self.can_message_rx_timeout_default_s)
        )
        self.can_rx_is_cyclic = bool(can.get("rx_is_cyclic", self.can_rx_is_cyclic))
        self.can_message_rx_cycle_default_s = float(
            can.get("message_rx_cycle_default_s", self.can_message_rx_cycle_default_s)
        )

    def _parse_mqtt(self, mqtt: Dict[str, Any]) -> None:
        self.mqtt_base_topic = mqtt.get("base_topic", self.mqtt_base_topic)
        self.mqtt_qos_default = int(mqtt.get("qos_default", self.mqtt_qos_default))
        self.mqtt_retain_default = bool(mqtt.get("retain_default", self.mqtt_retain_default))
        self.mqtt_tx_is_cyclic = bool(mqtt.get("tx_is_cyclic", self.mqtt_tx_is_cyclic))
        self.mqtt_rx_is_cyclic = bool(mqtt.get("rx_is_cyclic", self.mqtt_rx_is_cyclic))
        self.mqtt_signal_tx_cycle_s = float(
            mqtt.get("signal_tx_cycle_s", self.mqtt_signal_tx_cycle_s)
        )
        self.mqtt_signal_timeout_s = float(
            mqtt.get("signal_timeout_s", self.mqtt_signal_timeout_s)
        )
        self.check_incoming_signal_name = bool(
            mqtt.get("check_incoming_signal_name", self.check_incoming_signal_name)
        )
        self.signal_name_in_payload = bool(
            mqtt.get("signal_name_in_payload", self.signal_name_in_payload)
        )

    # -------------------------------------------------
    # Registry building
    # -------------------------------------------------
    def build_registry(self, registry: Optional[C2mSignalRegistry] = None) -> C2mSignalRegistry:
        """Build (or populate) a C2mSignalRegistry from the `signals` section.

        Args:
            registry: Optional existing registry to register the signals into.
                      If None, a new C2mSignalRegistry is created.

        Returns:
            The populated registry.

        Raises:
            ValueError: If the `signals` section is missing/invalid or a signal
                        is malformed.
        """
        if registry is None:
            registry = C2mSignalRegistry()

        signals_raw = self._raw.get("signals")
        if signals_raw is None:
            raise ValueError(f"No 'signals' section found in config: {self.config_path}")
        if not isinstance(signals_raw, list):
            raise ValueError(
                f"'signals' must be a list, got {type(signals_raw).__name__}: {self.config_path}"
            )

        signals = [self._build_signal(i, entry) for i, entry in enumerate(signals_raw)]
        registry.register(signals)
        self._logger.info("Built %d signals from %s", len(signals), self.config_path)
        return registry

    def _build_signal(self, index: int, entry: Dict[str, Any]) -> C2mSignal:
        if not isinstance(entry, dict):
            raise ValueError(f"Signal #{index} must be a mapping, got {type(entry).__name__}")

        def required(key: str) -> Any:
            if key not in entry:
                name = entry.get("name", f"#{index}")
                raise ValueError(f"Signal {name} is missing required key '{key}'")
            return entry[key]

        name = required("name")

        # Resolve the MQTT topic: stored as a suffix relative to base_topic.
        topic = entry.get("mqtt_topic")
        mqtt_topic = self._resolve_topic(topic) if topic is not None else None

        # Layout/encoding fields must be specified explicitly per signal.
        can_byteorder = required("can_byteorder")
        can_scaling = required("can_scaling")
        can_offset = required("can_offset")
        is_signed = required("is_signed")

        return C2mSignal(
            signal_name=name,
            device_name=required("device"),
            init_value=required("init_value"),
            can_bus=entry.get("can_bus", self.can_bus),
            can_id=required("can_id"),
            can_msg_offset=required("can_msg_offset"),
            can_bit_length=required("can_bit_length"),
            can_byteorder=can_byteorder,
            can_scaling=can_scaling,
            can_offset=can_offset,
            mqtt_topic=mqtt_topic,
            mqtt_qos=entry.get("mqtt_qos", self.mqtt_qos_default),
            mqtt_retain=entry.get("mqtt_retain", self.mqtt_retain_default),
            is_signed=is_signed,
            unit=entry.get("unit"),
            vtable=self._resolve_vtable(entry.get("vtable")),
            source=self._resolve_source(entry.get("source", "CAN")),
        )

    # -------------------------------------------------
    # Helpers
    # -------------------------------------------------
    def _resolve_topic(self, topic: str) -> str:
        """Join a per-signal topic suffix with the configured base topic.

        Absolute topics (already starting with the base topic, or containing the
        base topic as their first segment) are returned unchanged.
        """
        topic = str(topic).strip()
        if not self.mqtt_base_topic:
            return topic
        if topic.startswith(self.mqtt_base_topic + "/") or topic == self.mqtt_base_topic:
            return topic
        return f"{self.mqtt_base_topic}/{topic.lstrip('/')}"

    def _resolve_vtable(self, vtable: Any) -> Optional[Dict[int, str]]:
        """Resolve a signal's value table.

        Supports an inline mapping (the standard DBC-like form) or a string name
        referencing an optional top-level `value_tables` section.
        """
        if vtable is None:
            return None
        if isinstance(vtable, dict):
            return {int(k): str(v) for k, v in vtable.items()}
        if isinstance(vtable, str):
            tables = self._raw.get("value_tables", {}) or {}
            if vtable not in tables:
                raise ValueError(f"Unknown value table reference '{vtable}'")
            return {int(k): str(v) for k, v in tables[vtable].items()}
        raise ValueError(f"Invalid vtable type: {type(vtable).__name__}")

    @staticmethod
    def _resolve_source(source: Any) -> C2mSource:
        if isinstance(source, C2mSource):
            return source
        key = str(source).strip().upper()
        try:
            return C2mSource[key]
        except KeyError as exc:
            valid = ", ".join(s.name for s in C2mSource)
            raise ValueError(f"Invalid source '{source}'. Expected one of: {valid}") from exc

    def __repr__(self) -> str:
        return (
            f"C2mConfigParser(path={str(self.config_path)!r}, "
            f"bus={self.can_bus!r}, base_topic={self.mqtt_base_topic!r}, "
            f"signals={len(self._raw.get('signals', []) or [])})"
        )
