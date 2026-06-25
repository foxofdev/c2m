# can2mqtt (c2m)

A configurable bridge between a **CAN bus** and an **MQTT broker**. It maps
individual CAN signals to MQTT topics (and back), so anything on the bus can be
read and controlled over MQTT without writing bus-specific glue code.

The bridge is config-driven: a single YAML file (`c2m_config.yaml`) describes
the CAN/MQTT settings and every signal (CAN id, bit offset/length, byte order,
scaling, unit, value table, MQTT topic, direction). That config can be written
by hand or generated from a DBC file with the helper in [`helpers/`](helpers/).

## What c2m does

For each signal in the config, c2m knows its physical CAN layout and its MQTT
topic. Depending on the signal's `source` it moves data in one of two
directions:

- **`source: CAN` (telemetry, CAN → MQTT)** — c2m listens on the bus, decodes
  the raw bits into a scaled value, and publishes it to the signal's MQTT topic.
- **`source: MQTT` (commands, MQTT → CAN)** — c2m subscribes to the signal's
  topic, and encodes incoming values into the correct CAN message, which it
  sends cyclically on the bus.

Along the way it handles bit-level encoding/decoding (Intel little-endian and
Motorola big-endian, signed/unsigned), scaling + offset, SI-prefix unit
conversion (e.g. `mV` ↔ `V`), value tables (named enum values), signal
timeouts, and periodic service-status reporting over MQTT.

### MQTT payload format

Values are exchanged as a small JSON object. `value` and `timestamp` (Unix
time) are mandatory; `name`, `unit` and `value_name` are optional:

```json
{ "value": 1, "timestamp": 1718524800, "unit": "V", "value_name": "Running" }
```

## How it fits together

```
CAN bus     <----->  C2mCanBridge  ┐
                                   |──  C2mSignalRegistry  (C2mSignal objects)
MQTT broker <----->  C2mMqttBridge ┘
                           ▲
                     C2mConfigParser (c2m_config.yaml)
```

The config parser builds a registry of `C2mSignal` objects; the two bridges
attach to that registry and translate between the bus and the broker. `c2m.py`
wires it all up and runs the main loop.

## Folder structure

```
can2mqtt/
├── c2m.py                  Entry point: CLI, wiring, main loop
├── c2m_config_parser.py    Loads c2m_config.yaml → C2mSignalRegistry
├── c2m_signal.py           C2mSignal + C2mSignalRegistry (bit codec, units, value tables)
├── c2m_can.py              C2mCanBridge: CAN <-> registry (decode RX, encode/send TX)
├── c2m_mqtt.py             C2mMqttBridge: MQTT <-> registry (subscribe/publish, JSON payload)
├── c2m_status.py           C2mStatusReporter: service status (starting/running/stopped) over MQTT
├── c2m_logging.py          Logging setup + exit-reason handlers
├── requirements.txt        Python dependencies (python-can, paho-mqtt, cantools, PyYAML)
├── helpers/                 
│   ├── c2m_dbc_to_config.py Script to convert a DBC file into c2m config
│   └── README.md           Documentation for the DBC to config script
└── examples/
    └── basic/              A basic example of how to use c2m
        ├── c2m_config.yaml The basic example configuration
        ├── setup.sh        Script to run the example
        ├── basic_example.py Mock CAN device to generate and listen to traffic
        ├── mosquitto.conf  Basic MQTT broker config
        └── README.md       Instructions for the basic example
```

## Usage

Install dependencies and run the bridge against an MQTT broker:

```bash
pip install -r requirements.txt
python c2m.py <MQTT_HOST> <MQTT_PORT> --config c2m_config.yaml
```

Useful options: `--config`, `--mqtt-username` / `--mqtt-password`,
`--log-level`, `--log-dir`, `--log-verbose`, `--status` / `--no-status`,
`--status-topic`. Run `python c2m.py --help` for the full list.

> The CAN side uses SocketCAN, so a configured `canX` interface is required
> (this is a Linux-only feature; on other platforms only config parsing works).

## Related docs

- [`examples/basic/README.md`](examples/basic/README.md) — Basic example overview and usage
- [`helpers/README.md`](helpers/README.md) — generating a config from a DBC file
