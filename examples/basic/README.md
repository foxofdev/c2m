# can2mqtt Basic Example

This is a basic demonstration of `can2mqtt`. It shows how to bridge CAN signals to MQTT and vice-versa using a virtual CAN interface (`vcan0`) and a local MQTT broker.

## Prerequisites

- **Linux OS** (for native `vcan0` support via SocketCAN). If you are on Mac/Windows, you must set up an equivalent CAN interface or run this on a Raspberry Pi / Linux machine.
- **Python 3**
- **Mosquitto** (`sudo apt install mosquitto mosquitto-clients`)

## Included Files

- `c2m_config.yaml`: The configuration file mapping the CAN signals to MQTT topics.
- `mosquitto.conf`: A minimal Mosquitto configuration that allows anonymous connections on port 1883.
- `setup.sh`: The main launch script that sets up the environment and starts all necessary services.
- `basic_example.py`: A Python script acting as a mock CAN device. It generates outgoing traffic and listens for incoming traffic.

## How to Run

1. Make the setup script executable:
   ```bash
   chmod +x setup.sh
   ```
2. Run the setup script:
   ```bash
   ./setup.sh
   ```

### What happens when you run `setup.sh`:
1. It ensures the `vcan0` interface is up.
2. It creates a Python virtual environment and installs `can2mqtt` dependencies.
3. It starts a local Mosquitto MQTT broker using `mosquitto.conf`.
4. It starts the main `c2m.py` service in the background.
5. It starts `basic_example.py` to generate and listen to CAN traffic.

## Interacting with the Example

Once running, `basic_example.py` will start sending a `fromCan` signal (flipping between 0 and 1) on CAN ID `0x1` every second. `can2mqtt` translates this and publishes it to MQTT.

**1. Watch the CAN to MQTT flow**
Open a new terminal and subscribe to the MQTT topic to see the data arriving from the CAN bus:
```bash
mosquitto_sub -t "c2m_basic_example/gateway/fromCan" -v
```

**2. Watch the MQTT to CAN flow**
`basic_example.py` is actively listening to the CAN bus for the `fromMqtt` signal. You can publish a message to MQTT, and `can2mqtt` will translate it back to CAN:
```bash
mosquitto_pub -t "c2m_basic_example/gateway/fromMqtt" -m "1"
```
You should see `basic_example.py` immediately print that it received the `fromMqtt` bit in its console output!

## Stopping

Simply press `Ctrl+C` in the terminal where `setup.sh` is running. A cleanup trap will automatically kill the background `c2m.py` process.
