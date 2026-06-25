import can
import time
import argparse
import threading

def receive_traffic(bus):
    print("Started listening for CAN traffic (Press Ctrl+C to stop).")
    while True:
        try:
            msg = bus.recv(timeout=1.0)
            if msg is not None:
                # We expect fromMqtt to come back on ID 0x456
                if msg.arbitration_id == 0x456:
                    # c2m should be sending this when fromMqtt changes
                    # fromMqtt is at can_msg_offset: 3 (byte 3)
                    val = "N/A"
                    if len(msg.data) > 3:
                        val = msg.data[3] & 0x01
                    print(f"[RX] ID: 0x1, Data: {list(msg.data)}, extracted fromMqtt bit: {val}")
        except KeyboardInterrupt:
            break
        except Exception as e:
            pass

def generate_traffic(bus_name):
    # Setup CAN bus
    print(f"Connecting to CAN bus: {bus_name}")
    try:
        bus = can.interface.Bus(channel=bus_name, bustype='socketcan')
    except Exception as e:
        print(f"Failed to connect to {bus_name}: {e}")
        print("Ensure the interface exists and python-can is configured correctly.")
        return
    
    print(f"Starting to send 'fromCan' traffic on {bus_name} (ID: 0x1)")
    
    # Start receiver thread
    rx_thread = threading.Thread(target=receive_traffic, args=(bus,), daemon=True)
    rx_thread.start()

    val = 0
    try:
        while True:
            # We send ID 0x123. 'fromCan' is at can_msg_offset: 0
            # We use an 8-byte payload to ensure byte 3 (for fromMqtt) exists 
            # if they share the same CAN ID and c2m expects a full frame.
            data = [val & 0x01, 0, 0, 0, 0, 0, 0, 0]
            msg = can.Message(arbitration_id=0x123, data=data, is_extended_id=False)
            bus.send(msg)
            print(f"[TX] Sent fromCan={val & 0x01} -> {msg}")
            
            # Flip value between 0 and 1
            val = 1 - val
            
            # Send every 1 second
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nStopping traffic generator.")
    finally:
        bus.shutdown()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Basic example: Generate and listen to CAN traffic.")
    parser.add_argument('--bus', default='vcan0', help='CAN bus interface to use (default: vcan0)')
    args = parser.parse_args()
    
    generate_traffic(args.bus)
