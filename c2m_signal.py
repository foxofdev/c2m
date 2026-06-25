#!/usr/bin/env python

"""Signal class for can2mqtt."""

from __future__ import annotations
from datetime import datetime
import logging
from typing import Dict, Tuple, Optional, Union, Iterable, Callable, List, Literal, TYPE_CHECKING
from enum import Enum
from threading import RLock

if TYPE_CHECKING:
    from can import Message as RawMessage
else:
    try:
        from can import Message as RawMessage
    except ImportError:
        RawMessage = None  # type: ignore

C2mSignalValue = Union[int, float]
C2mCanByteorder = Literal[
    "little", # little endian (Intel)
    "big", # big endian (Motorola)
]
C2M_UNIT_PREFIXES_DICT = {
    "G": 1e9,
    "M": 1e6,
    "k": 1e3,
    "m": 1e-3,
    "µ": 1e-6,
    "n": 1e-9,
    "p": 1e-12,
    "f": 1e-15,
} # Prefixes for SI units with their corresponding factor

class C2mSource(Enum):
    """ Represents the source for the C2mSignal, either from CAN or MQTT"""
    CAN = 1
    MQTT = 2


class C2mSignal:
    """
    Unified signal object that supports:
    - numeric values (int/float)
    - named values (C2mNamedSignal)
    - CAN mapping
    - MQTT mapping
    """

    def __init__(
        self,
        signal_name: str,
        device_name: str,
        init_value: C2mSignalValue,
        can_bus: str,
        can_id: int,
        can_msg_offset: int,
        can_bit_length: int,
        can_byteorder: C2mCanByteorder = "little",
        can_scaling: float = 1,
        can_offset: float = 0,
        mqtt_topic: Optional[str] = None,
        mqtt_qos: Optional[int] = None,
        mqtt_retain: Optional[bool] = None,
        is_signed: bool = False,
        unit: Optional[str] = None,
        vtable: Optional[Dict] = None,
        timestamp: datetime = datetime.now(),
        on_update: Optional[Callable[[C2mSignal], None]] = None,
        source: C2mSource = C2mSource.CAN,
    ):
        """C2mSignal constructor

        Args:
            signal_name (str): name of the signal/variable
            device_name (str): name of the device on the CANbus that sends/receives the signal
            init_value (C2mSignalValue): Initial signal value (int or float). Will be used in case of a timeout
            can_bus (str): name of the CAN bus
            can_id (int): message ID
            can_msg_offset (int): offset of the signal in message
            can_bit_length (int): bit length of the signal
            can_byteorder (Optional[C2mCanByteorder], optional): byte order of the signal. Defaults to "little".
            can_scaling (Optional[float], optional): scaling factor of the signal. Defaults to 1.
            can_offset (Optional[float], optional): offset of the signal. Defaults to 0.
            mqtt_topic (Optional[str], optional): MQTT topic of the signal. Defaults to None.
            mqtt_qos (Optional[int], optional): MQTT QoS of the signal. Defaults to None.
            mqtt_retain (Optional[bool], optional): MQTT retain flag of the signal. Defaults to None.
            unit (Optional[str], optional): unit of the signal. Defaults to None.
            vtable (Optional[Dict], optional): value table of the signal. Defaults to None.
            timestamp (Optional[datetime], optional): datetime timestamp. Defaults to None.
            on_update (Optional[Callable[[C2mSignal], None]], optional): callback function to be called when the signal is updated. Defaults to None.
            source (C2mSource, optional): Source of the signal (CAN or MQTT). Defaults to C2mSource.CAN.
        """
        # Names
        self.name = signal_name
        self.device = device_name
        
        # Value
        self._initial_value = init_value # Used for resetting
        self.value = init_value
        self.unit = unit
        self.vtable = vtable
        self.timestamp = timestamp
        
        # Source
        self.source = source
        
        # Required CAN variables
        self.can_bus = can_bus
        self.can_id = can_id
        self.can_msg_offset = can_msg_offset
        
        # CAN extraction parameters
        if can_bit_length > 64:
            raise ValueError(f"Bit length {can_bit_length} too large for this implementation")
        self._can_bit_length = can_bit_length 
        
        # Modifier
        self._can_scaling = can_scaling
        self._can_offset = can_offset
        self._is_signed = is_signed
        
        self._bitmask = (1 << self._can_bit_length) - 1
        self._can_byteorder: C2mCanByteorder = can_byteorder
        self._bit_positions: Optional[List[int]] = None

        if self._can_byteorder == "big":
            self._bit_positions = self._motorola_bit_positions(self.can_msg_offset, self._can_bit_length)
            self._required_length = (max(self._bit_positions) // 8) + 1
            self._byte_offset = min(self._bit_positions) // 8
            self._bit_in_byte = min(self._bit_positions) % 8
            self._bytes_needed = self._required_length - self._byte_offset
        else:
            # Convert bit offset to byte offset and bit position within byte (Intel)
            self._byte_offset = self.can_msg_offset // 8
            self._bit_in_byte = self.can_msg_offset % 8
            self._bytes_needed = (self._can_bit_length + self._bit_in_byte + 7) // 8
            self._required_length = self._byte_offset + self._bytes_needed
            
        # MQTT configuration
        if mqtt_topic is None:
            self.mqtt_topic = f"c2m/{self.device}/{self.name}"
        else:
            self.mqtt_topic = mqtt_topic
            
        if mqtt_qos is None:
            self.mqtt_qos = 0
        else:
            self.mqtt_qos = mqtt_qos
            
        if mqtt_retain is None:
            self.mqtt_retain = False
        else:
            self.mqtt_retain = mqtt_retain
            
        # Callback function
        self._on_update = on_update

    # -------------------------------------------------------
    # Update signal value
    # -------------------------------------------------------
    def reset(self):
        """ Reset the signal value to the initial value."""
        self.value = self._initial_value
        self.timestamp = datetime.fromtimestamp(0)
        if self._on_update:
            self._on_update(self)

    def update(
        self,
        new_value: C2mSignalValue,
        unit: Optional[str] = None,
        timestamp: Optional[datetime] = None,
        trigger_callback: bool = True
    ):
        """ Update the signal value directly.

        Args:
            new_value (C2mSignalValue): the value to apply.
            unit (Optional[str], optional): unit of the value if applicable. Defaults to None.
            timestamp (Optional[datetime], optional): datetime timestamp if applicable. Defaults to datetime.now().
            trigger_cb (bool, optional): Whether to trigger the callback function. Defaults to True.

        Raises:
            TypeError: In case of trying to update the varible with other type
        """
        if isinstance(self.value, int):
            new_value = int(new_value)
        if unit is not None and self.unit is not None and not isinstance(new_value, str):
            new_value = self.convert_unit_value(new_value, unit, self.unit)
        self.value = new_value
        self.timestamp = timestamp or datetime.now()
        
        if self._on_update and trigger_callback:
            self._on_update(self)
            
    def from_can(
        self,
        new_value: C2mSignalValue,
        timestamp: Optional[datetime] = None,
        trigger_callback: bool = True
    ):
        """ Update the signal value from raw value received over can. 
        Applies scaling and offset to convert raw CAN value to signal value.

        Args:
            new_value (C2mSignalValue): Raw CAN value.
            timestamp (Optional[datetime], optional): datetime timestamp if applicable. Defaults to datetime.now().
            trigger_callback (bool, optional): Whether to trigger the callback function. Defaults to True.

        Raises:
            TypeError: In case of trying to update the variable with other type
        """
        # Decode: signal_value = raw_value * scaling + offset
        return self.update(
            self._decode_can_value(new_value), 
            timestamp=timestamp, 
            trigger_callback=trigger_callback
        )
    
    def from_can_msg(
        self,
        msg: RawMessage,
        timestamp: Optional[datetime] = None,
        trigger_callback: bool = True
    ):
        """
        Extract value from a CAN message and update the signal.
        Uses the signal's CAN configuration (bus, ID, offset, byteorder) to extract the value.
        
        Args:
            msg: CAN message from python-can
            bit_length: Optional bit length override (uses self._can_bit_length if None)
            timestamp: Optional timestamp (defaults to datetime.now())
            trigger_callback: Whether to trigger the update callback (default: True)
            
        Raises:
            ValueError: If the CAN message doesn't match this signal's bus/ID, or extraction fails
        """
        if RawMessage is None:
            raise ImportError("python-can is required for CAN message processing")
        
        # Verify this message matches our signal
        can_bus = str(msg.channel).lower() if hasattr(msg, "channel") else "unknown"
        if can_bus != self.can_bus.lower():
            raise ValueError(f"CAN bus mismatch: expected {self.can_bus}, got {can_bus}")
        if msg.arbitration_id != self.can_id:
            raise ValueError(f"CAN ID mismatch: expected {self.can_id}, got {msg.arbitration_id}")
        
        # Extract value from CAN message using signal's offset and byteorder
        raw_value = self._extract_value_from_can_message(msg)
        
        # Update signal with extracted value (applies scaling/offset)
        self.from_can(raw_value, timestamp, trigger_callback)
        
    
    def _extract_value_from_can_message(self, msg: RawMessage) -> C2mSignalValue:
        """
        Extract a signed integer value from CAN message data.
        Uses pre-calculated byte offset, bit position, and bytes needed from __init__.
        Supports both little-endian (Intel) and big-endian (Motorola) byte order.
        
        Args:
            msg: CAN message from python-can
            
        Returns:
            Extracted integer value (raw CAN value, before scaling/offset)
            
        Raises:
            ValueError: If parameters are invalid or message is too short
        """
        # Use pre-calculated values from __init__ instead of recalculating
        # Ensure we have enough data
        if len(msg.data) < self._required_length:
            raise ValueError(
                f"CAN message too short: need {self._required_length} bytes, got {len(msg.data)}"
            )
        
        # Extract bytes containing the value
        data_bytes = msg.data[self._byte_offset : self._byte_offset + self._bytes_needed]
        
        if self._can_byteorder == "big":
            if self._bit_positions is None:
                raise ValueError("Motorola bit positions are not initialized")
            value = 0
            for pos in self._bit_positions:
                byte_index = pos // 8
                bit_index = pos % 8
                bit_val = (msg.data[byte_index] >> bit_index) & 0x1
                value = (value << 1) | bit_val
            if self._is_signed and self._can_bit_length < 64:
                sign_bit = 1 << (self._can_bit_length - 1)
                if value & sign_bit:
                    sign_mask = (1 << self._can_bit_length) - 1
                    value = value | (~sign_mask)
            return value
        else:
            # Intel (little-endian) byte order - original implementation
            # Case 1: Bit-aligned extraction
            if self._bit_in_byte == 0 and self._can_bit_length % 8 == 0:
                # Aligned to byte boundary - simple case
                extract_bytes = self._can_bit_length // 8
                return int.from_bytes(
                    data_bytes[:extract_bytes], 
                    byteorder="little", 
                    signed=self._is_signed
                )
            
            # Case 2: Bit-unaligned extraction
            # Convert to integer for bit manipulation
            combined = int.from_bytes(
                data_bytes, 
                byteorder="little", 
                signed=False  # Don't sign here, handle after masking
            )
            
            # Shift right by bit_in_byte
            value = combined >> self._bit_in_byte
            
            # Mask to bit_length bits (use pre-calculated bitmask if available)
            if self._can_bit_length < 64:
                value = value & self._bitmask
            
            # Handle sign extension for signed values
            if self._is_signed and self._can_bit_length < 64:
                # Check if MSB is set (sign bit)
                sign_bit = 1 << (self._can_bit_length - 1)
                if value & sign_bit:
                    # Sign extend
                    sign_mask = (1 << self._can_bit_length) - 1
                    value = value | (~sign_mask)
            
            return value

    def set_update_callback(self, callback: Optional[Callable[[C2mSignal], None]]):
        """Set a callback that is called whenever update() is called."""
        self._on_update = callback

    # -------------------------------------------------------
    # Unit conversion helpers
    # -------------------------------------------------------
    @staticmethod
    def _parse_unit_prefix(unit: str) -> Tuple[Optional[str], str]:
        """
        Parse a unit string to extract the prefix and base unit.
        
        Args:
            unit: Unit string (e.g., "kV", "mV", "V", "µA")
            
        Returns:
            Tuple of (prefix, base_unit). Prefix is None if no recognized prefix found.
            Example: ("k", "V") for "kV", (None, "V") for "V"
        """
        if not unit:
            return None, ""
        
        for prefix in C2M_UNIT_PREFIXES_DICT:
            if unit.startswith(prefix):
                base_unit = unit[len(prefix):]
                return prefix, base_unit
        
        # No recognized prefix found
        return None, unit
    
    @staticmethod
    def convert_unit_value(value: float, from_unit: str, to_unit: str) -> float:
        """
        Convert a value from one unit to another, handling SI unit prefixes.
        
        Args:
            value: The value to convert
            from_unit: Source unit (e.g., "mV", "kV")
            to_unit: Target unit (e.g., "V", "µV")
            
        Returns:
            Converted value
            
        Raises:
            ValueError: If units are incompatible (different base units)
            
        Example:
            convert_unit_value(1000, "mV", "V") -> 1.0
            convert_unit_value(1, "V", "mV") -> 1000.0
            convert_unit_value(1, "kV", "V") -> 1000.0
        """
        if len(from_unit) == 0 or len(to_unit) == 0:
            logging.getLogger(__name__).warning("Invalid units: from_unit='%s', to_unit='%s'", from_unit, to_unit)
            return value
        
        # Parse both units
        from_prefix, from_base = C2mSignal._parse_unit_prefix(from_unit)
        to_prefix, to_base = C2mSignal._parse_unit_prefix(to_unit)
        
        # Check if base units match
        if from_base.lower() != to_base.lower():
            raise ValueError(
                f"Incompatible units: cannot convert from '{from_unit}' (base: '{from_base}') "
                f"to '{to_unit}' (base: '{to_base}')"
            )
        
        # Get prefix factors
        from_factor = C2M_UNIT_PREFIXES_DICT.get(from_prefix, 1.0) if from_prefix else 1.0
        to_factor = C2M_UNIT_PREFIXES_DICT.get(to_prefix, 1.0) if to_prefix else 1.0
        
        # Convert: value_in_base = value * from_factor
        #          value_in_target = value_in_base / to_factor
        value_in_base = value * from_factor
        converted_value = value_in_base / to_factor

        logging.getLogger(__name__).debug("Converted value %.3f%s to %.3f%s", value, from_unit, converted_value, to_unit)
        
        return converted_value
    
    def convert_to_signal_unit(self, value: float, source_unit: Optional[str]) -> float:
        """
        Convert a value to this signal's unit, if units are specified and compatible.
        
        Args:
            value: The value to convert
            source_unit: Source unit (e.g., "mV"). If None, no conversion is performed.
            
        Returns:
            Converted value in this signal's unit, or original value if no conversion needed.
            
        Raises:
            ValueError: If units are incompatible
        """
        # If no units specified, no conversion needed
        if not self.unit or not source_unit:
            return value
        
        # If units match exactly, no conversion needed
        if self.unit == source_unit:
            return value
        
        # Try to convert
        try:
            return self.convert_unit_value(value, source_unit, self.unit)
        except ValueError:
            # Units might be incompatible, but check if they have the same base
            # with different prefixes
            _, from_base = self._parse_unit_prefix(source_unit)
            _, to_base = self._parse_unit_prefix(self.unit)
            
            if from_base.lower() == to_base.lower():
                # Same base unit, just different prefixes - conversion should have worked
                # Re-raise the original error
                raise
            else:
                # Different base units - can't convert, return original value
                # (or raise if you want strict checking)
                return value

    # -------------------------------------------------------
    # Helpers
    # -------------------------------------------------------
    @staticmethod
    def _motorola_bit_positions(start_bit: int, length: int) -> List[int]:
        positions: List[int] = []
        pos = start_bit
        for _ in range(length):
            positions.append(pos)
            if pos % 8 == 0:
                pos += 15
            else:
                pos -= 1
        return positions

    def get_value_name(self) -> Optional[str]:
        """Get the value name from the vtable."""
        if self.vtable is None:
            return None
        return self.vtable.get(self.value)
    
    def _encode_can_value(self, value: C2mSignalValue) -> int:
        """
        Encode signal value to raw CAN value.
        Applies reverse scaling and offset: raw = (signal - offset) / scaling
        """
        raw = (value - self._can_offset) / self._can_scaling
        if raw >= 0:
            raw_int = int(raw + 0.5)
        else:
            raw_int = int(raw - 0.5)
        return raw_int & self._bitmask
    
    def _decode_can_value(self, value: C2mSignalValue) -> int:
        """
        Decode raw CAN value to signal value.
        Applies scaling and offset: signal = raw * scaling + offset
        """
        return int(value * self._can_scaling + self._can_offset)
    
    def add_to_payload(self, message_data: List[int]) -> None:
        """
        Encode the signal's current value into the provided message data list.
        This is the reverse of _extract_value_from_can_message.
        Supports both little-endian (Intel) and big-endian (Motorola) byte order.
        
        The method modifies the provided list in-place by placing the encoded signal value
        at the correct byte offset and bit position within the CAN message data.
        
        Args:
            message_data: List of integers (0-255) representing CAN message data.
                         This list will be modified in-place. Must be large enough to
                         accommodate the signal at its offset position.
        
        Raises:
            ValueError: If the signal value cannot be encoded (e.g., string without vtable)
                       or if message_data is too short
            TypeError: If the value type is not supported
            IndexError: If message_data is too short for the signal's offset and length
        """
        # Validate message_data is large enough
        if len(message_data) < self._required_length:
            raise IndexError(
                f"Message data too short: need {self._required_length} bytes, got {len(message_data)} "
                f"for signal {self.name} at offset {self.can_msg_offset}"
            )
        
        # Apply reverse scaling, offset and masking to get raw CAN value
        raw_can_value = self._encode_can_value(self.value)
        
        if self._can_byteorder == "big":
            if self._bit_positions is None:
                raise ValueError("Motorola bit positions are not initialized")
            for pos in self._bit_positions:
                byte_index = pos // 8
                bit_index = pos % 8
                message_data[byte_index] &= ~(1 << bit_index)
            for i, pos in enumerate(self._bit_positions):
                bit_val = (raw_can_value >> (self._can_bit_length - 1 - i)) & 0x1
                if bit_val:
                    byte_index = pos // 8
                    bit_index = pos % 8
                    message_data[byte_index] |= (1 << bit_index)
        else:
            # Intel (little-endian) byte order - original implementation
            # Combine bytes into a single integer
            payload_int = 0
            for i, byte in enumerate(message_data):
                # Shift each byte into its correct position
                payload_int |= (byte << (i * 8))
            
            # Clear the bits where our signal will go
            clear_mask = self._bitmask << self.can_msg_offset
            payload_int &= ~clear_mask
          
            # Shift the raw CAN value to the correct position
            payload_int |= (raw_can_value << self.can_msg_offset)
            
            # Extract bytes back
            for i, byte in enumerate(message_data):
                message_data[i] = (payload_int >> (i * 8)) & 0xFF
        
    
    def __repr__(self):
        ret = f"C2mSignal(name={self.name!r}, value={self.value!r}, "
        if self.unit is not None:
            ret += f"unit={self.unit}, "
        if self.vtable is not None and self.vtable.get(self.value) is not None:
            ret += f"value_name={self.vtable.get(self.value)}"
        return ret


class C2mSignalRegistry:
    """
    Registry for C2mSignal instances with lookup by:
      - signal name
      - CAN key (can_bus, can_id, can_msg_offset)
      - MQTT topic
    """

    def __init__(self):
        self._by_name: Dict[str, C2mSignal] = {}
        # Nested dict structure: _by_can[can_bus][can_id][can_msg_offset] = C2mSignal
        self._by_can: Dict[str, Dict[int, Dict[int, C2mSignal]]] = {}
        self._by_topic: Dict[str, C2mSignal] = {}
        # Index by source for fast lookup
        self._by_source: Dict[C2mSource, List[C2mSignal]] = {
            C2mSource.CAN: [],
            C2mSource.MQTT: [],
        }
        self._lock = RLock()

    # -----------------------------
    # Register / Unregister
    # -----------------------------
    def register(self, signal: C2mSignal | Iterable[C2mSignal]) -> None:
        """Register or update one or multiple signals in all applicable indexes."""
        if isinstance(signal, Iterable):
            return self._register_many(signal)
        self._register_many([signal])    

    def unregister(self, signal: C2mSignal | Iterable[C2mSignal]) -> None:
        """Remove one or multiple  signals from all indexes."""
        if isinstance(signal, Iterable):
            return self._unregister_many(signal)
        self._unregister_many([signal])  
        
    def _register_many(self, signals:  Iterable[C2mSignal]) -> None:
        """Register or update multiple signals in one shot."""
        with self._lock:
            for signal in signals:
                # index by name
                self._by_name[signal.name] = signal

                # CAN index - nested dict structure
                can_bus = self.normalize_bus_name(signal.can_bus)
                can_id = signal.can_id
                can_offset = signal.can_msg_offset
                
                if can_bus not in self._by_can:
                    self._by_can[can_bus] = {}
                if can_id not in self._by_can[can_bus]:
                    self._by_can[can_bus][can_id] = {}
                self._by_can[can_bus][can_id][can_offset] = signal

                # MQTT index
                if signal.mqtt_topic:
                    topic_key = self.normalize_topic_name(signal.mqtt_topic)
                    self._by_topic[topic_key] = signal
                
                # Source index - add to appropriate source list if not already there
                if signal not in self._by_source[signal.source]:
                    self._by_source[signal.source].append(signal)
    
    def _unregister_many(self, signals:  Iterable[C2mSignal]) -> None:
        with self._lock:
            for signal in signals:
                self._by_name.pop(signal.name, None)

                # CAN index - nested dict structure
                can_bus = self.normalize_bus_name(signal.can_bus)
                can_id = signal.can_id
                can_offset = signal.can_msg_offset
                
                if can_bus in self._by_can:
                    if can_id in self._by_can[can_bus]:
                        self._by_can[can_bus][can_id].pop(can_offset, None)
                        # Clean up empty dicts
                        if not self._by_can[can_bus][can_id]:
                            del self._by_can[can_bus][can_id]
                    if not self._by_can[can_bus]:
                        del self._by_can[can_bus]

                if signal.mqtt_topic:
                    topic_key = self.normalize_topic_name(signal.mqtt_topic)
                    self._by_topic.pop(topic_key, None)
                
                # Source index - remove from source list
                if signal in self._by_source[signal.source]:
                    self._by_source[signal.source].remove(signal)

    # -----------------------------
    # Lookups
    # -----------------------------
    def get_by_name(self, name: str) -> Optional[C2mSignal]:
        """Get a signal by name."""
        with self._lock:
            return self._by_name.get(name)

    def get_by_can(self, can_bus: str, can_id: int, can_offset: int) -> Optional[C2mSignal]:
        """Get a signal by CAN bus, ID and offset."""
        can_bus_norm = self.normalize_bus_name(can_bus)
        with self._lock:
            if can_bus_norm in self._by_can:
                if can_id in self._by_can[can_bus_norm]:
                    return self._by_can[can_bus_norm][can_id].get(can_offset)
            return None
    
    def get_by_can_msg(self, can_bus: str, can_id: int) -> List[C2mSignal]:
        """
        Get all signals for a specific CAN bus and message ID.
        Useful for processing CAN messages where you know bus and ID but need to check all offsets.
        
        Args:
            can_bus: CAN bus name
            can_id: CAN message ID
            
        Returns:
            List of C2mSignal objects matching the bus and ID
        """
        can_bus_norm = self.normalize_bus_name(can_bus)
        with self._lock:
            if can_bus_norm in self._by_can:
                if can_id in self._by_can[can_bus_norm]:
                    return list(self._by_can[can_bus_norm][can_id].values())
            return []

    def get_by_topic(self, mqtt_topic: str) -> Optional[C2mSignal]:
        """Get a signal by MQTT topic."""
        key = self.normalize_topic_name(mqtt_topic)
        with self._lock:
            return self._by_topic.get(key)
        
    def get_all_signals(self) -> List[C2mSignal]:
        """Get all signals from the registry."""
        with self._lock:
            return list(self._by_name.values())
    
    def get_all_can_signals(self) -> List[C2mSignal]:
        """
        Get all signals that come from CAN.
        Fast lookup using source index.
        
        Returns:
            List of C2mSignal objects with source=C2mSource.CAN
        """
        with self._lock:
            return list(self._by_source[C2mSource.CAN])
    
    def get_all_mqtt_signals(self) -> List[C2mSignal]:
        """
        Get all signals that come from MQTT.
        Fast lookup using source index.
        
        Returns:
            List of C2mSignal objects with source=C2mSource.MQTT
        """
        with self._lock:
            return list(self._by_source[C2mSource.MQTT])
        
    # -----------------------------
    # Updaters
    # -----------------------------   
    def update_by_name(
        self, 
        name: str, 
        new_value: C2mSignalValue, 
        unit: Optional[str] = None,
        timestamp: Optional[datetime] = None
    ) -> None:
        """Update a signal by name."""
        with self._lock:
            temp = self._by_name.get(name)
            if temp is not None:
                temp.update(new_value, unit=unit, timestamp=timestamp)
                
    def update_by_can(
        self, 
        can_bus: str,
        can_id: int,
        can_offset: int,
        new_value: C2mSignalValue, 
        timestamp: Optional[datetime] = None
    ) -> None:
        """Update a signal by CAN bus, ID and offset."""
        can_bus_norm = self.normalize_bus_name(can_bus)
        with self._lock:
            if can_bus_norm in self._by_can:
                if can_id in self._by_can[can_bus_norm]:
                    temp = self._by_can[can_bus_norm][can_id].get(can_offset)
                    if temp is not None:
                        temp.update(new_value, timestamp=timestamp) # it is assumed that the value from the CAN message is already in the correct unit
                
    def update_by_topic(
        self, 
        mqtt_topic: str,
        new_value: C2mSignalValue, 
        unit: Optional[str] = None,
        timestamp: Optional[datetime] = None
    ) -> Optional[C2mSignal]:
        """Update a signal by MQTT topic."""
        key = self.normalize_topic_name(mqtt_topic)
        with self._lock:
            temp = self._by_topic.get(key)
            if temp is not None:
                temp.update(new_value, unit=unit, timestamp=timestamp)


    # -----------------------------
    # Helpers
    # -----------------------------
    @staticmethod
    def normalize_bus_name(can_bus: str) -> str:
        """Normalize CAN bus name to lowercase."""
        return can_bus.lower()

    @staticmethod
    def normalize_topic_name(topic: str) -> str:
        """Normalize MQTT topic name. As these are case-sensitive, we only trim whitespace."""
        return topic.strip()

    # -----------------------------
    # Utility
    # -----------------------------
    def __len__(self) -> int:
        with self._lock:
            return len(self._by_name)

    def clear(self) -> None:
        """Clear all signals from the registry."""
        with self._lock:
            self._by_name.clear()
            self._by_can.clear()
            self._by_topic.clear()
            self._by_source[C2mSource.CAN].clear()
            self._by_source[C2mSource.MQTT].clear()
