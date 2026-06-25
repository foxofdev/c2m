#!/bin/bash

# Exit on error
set -e

# Configuration
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
ROOT_DIR="$(dirname $(dirname $DIR))"
VENV_DIR="${DIR}/venv"
CONFIG_FILE="${DIR}/c2m_config.yaml"
TRAFFIC_SCRIPT="${DIR}/basic_example.py"
MOSQUITTO_CONF="${DIR}/mosquitto.conf"

echo "=== can2mqtt basic example setup ==="

# 1.1. Enable vcan0
echo "1.1. Enabling vcan0 interface..."
if [[ "$OSTYPE" == "linux-gnu"* ]]; then
    sudo modprobe vcan 2>/dev/null || echo "vcan module already loaded or unavailable"
    sudo ip link add dev vcan0 type vcan 2>/dev/null || echo "vcan0 already exists"
    sudo ip link set up vcan0 2>/dev/null || echo "vcan0 already up"
else
    echo "Warning: Not on Linux (detected OS: $OSTYPE). Skipping native vcan0 setup."
    echo "Make sure your environment supports socketcan or provides a vcan0 equivalent."
fi

# 1.2. Make a python venv that satisfies the requirements.txt
echo "1.2. Setting up Python virtual environment..."
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"
pip install --upgrade pip
pip install -r "${ROOT_DIR}/requirements.txt"

# 1.3. Start a simple mqtt broker using mosquitto
echo "1.3. Starting Mosquitto MQTT broker..."
mosquitto -c "$MOSQUITTO_CONF" -d 2>/dev/null || {
    echo "Mosquitto is likely already running on port 1883 or not installed."
}

# Cleanup on exit
function cleanup {
    echo "Cleaning up..."
    if [ -n "$C2M_PID" ]; then
        kill $C2M_PID 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

# 1.4. Start the c2m with the config
echo "1.4. Starting c2m in the background..."
python "${ROOT_DIR}/c2m.py" -c "$CONFIG_FILE" &
C2M_PID=$!

# Give c2m a moment to initialize
sleep 2

# 1.5. Start the python script described next
echo "1.5. Starting CAN traffic generator..."
python "$TRAFFIC_SCRIPT" --bus vcan0
