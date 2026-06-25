#!/usr/bin/env python

"""CAN bus bridging for can2mqtt."""

import logging
from threading import RLock
from typing import List, Optional, Union, Dict, Tuple, Set

from can import Message as RawMessage, BusABC, Notifier
from can.typechecking import Channel as RawMessageChannel

from c2m_signal import C2mSignalRegistry, C2mSource, C2mSignal

# Fallback used when no explicit timeout is provided to the bridge.
C2M_CAN_MESSAGE_TX_TIMEOUT_DEFAULT_S = 0.1


class C2mCanBridge:
    """
    Bridges CAN messages <-> SignalRegistry (C2mSignal objects).

    - listens to CAN buses via Notifier or Bus objects
    - on incoming messages, updates the corresponding C2mSignal
    - sets signal source to CAN when updating from CAN messages
    """

    def __init__(
        self,
        registry: C2mSignalRegistry,
        notifiers: Optional[Union[Notifier, List[Notifier]]] = None,
        message_tx_timeout_s: float = C2M_CAN_MESSAGE_TX_TIMEOUT_DEFAULT_S,
    ):
        self.registry = registry
        self.message_tx_timeout_s = message_tx_timeout_s
        self._buses: Dict[str, BusABC] = {}
        self._notifiers: Set[Notifier] = set()
        self._lock = RLock()
        self._logger = logging.getLogger(__name__ + '.' + self.__class__.__name__)
        
        if notifiers is not None:
            self.initialize(notifiers)

    def initialize(self, notifiers: Union[Notifier, List[Notifier]]) -> None:
        """
        Initialize the bridge: setup bus map and listeners.
        """
        with self._lock:
            new_notifiers = notifiers if isinstance(notifiers, list) else [notifiers]
            for n in new_notifiers:
                # Prevent duplicate callback registration
                if self.can_message_callback not in n.listeners:
                    n.listeners.append(self.can_message_callback)
                
                # Add notifier to set for tracking (even if bus is missing, we need to track it for cleanup)
                self._notifiers.add(n)
                
                notified_objects = n.bus if isinstance(n.bus, list) else [n.bus]
                for b in notified_objects:
                    if isinstance(b, BusABC):
                        self._buses[self._get_bus_name(b)] = b
                self._logger.info("Initialized CAN bridge for busses: %s", ', '.join(self._buses.keys()))
    # -------------------------------------------------
    # CAN callback
    # -------------------------------------------------
    def can_message_callback(self, msg: RawMessage) -> None:
        """
        Callback for incoming CAN messages.
        Compatible with python-can's MessageRecipient type.
        
        Args:
            msg: CAN message from python-can
        """
        can_bus = self._get_bus_name(msg.channel)
        can_id = msg.arbitration_id

        # Fast lookup: get all signals for this CAN bus and ID
        signals = self.registry.get_by_can_msg(can_bus, can_id)

        if signals:
            for sig in signals:
                if sig.source == C2mSource.MQTT:
                    # If one signal comes from MQTT, all of them do, abort processing
                    break
                try:
                    # Signal extracts and updates itself from CAN message
                    # Uses signal's own can_msg_offset and can_byteorder
                    sig.from_can_msg(msg, trigger_callback=True)
                    self._logger.debug(
                        "Updated signal %s from CAN (bus=%s, id=0x%X, offset=%s) to %r", sig.name, can_bus, can_id, sig.can_msg_offset, sig.value
                    )
                except Exception as e:
                    self._logger.error(
                        "Error processing CAN message for signal %s: %s", sig.name, e, exc_info=True
                    )

    # -------------------------------------------------
    # Helper methods
    # -------------------------------------------------
    def _get_bus_name(self, bus: BusABC | RawMessageChannel | None) -> str:
        """Get normalized bus name from Bus object.
        Returns "unknown" if the bus does for whatever reason not embed a channel.
        """
        if isinstance(bus, BusABC):
            return str(getattr(bus, "channel", "unknown")).lower()
        if isinstance(bus, str | int ):
            return str(bus).lower()
        return "unknown"
    
    # -------------------------------------------------
    # Encoding and sending MQTT signals to CAN
    # -------------------------------------------------
    def send_mqtt_signals_to_can(self) -> None:
        """
        Send all signals with source=C2mSource.MQTT to CAN bus.
        Groups signals by CAN bus and ID, constructs messages using signal encoding,
        and sends them.
        """
        mqtt_signals = self.registry.get_all_mqtt_signals()
        
        if not mqtt_signals:
            return
        
        # Group signals by (can_bus, can_id)
        signal_groups: Dict[Tuple[str, int], List] = {}
        
        for sig in mqtt_signals:
            key = (self.registry.normalize_bus_name(sig.can_bus), sig.can_id)
            if key not in signal_groups:
                signal_groups[key] = []
            signal_groups[key].append(sig)
        
        # Construct and send messages using send_message_by_id
        for (can_bus, can_id), signals in signal_groups.items():
            # Determine message length (DLC) - find the maximum byte needed
            max_byte = 0
            for sig in signals:
                try:
                    byte_offset = getattr(sig, '_byte_offset', 0)
                    bytes_needed = getattr(sig, '_bytes_needed', 0)
                    byte_end = byte_offset + bytes_needed
                    max_byte = max(max_byte, byte_end)
                except Exception as e:
                    self._logger.warning("Error calculating byte offset for signal %s: %s", sig.name, e)
                    continue
            
            # If no valid byte calculation was found, skip this message
            if max_byte == 0:
                self._logger.warning("Could not determine message length for 0x%X on %s, skipping", can_id, can_bus)
                continue
            
            # Cap at 8 bytes for standard CAN
            if max_byte > 8:
                self._logger.warning("Message 0x%X on %s exceeds 8 bytes, truncating", can_id, can_bus)
                max_byte = 8
            
            # Initialize message data with zeros
            message_data = [0] * max_byte
            
            # Encode each signal into the message data
            for sig in signals:
                try:
                    sig.add_to_payload(message_data)
                except Exception as e:
                    self._logger.error("Error encoding signal %s into CAN message: %s", sig.name, e, exc_info=True)
                    continue
            
            # Use send_custom_message to send the message
            if self.send_custom_message(can_bus, can_id, data=message_data):
                self._logger.debug("Sent CAN message (bus=%s, id=0x%X, dlc=%d) with %d signal(s)", can_bus, can_id, max_byte, len(signals))
    
    def _construct_message_from_signals(self, signals: List[C2mSignal]) -> Optional[bytes]:
        """
        Helper method to construct CAN message data from a list of signals.
        
        Args:
            signals: List of C2mSignal objects
            
        Returns:
            Message data as bytes, or None if construction failed
        """
        # Determine message length (DLC) - find the maximum byte needed
        max_byte = 0
        for sig in signals:
            try:
                byte_offset = getattr(sig, '_byte_offset', 0)
                bytes_needed = getattr(sig, '_bytes_needed', 0)
                byte_end = byte_offset + bytes_needed
                max_byte = max(max_byte, byte_end)
            except Exception as e:
                self._logger.warning("Error calculating byte offset for signal %s: %s", sig.name, e)
                continue
        
        # If no valid byte calculation was found, return None
        if max_byte == 0:
            return None
        
        # Cap at 8 bytes for standard CAN
        if max_byte > 8:
            self._logger.warning("Message exceeds 8 bytes, truncating")
            max_byte = 8
        
        # Initialize message data with zeros
        message_data_list = [0] * max_byte
        
        # Encode each signal into the message data
        for sig in signals:
            try:
                sig.add_to_payload(message_data_list)
            except Exception as e:
                self._logger.error("Error encoding signal %s into CAN message: %s", sig.name, e, exc_info=True)
                continue
        
        return bytes(message_data_list)
    
    def _send_can_message(self, can_bus: str, can_id: int, message_data: bytes) -> bool:
        """
        Helper method to send a CAN message.
        
        Args:
            can_bus: CAN bus name (normalized)
            can_id: CAN message ID
            message_data: Message data as bytes
            
        Returns:
            True if message was sent successfully, False otherwise
        """
        # Find the bus object
        bus = self._buses.get(can_bus)
        if bus is None:
            self._logger.warning("No bus found for %s, cannot send message 0x%X", can_bus, can_id)
            return False
        
        # Create and send CAN message
        try:
            msg = RawMessage(
                arbitration_id=can_id,
                data=message_data,
                is_extended_id=False,
                channel=can_bus
            )
            bus.send(msg, timeout=self.message_tx_timeout_s)
            self._logger.debug(
                "Sent CAN message (bus=%s, id=0x%X, dlc=%d, data=%s)",
                can_bus,
                can_id,
                len(message_data),
                message_data.hex(),
            )
            return True
        except (OSError, IOError) as e:
            self._logger.error("Error sending CAN message (bus=%s, id=0x%X): %s", can_bus, can_id, e, exc_info=True)
            # Remove invalid bus from cache
            if hasattr(bus, 'shutdown'):
                try:
                    bus.shutdown()
                except Exception:
                    pass
            self._buses.pop(can_bus, None)
            return False
        except Exception as e:
            self._logger.error("Error sending CAN message (bus=%s, id=0x%X): %s", can_bus, can_id, e, exc_info=True)
            return False
    
    def send_custom_message(self, can_bus: str, can_id: int, data: Union[bytes, List[int], List[C2mSignal]]) -> bool:
        """
        Send a custom CAN message with provided data.
        
        Args:
            can_bus: CAN bus name (e.g., "can0", "can1")
            can_id: CAN message ID
            data: Message data. Can be bytes, list of integers, or list of C2mSignal objects.
                  
        Returns:
            True if message was sent successfully, False otherwise
        """
        # Normalize bus name
        can_bus = self.registry.normalize_bus_name(can_bus)
        
        # Handle different data types
        if isinstance(data, list) and len(data) > 0 and isinstance(data[0], C2mSignal):
            # List of C2mSignal objects - construct message from signals
            signals: List[C2mSignal] = data  # type: ignore
            message_data = self._construct_message_from_signals(signals)
            if message_data is None:
                self._logger.warning("Could not construct message from signals for 0x%X on %s", can_id, can_bus)
                return False
        elif isinstance(data, list):
            # List of integers - convert to bytes
            int_list: List[int] = data  # type: ignore
            message_data = bytes(int_list)
            # Cap at 8 bytes for standard CAN
            if len(message_data) > 8:
                self._logger.warning("Message 0x%X on %s exceeds 8 bytes, truncating", can_id, can_bus)
                message_data = message_data[:8]
        elif isinstance(data, bytes):
            message_data = data
            # Cap at 8 bytes for standard CAN
            if len(message_data) > 8:
                self._logger.warning("Message 0x%X on %s exceeds 8 bytes, truncating", can_id, can_bus)
                message_data = message_data[:8]
        else:
            self._logger.error("Invalid data type for message 0x%X: %s", can_id, type(data))
            return False
        
        return self._send_can_message(can_bus, can_id, message_data)
    
    def send_message_by_id(self, can_bus: str, can_id: int) -> bool:
        """
        Send a CAN message by bus and ID, constructing it from signals in the registry.
        
        Args:
            can_bus: CAN bus name (e.g., "can0", "can1")
            can_id: CAN message ID
                  
        Returns:
            True if message was sent successfully, False otherwise
        """
        # Normalize bus name
        can_bus = self.registry.normalize_bus_name(can_bus)
        
        # Construct message from signals in registry
        signals = self.registry.get_by_can_msg(can_bus, can_id)
        
        if not signals:
            self._logger.warning("No signals found for bus=%s, id=0x%X, cannot construct message", can_bus, can_id)
            return False
        
        # Construct message data from signals
        message_data = self._construct_message_from_signals(signals)
        if message_data is None:
            self._logger.warning("Could not determine message length for 0x%X on %s, skipping", can_id, can_bus)
            return False
        
        return self._send_can_message(can_bus, can_id, message_data)
    
    # -------------------------------------------------
    # Cleanup / Deinitialization
    # -------------------------------------------------
    def deinitialize(self):
        """
        Stop all notifiers and clean up resources.
        Note: This will stop notifiers, which may affect other listeners.
        Use with caution if notifiers are shared.
        """
        with self._lock:
            for notifier in self._notifiers:
                try:
                    # Remove our callback from listeners
                    if notifier.listeners and self.can_message_callback in notifier.listeners:
                        notifier.listeners.remove(self.can_message_callback)
                    
                    # Only stop notifiers we created (check if we're the only listener)
                    # For safety, we'll let the user manage notifier lifecycle
                    # Just remove our callback
                    self._logger.info("Removed callback from Notifier")
                except Exception as e:
                    self._logger.error("Error removing callback from Notifier: %s", e, exc_info=True)

            self._notifiers.clear()
            self._buses.clear()
            self._logger.info("Deinitialized: removed all callbacks")

    def __del__(self):
        """Destructor: attempt cleanup if deinitialize wasn't called explicitly."""
        try:
            # Only attempt cleanup if object is properly initialized
            if hasattr(self, '_notifiers') and hasattr(self, '_lock'):
                self.deinitialize()
        except Exception:
            pass  # Ignore errors during destruction
        