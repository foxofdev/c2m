#!/usr/bin/env python

"""
c2m_dbc_to_config.py
=================

Generate a can2mqtt YAML configuration (see ../examples/basic/c2m_config.yaml)
straight from a DBC file.

All CAN layout and naming is taken from the DBC:

    * can_id            <- BO_ frame id
    * can_msg_offset    <- SG_ start bit
    * can_bit_length    <- SG_ length
    * can_byteorder     <- SG_ byte order (@1 little / @0 big)
    * can_scaling       <- SG_ factor
    * can_offset        <- SG_ offset
    * is_signed         <- SG_ sign (@..- signed / @..+ unsigned)
    * unit              <- SG_ unit
    * vtable            <- VAL_ / value table entries (raw values, masked to the
                           signal's bit width so signed -1/-2 become
                           0xFFFFFFFF/0xFFFFFFFE etc.)
    * init_value        <- the value table entry named "Init"/"INIT" if present,
                           otherwise 0
    * device            <- the message transmitter node (BO_ sender)
    * source            <- MQTT for signals transmitted by the c2m node (the
                           node the bridge runs on; these originate from MQTT
                           and are written to CAN), CAN for everything else.
                           The c2m node is selected with --c2m-node.

The global `can:` and `mqtt:` blocks default to the values used in
examples/basic/c2m_config.yaml; override them via CLI flags. Every signal
carries its layout/encoding fields (can_byteorder, can_scaling, can_offset,
is_signed) explicitly -- there is no signal_defaults block.

By default each signal's MQTT topic suffix is ``<nodeName>/<signalName>``
(relative to ``mqtt.base_topic``).

Usage:
    python c2m_dbc_to_config.py path/to/your/file.dbc -o c2m_config.yaml
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

try:
    import cantools
except ImportError as exc:  # pragma: no cover - dependency hint
    raise SystemExit(
        "cantools is required: install it with `pip install cantools` "
        "(see requirements.txt)."
    ) from exc


# =====================================================================
# Global defaults (mirroring examples/basic/c2m_config.yaml)
# =====================================================================
@dataclass
class CanDefaults:
    bus: str = "can0"
    tx_is_cyclic: bool = True
    message_tx_cycle_default_s: float = 0.5
    message_tx_timeout_default_s: float = 0.1
    message_rx_timeout_default_s: float = 5
    rx_is_cyclic: bool = False
    message_rx_cycle_default_s: float = 0.5


@dataclass
class MqttDefaults:
    base_topic: str = "c2m"
    qos_default: int = 0
    retain_default: bool = True
    tx_is_cyclic: bool = True
    rx_is_cyclic: bool = False
    signal_tx_cycle_s: float = 0.25
    signal_timeout_s: float = 5
    check_incoming_signal_name: bool = False
    signal_name_in_payload: bool = False


@dataclass
class DbcParserConfig:
    """Options that control how the DBC is turned into a config."""

    # Node the c2m bridge runs on. Messages this node transmits are sourced
    # from MQTT (the bridge reads them from MQTT and puts them on the bus);
    # everything else is sourced from CAN. Matched case insensitively against
    # the DBC BO_ sender / BU_ node names.
    c2m_node: str = "Gateway"
    # Topic template; available fields: {node}, {signal}, {device}, {message}.
    topic_template: str = "{node}/{signal}"
    can: CanDefaults = field(default_factory=CanDefaults)
    mqtt: MqttDefaults = field(default_factory=MqttDefaults)


# =====================================================================
# Helpers
# =====================================================================
def _clean_node_name(node: str) -> str:
    """Normalize a DBC node name into a device/topic-friendly token.

    "DC_Matrix" -> "dcmatrix", "Precharge_PSU" -> "prechargepsu".
    """
    return node.lower().replace("_", "")


def _mask_to_unsigned(value: int, bit_length: int) -> int:
    """Represent a (possibly signed) raw value as the unsigned bit pattern.

    The C2mSignal value tables key off the raw unsigned value, so signed
    sentinels such as -1/-2 on a 32-bit signal become 0xFFFFFFFF/0xFFFFFFFE.
    """
    if bit_length <= 0:
        return value
    return value & ((1 << bit_length) - 1)


def _hex(value: int) -> str:
    """Format an int like the example config: 0x prefix, uppercase digits."""
    return f"0x{value:X}"


def _num(value: float) -> str:
    """Render a number without a trailing .0 when it is integral."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _needs_quotes(text: str) -> bool:
    """Whether a YAML scalar string needs quoting to be safe/unambiguous."""
    if text == "":
        return True
    return any(not (c.isalnum() or c in "_-.") for c in text)


def _yaml_str(text: str) -> str:
    """Quote a string for YAML output only when necessary."""
    text = str(text)
    if _needs_quotes(text):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


# =====================================================================
# Signal extraction
# =====================================================================
@dataclass
class SignalEntry:
    name: str
    device: str
    init_value: int
    can_id: int
    can_msg_offset: int
    can_bit_length: int
    can_byteorder: str
    can_scaling: float
    can_offset: float
    mqtt_topic: str
    is_signed: bool
    unit: Optional[str]
    source: str
    vtable: List[Tuple[int, str]]


def _byteorder_str(signal) -> str:
    return "big" if signal.byte_order == "big_endian" else "little"


def _build_vtable(signal) -> List[Tuple[int, str]]:
    """Build a [(raw_unsigned_value, name), ...] list preserving DBC order."""
    choices = getattr(signal, "choices", None)
    if not choices:
        return []
    return [
        (_mask_to_unsigned(int(raw), signal.length), str(name))
        for raw, name in choices.items()
    ]


def _init_value_from_vtable(vtable: List[Tuple[int, str]]) -> int:
    """Pick the 'Init'/'INIT' entry as the init value, else 0."""
    for raw, name in vtable:
        if name.strip().lower() == "init":
            return raw
    return 0


def _build_signal_entry(message, signal, cfg: DbcParserConfig) -> SignalEntry:
    node = message.senders[0] if message.senders else "unknown"
    device = _clean_node_name(node)
    transmitted_by_c2m = node.strip().lower() == cfg.c2m_node.strip().lower()
    source = "MQTT" if transmitted_by_c2m else "CAN"

    vtable = _build_vtable(signal)
    topic = cfg.topic_template.format(
        node=device,
        device=device,
        signal=signal.name,
        message=message.name,
    )
    unit = signal.unit if signal.unit else None

    return SignalEntry(
        name=signal.name,
        device=device,
        init_value=_init_value_from_vtable(vtable),
        can_id=message.frame_id,
        can_msg_offset=signal.start,
        can_bit_length=signal.length,
        can_byteorder=_byteorder_str(signal),
        can_scaling=signal.scale,
        can_offset=signal.offset,
        mqtt_topic=topic,
        is_signed=bool(signal.is_signed),
        unit=unit,
        source=source,
        vtable=vtable,
    )


def extract_signals(db, cfg: DbcParserConfig) -> List[Tuple[object, List[SignalEntry]]]:
    """Return [(message, [SignalEntry, ...]), ...] sorted for stable output."""
    result: List[Tuple[object, List[SignalEntry]]] = []
    for message in sorted(db.messages, key=lambda m: m.frame_id):
        signals = sorted(message.signals, key=lambda s: s.start)
        entries = [_build_signal_entry(message, s, cfg) for s in signals]
        result.append((message, entries))
    return result


# =====================================================================
# YAML emission
# =====================================================================
def _emit_header(out: List[str]) -> None:
    out.append("# =====================================================================")
    out.append("# can2mqtt configuration")
    out.append("# ---------------------------------------------------------------------")
    out.append("# Auto-generated from a DBC file by helpers/c2m_dbc_to_config.py.")
    out.append("# Edit the generator (or this file) rather than hand-tuning by hand if")
    out.append("# you intend to regenerate it later.")
    out.append("# =====================================================================")
    out.append("")


def _emit_can(out: List[str], can: CanDefaults) -> None:
    out.append("# -------------------------------------------------")
    out.append("# CAN setup")
    out.append("# -------------------------------------------------")
    out.append("can:")
    out.append(f"  bus: {can.bus}")
    out.append(f"  tx_is_cyclic: {str(can.tx_is_cyclic).lower()}")
    out.append(f"  message_tx_cycle_default_s: {_num(can.message_tx_cycle_default_s)}")
    out.append(f"  message_tx_timeout_default_s: {_num(can.message_tx_timeout_default_s)}")
    out.append(f"  message_rx_timeout_default_s: {_num(can.message_rx_timeout_default_s)}")
    out.append(f"  rx_is_cyclic: {str(can.rx_is_cyclic).lower()}")
    out.append(f"  message_rx_cycle_default_s: {_num(can.message_rx_cycle_default_s)}")
    out.append("")


def _emit_mqtt(out: List[str], mqtt: MqttDefaults) -> None:
    out.append("# -------------------------------------------------")
    out.append("# MQTT setup")
    out.append("# -------------------------------------------------")
    out.append("mqtt:")
    out.append(f"  base_topic: {_yaml_str(mqtt.base_topic)}")
    out.append(f"  qos_default: {mqtt.qos_default}")
    out.append(f"  retain_default: {str(mqtt.retain_default).lower()}")
    out.append(f"  tx_is_cyclic: {str(mqtt.tx_is_cyclic).lower()}")
    out.append(f"  rx_is_cyclic: {str(mqtt.rx_is_cyclic).lower()}")
    out.append(f"  signal_tx_cycle_s: {_num(mqtt.signal_tx_cycle_s)}")
    out.append(f"  signal_timeout_s: {_num(mqtt.signal_timeout_s)}")
    out.append(f"  check_incoming_signal_name: {str(mqtt.check_incoming_signal_name).lower()}")
    out.append(f"  signal_name_in_payload: {str(mqtt.signal_name_in_payload).lower()}")
    out.append("")


def _emit_signal(out: List[str], entry: SignalEntry) -> None:
    # Every layout/encoding field is written explicitly per signal (there is
    # no signal_defaults block to fall back on).
    out.append(f"  - name: {_yaml_str(entry.name)}")
    out.append(f"    device: {_yaml_str(entry.device)}")
    out.append(f"    init_value: {_hex(entry.init_value)}")
    out.append(f"    can_id: {_hex(entry.can_id)}")
    out.append(f"    can_msg_offset: {entry.can_msg_offset}")
    out.append(f"    can_bit_length: {entry.can_bit_length}")
    out.append(f"    can_byteorder: {entry.can_byteorder}")
    out.append(f"    can_scaling: {_num(entry.can_scaling)}")
    out.append(f"    can_offset: {_num(entry.can_offset)}")
    out.append(f"    is_signed: {str(entry.is_signed).lower()}")
    out.append(f"    mqtt_topic: {_yaml_str(entry.mqtt_topic)}")
    if entry.unit:
        out.append(f"    unit: {_yaml_str(entry.unit)}")
    out.append(f"    source: {entry.source}")
    if entry.vtable:
        out.append("    vtable:")
        for raw, name in entry.vtable:
            out.append(f"      {_hex(raw)}: {_yaml_str(name)}")
    out.append("")


def render_yaml(db, cfg: DbcParserConfig) -> str:
    out: List[str] = []
    _emit_header(out)
    _emit_can(out, cfg.can)
    _emit_mqtt(out, cfg.mqtt)

    out.append("# -------------------------------------------------")
    out.append("# Signals (generated from the DBC)")
    out.append("# -------------------------------------------------")
    out.append("signals:")

    for message, entries in extract_signals(db, cfg):
        if not entries:
            continue
        out.append(f"  # --- {message.name} (BO_ {message.frame_id} / {_hex(message.frame_id)}) ---")
        for entry in entries:
            _emit_signal(out, entry)

    # Collapse a trailing blank line into a single terminating newline.
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out) + "\n"


# =====================================================================
# CLI
# =====================================================================
def _build_config_from_args(args: argparse.Namespace) -> DbcParserConfig:
    cfg = DbcParserConfig()
    cfg.c2m_node = args.c2m_node
    cfg.topic_template = args.topic_template
    cfg.can.bus = args.bus
    cfg.mqtt.base_topic = args.base_topic
    return cfg


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a can2mqtt YAML config from a DBC file.",
    )
    parser.add_argument("dbc", type=str, help="Path to the input DBC file.")
    parser.add_argument(
        "-o",
        "--output",
        type=str,
        default=None,
        help="Output YAML path (default: stdout).",
    )
    parser.add_argument(
        "--c2m-node",
        type=str,
        default=DbcParserConfig.c2m_node,
        help="DBC node the c2m bridge runs on; messages it transmits get "
        f"source=MQTT, all others source=CAN (default: {DbcParserConfig.c2m_node}).",
    )
    parser.add_argument(
        "--bus",
        type=str,
        default=CanDefaults.bus,
        help=f"CAN bus name written to the config (default: {CanDefaults.bus}).",
    )
    parser.add_argument(
        "--base-topic",
        type=str,
        default=MqttDefaults.base_topic,
        help=f"MQTT base topic (default: {MqttDefaults.base_topic}).",
    )
    parser.add_argument(
        "--topic-template",
        type=str,
        default="{node}/{signal}",
        help="Per-signal topic suffix template. Fields: {node}, {device}, "
        "{signal}, {message} (default: {node}/{signal}).",
    )
    args = parser.parse_args(argv)

    dbc_path = Path(args.dbc)
    if not dbc_path.is_file():
        parser.error(f"DBC file not found: {dbc_path}")

    db = cantools.database.load_file(str(dbc_path))
    cfg = _build_config_from_args(args)
    yaml_text = render_yaml(db, cfg)

    if args.output:
        out_path = Path(args.output)
        out_path.write_text(yaml_text, encoding="utf-8")
        print(f"Wrote {len(db.messages)} messages to {out_path}", file=sys.stderr)
    else:
        sys.stdout.write(yaml_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
