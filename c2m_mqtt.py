#!/usr/bin/env python

"""MQTT bridging for can2mqtt."""

import json
import logging
from datetime import datetime, timedelta
from typing import Optional, Tuple

import paho.mqtt.client as mqtt

from c2m_signal import (
    C2mSignal,
    C2mSignalValue,
    C2mSignalRegistry,
    C2mSource,
)

# Fallbacks used when no explicit value is provided to the bridge.
C2M_CAN_MESSAGE_RX_TIMEOUT_DEFAULT_S = 5.0
C2M_CHECK_INCOMING_SIGNAL_NAME = False
C2M_SIGNAL_NAME_IN_PAYLOAD = False
C2M_MQTT_TX_IS_CYCLIC = True


class C2mMqttBridge:
    """
    Bridges MQTT messages <-> SignalRegistry (C2mSignal objects).

    - subscribes to all mqtt_topic in the registry
    - on incoming messages, updates the corresponding C2mSignal
      (without automatic publish to avoid loops)
    - attaches callbacks to signals to automatically publish updates
      from your application logic (CAN, internal controllers, etc.)
    """

    def __init__(
        self,
        client: mqtt.Client,
        registry: C2mSignalRegistry,
        default_qos: int = 0,
        default_retain: bool = False,
        signal_rx_timeout_s: float = C2M_CAN_MESSAGE_RX_TIMEOUT_DEFAULT_S,
        check_incoming_signal_name: bool = C2M_CHECK_INCOMING_SIGNAL_NAME,
        signal_name_in_payload: bool = C2M_SIGNAL_NAME_IN_PAYLOAD,
        tx_is_cyclic: bool = C2M_MQTT_TX_IS_CYCLIC,
    ):
        self.client = client
        self.registry = registry
        self.default_qos = default_qos
        self.default_retain = default_retain
        self.signal_rx_timeout_s = signal_rx_timeout_s
        self.check_incoming_signal_name = check_incoming_signal_name
        self.signal_name_in_payload = signal_name_in_payload
        self.tx_is_cyclic = tx_is_cyclic
        self._logger = logging.getLogger(__name__ + '.' + self.__class__.__name__)

        # Track if unified callback is set
        self._unified_callback_set: bool = False

    # -------------------------------------------------
    # Setup: attach callbacks to all existing signals
    # -------------------------------------------------
    def attach_all_signals(self):
        """
        Attach update callbacks and subscribe to MQTT topics
        for all signals currently in the registry.
        Call this after your initial registry population.
        """
        for sig in self.registry.get_all_signals():
            self.attach_signal(sig)

    def attach_signal(self, signal: C2mSignal):
        """
        Attach a single signal at runtime (e.g. after registering a new one).
        """
        # If signal comes from CAN, we only need to forwad it to MQTT on update
        if signal.source == C2mSource.CAN:
            signal.set_update_callback(self._on_signal_update)
            return
        
        # At this point we can assume the signal comes from MQTT, so we need to 
        # subscribe to the topic and set up a unified callback, if not already set
        topic = signal.mqtt_topic
        if topic is None:
            self._logger.warning("Signal %s has no MQTT topic, not subscribing", signal.name)
            return
        
        qos = signal.mqtt_qos if signal.mqtt_qos is not None else self.default_qos
        self._logger.info("Subscribing to %s qos=%s", topic, qos)
        self.client.subscribe(topic, qos=qos)
        
        # Set unified callback if not already set
        if not self._unified_callback_set:
            self.client.on_message = self._on_message
            self._unified_callback_set = True
            self._logger.info("Unified message callback set for all topics")

    # -------------------------------------------------
    # MQTT callbacks
    # -------------------------------------------------
    def _on_message(self, client, userdata, msg):
        """
        Unified message callback for all MQTT topics.
        Looks up the signal by topic and updates it.
        """
        self._logger.info("MQTT RX topic=%s payload=%s", msg.topic, msg.payload.decode("utf-8", errors="replace"))
        rs = self._signal_from_json(msg.payload)
        if rs is None:
            return
        
        sig = self.registry.get_by_topic(msg.topic)
        if sig is None:
            self._logger.warning("No signal found for topic %s, ignoring message", msg.topic)
            return

        # Validate signal name if enabled
        if self.check_incoming_signal_name and \
            (rs[0] is None or sig.name != rs[0]):
            self._logger.warning(
                "Signal name mismatch on topic %s: expected '%s', got '%s', ignoring.", msg.topic, sig.name, rs[0]
            )
            return

        # Update the signal from MQTT, but DON'T trigger callback to avoid loop 
        # (Legacy, we do not add MQTT callbacks to signals originating from MQTT).
        sig.update(new_value=rs[1], timestamp=rs[2], unit=rs[3], trigger_callback=False)
        self._logger.debug(
            "Updated signal %s from topic %s to %r", sig.name, msg.topic, sig.value
        )

    # -------------------------------------------------
    # Handle local updates -> auto publish
    # -------------------------------------------------
    def _on_signal_update(self, signal: C2mSignal):
        """
        Callback called whenever a signal.update(...) is called with trigger_callback=True.
        Only publishes signals that come from CAN (to avoid publishing MQTT-originated updates).
        """
        if not self.tx_is_cyclic:
            if signal.source == C2mSource.CAN:
                self.publish_signal(signal)

    # -------------------------------------------------
    # Payload parsing
    # -------------------------------------------------
    def _signal_from_json(
        self, payload: bytes
    ) -> Optional[Tuple[str, C2mSignalValue, datetime, Optional[str], Optional[str]]]:
        """Own method to decypher our standard JSON payload

        Args:
            payload (bytes): JSON payload directly from MQTT client

        Raises:
            ValueError: In case of missing mandatory fields name, value and timestamp

        Returns:
            A tuple of name, value, timestamt, unit, value_name if JSON was correct, None in case of error
        """
        text = payload.decode("utf-8").strip()

        # 1) check if JSON
        if not text.startswith("{"):
            self._logger.warning("Payload is not a valid JSON: %s", text)
            return None

        try:
            data = json.loads(text)

            # "name" is optional for Everest payloads; if missing, we use the signal name from the topic.
            if self.check_incoming_signal_name and "name" not in data:
                raise ValueError(
                    f'Mandatory JSON field "name" missing: {text}'
                )
            if not "value" in data:
                raise ValueError(
                    f'Mandatory JSON field "value" missing: {text}'
                )
            if not "timestamp" in data:
                raise ValueError(
                    f'Mandatory JSON field "timestamp" missing: {text}'
                )

            # Timestamp is in unix time format and we need to convert it to datetime
            # Given that the timestamp may be in milliseconds
            ts = data.get("timestamp")
            if ts > (2 ** 32 - 1):
                ts = ts / 1000.0
                
            self._logger.debug("Parsed JSON payload: %s", data)
            
            return (
                data.get("name", None),
                data.get("value"),
                datetime.fromtimestamp(ts),
                data.get(
                    "unit", None
                ),  # Unit is optional and frankly only used for debugging purposes
                data.get(
                    "value_name", None
                ),  # Value name is optional and frankly only used for debugging purposes
            )
        except Exception as e:
            self._logger.error("Error parsing JSON payload: %s", e, exc_info=True)
            return None

    def _signal_to_json(self, signal: C2mSignal) -> str:
        ret = {
            "value": signal.value,
            "timestamp": int(
                signal.timestamp.timestamp()
            ),  # Timestamp has to be in unix time format
        }
        
        if self.signal_name_in_payload:
            ret["name"] = signal.name

        # Optional unit
        if signal.unit is not None:
            ret["unit"] = signal.unit

        # Optional value name
        vn = signal.get_value_name()
        if vn is not None:
            ret["value_name"] = vn

        return json.dumps(ret)

    # -------------------------------------------------
    # Publishing helpers
    # -------------------------------------------------
    def publish_signal(self, signal: C2mSignal):
        """Publish a single signal's value via MQTT using per-signal QoS/retain."""
        if signal.timestamp < datetime.now() - timedelta(seconds=self.signal_rx_timeout_s):
            self._logger.debug(
                "Signal %s has timed out, not publishing.", signal.name
            )
            return
        
        topic = signal.mqtt_topic
        if not topic:
            self._logger.warning(
                "Signal %s has no MQTT topic, not publishing.", signal.name
            )
            return

        qos = signal.mqtt_qos if signal.mqtt_qos is not None else self.default_qos
        retain = (
            signal.mqtt_retain
            if signal.mqtt_retain is not None
            else self.default_retain
        )

        payload = self._signal_to_json(signal)
        self._logger.debug(
            "Publishing %s to %s: %s (qos=%s, retain=%s)", signal.name, topic, payload, qos, retain
        )
        self.client.publish(topic, payload=payload, qos=qos, retain=retain)

    def publish_all(self):
        """Publish all signals currently in the registry."""
        for sig in self.registry.get_all_can_signals():
            self.publish_signal(sig)

    # -------------------------------------------------
    # Cleanup / Deinitialization
    # -------------------------------------------------
    def deinitialize(self):
        """
        Remove all attached callbacks from the MQTT client and clear signal registries.
        Call this when shutting down the bridge to properly clean up resources.
        """
        # Remove unified message callback
        if self._unified_callback_set:
            try:
                self.client.on_message = None
                self._unified_callback_set = False
                self._logger.info("Removed unified message callback")
            except Exception as e:
                self._logger.error(
                    "Error removing unified message callback: %s", e, exc_info=True
                )

        # Clear update callbacks from all signals
        for sig in self.registry.get_all_signals():
            sig.set_update_callback(None)

        # Clear the registry
        self.registry.clear()

        self._logger.info(
            "Deinitialized: removed all callbacks and cleared registry"
        )

    def __del__(self):
        """Destructor: attempt cleanup if deinitialize wasn't called explicitly."""
        try:
            if hasattr(self, "_unified_callback_set") and self._unified_callback_set:
                if hasattr(self, "client") and self.client:
                    self.client.on_message = None
        except:
            pass  # Ignore errors during destruction
