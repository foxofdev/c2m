# c2m_dbc_parser

Generate a [can2mqtt](../) YAML configuration directly from a DBC file.

The script reads a `.dbc` (using [`cantools`](https://github.com/cantools/cantools)) and emits a config in the same format as [`examples/solarlink/c2m_config.yaml`](../examples/solarlink/c2m_config.yaml), ready to be consumed by `c2m_config_parser.py`. All CAN layout, names, units and value tables come straight from the DBC, so the generated file stays in sync with your bus definition.

## Requirements

`cantools` (already listed in [`requirements.txt`](../requirements.txt)):

```bash
pip install -r ../requirements.txt
# or just:
pip install cantools
```

## Usage

```bash
python c2m_dbc_parser.py <input.dbc> [-o output.yaml] [options]
```

If `-o/--output` is omitted, the YAML is written to stdout.

### Examples

```bash
# Generate from the Solarlink DBC, write to a file
python c2m_dbc_parser.py ../Solarlink_Private.dbc -o c2m_config.yaml

# Print to stdout
python c2m_dbc_parser.py ../Solarlink_Private.dbc

# The bridge runs on the DC_Matrix node instead of the Gateway
python c2m_dbc_parser.py ../Solarlink_Private.dbc --c2m-node DC_Matrix -o c2m_config.yaml

# Custom bus name, base topic and topic layout
python c2m_dbc_parser.py ../Solarlink_Private.dbc \
    --bus can1 \
    --base-topic mysite \
    --topic-template "{node}/{signal}" \
    -o c2m_config.yaml
```

## Options

| Flag | Default | Description |
| --- | --- | --- |
| `dbc` (positional) | – | Path to the input DBC file. |
| `-o`, `--output` | stdout | Output YAML path. |
| `--c2m-node` | `Gateway` | DBC node the c2m bridge runs on. Messages this node transmits get `source: MQTT`; all others get `source: CAN`. Matched case-insensitively. |
| `--bus` | `can0` | CAN bus name written into the `can:` block. |
| `--base-topic` | `solarlink` | MQTT `base_topic`. |
| `--topic-template` | `{node}/{signal}` | Per-signal MQTT topic suffix template. Available fields: `{node}`, `{device}`, `{signal}`, `{message}`. |

The global `can:` and `mqtt:` blocks default to the values used in `examples/solarlink/c2m_config.yaml`.

## What gets mapped from the DBC

| Config field | DBC source |
| --- | --- |
| `name` | `SG_` signal name |
| `device` | message transmitter node (`BO_` sender), normalized (`DC_Matrix` → `dcmatrix`) |
| `can_id` | `BO_` frame id (emitted as hex) |
| `can_msg_offset` | `SG_` start bit |
| `can_bit_length` | `SG_` length |
| `can_byteorder` | `SG_` byte order (`@1` little / `@0` big) |
| `can_scaling` | `SG_` factor |
| `can_offset` | `SG_` offset |
| `is_signed` | `SG_` sign (`@..-` signed / `@..+` unsigned) |
| `unit` | `SG_` unit |
| `vtable` | `VAL_` / value-table entries (raw values, masked to the signal's bit width so signed `-1`/`-2` become `0xFFFFFFFF`/`0xFFFFFFFE`) |
| `init_value` | the value-table entry named `Init`/`INIT` if present, otherwise `0` |
| `mqtt_topic` | built from `--topic-template` (default `<node>/<signal>`), relative to `base_topic` |
| `source` | `MQTT` if the message is transmitted by `--c2m-node`, otherwise `CAN` |

Every signal carries its layout/encoding fields (`can_byteorder`, `can_scaling`, `can_offset`, `is_signed`) explicitly. There is no `signal_defaults` block — `c2m_config_parser.py` requires these keys on each signal, which is exactly why generating the config from the DBC is the intended workflow.

## Notes & limitations

- **Source classification is a heuristic.** A DBC does not record whether a signal flows MQTT→CAN or CAN→MQTT, so it is inferred from the transmitter relative to `--c2m-node`. This is correct for command/telemetry messages, but a node that transmits its *own* status (e.g. a `GatewayState` message sent by the `Gateway` while `--c2m-node Gateway`) will be classified as `MQTT` and may need a manual tweak to `CAN`.
- **Byte order:** only little-endian (`@1`) signals are exercised by the Solarlink DBC. Big-endian (Motorola) start bits are passed through as-is from `cantools`.
- The generated file is plain YAML; you can hand-edit it afterwards. If you plan to regenerate, prefer adjusting the generator or DBC over editing the output.

## Verifying the output

The generated config can be loaded with the project's own parser:

```bash
cd ..
python -c "from c2m_config_parser import C2mConfigParser; r = C2mConfigParser('helpers/c2m_config.yaml').build_registry(); print(len(r.get_all_signals()), 'signals')"
```
