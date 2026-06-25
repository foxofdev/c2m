#!/usr/bin/env python

"""CAN to MQTT bridge apllication."""

import argparse
import logging
import os
import time
from typing import Optional

# CAN imports
from can import Bus
from can.notifier import Notifier

# MQTT imports
import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

# own imports
from c2m_signal import C2mSignalRegistry
from c2m_mqtt import C2mMqttBridge
from c2m_can import C2mCanBridge
from c2m_config_parser import C2mConfigParser, C2M_DEFAULT_CONFIG_PATH
from c2m_logging import setup_logging, flush_log_handlers, install_exit_reason_handlers
from c2m_status import C2mStatusReporter

# Constants
STATUS_PUBLISH_INTERVAL_S = 10
MQTT_CONNECT_RETRY_COUNT = 10
MQTT_CONNECT_RETRY_COOLDOWN_S = 5


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="CAN to MQTT bridge"
    )
    parser.add_argument(
        "MQTT_HOST",
        type=str,
        help="MQTT broker hostname or IP address",
    )
    parser.add_argument(
        "MQTT_PORT",
        type=int,
        help="MQTT broker port",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=str(C2M_DEFAULT_CONFIG_PATH),
        help=f"Path to the YAML config file (default: {C2M_DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--mqtt-username",
        type=str,
        default=None,
        help="MQTT broker username (optional)",
    )
    parser.add_argument(
        "--mqtt-password",
        type=str,
        default=None,
        help="MQTT broker password (optional)",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Set the logging level (default: INFO)",
    )
    parser.add_argument(
        "--log-dir",
        type=str,
        default=None,
        help="Set the logging directory (default: ~/log/can2mqtt)",
    )
    parser.add_argument(
        "--log-verbose",
        action="store_true",
        default=False,
        help="Log verbose messages to console (default: False)",
    )
    parser.add_argument(
        "--status",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable/disable periodic MQTT status reporting (default: enabled)",
    )
    parser.add_argument(
        "--status-topic",
        type=str,
        default=None,
        help="MQTT topic for status reporting (default: <base_topic>/status/c2m-<bus>)",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    # Load configuration (CAN/MQTT settings + signal definitions)
    config = C2mConfigParser(args.config)
    mqtt_client_id = f"can2mqtt-{os.getenv('USER', 'unknown')}-{config.can_bus}"
    status_topic = args.status_topic or f"{config.mqtt_base_topic}/service/can2mqtt-{config.can_bus}"

    # Setup logging first, before any other operations
    setup_logging(
        bus_name=config.can_bus,
        log_level=args.log_level,
        log_dir=args.log_dir,
        log_verbose=args.log_verbose,
    )
    logger = logging.getLogger(__name__)
    install_exit_reason_handlers(logger)

    # Initialize variables to None for safe cleanup
    mqtt_client: Optional[mqtt.Client] = None
    mqtt_bridge: Optional[C2mMqttBridge] = None
    can_bridge: Optional[C2mCanBridge] = None
    signal_registry: Optional[C2mSignalRegistry] = None
    status: Optional[C2mStatusReporter] = None
    mqtt_loop_started: bool = False
    started_at = C2mStatusReporter.utc_iso_now()

    try:
        signal_registry = config.build_registry()
        logger.info("Registered %d signals", len(signal_registry.get_all_signals()))

        # Setup MQTT
        mqtt_client = mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=mqtt_client_id,
            userdata=None,
            protocol=mqtt.MQTTv5
        )
        status = C2mStatusReporter(
            client=mqtt_client,
            topic=status_topic,
            mode=args.log_level,
            can_bus=config.can_bus,
            started=started_at,
            enabled=args.status,
        )
        if status.enabled:
            # Last Will and Testament: minimal payload for unclean disconnect
            mqtt_client.will_set(status.topic, status.lwt_payload(), qos=0, retain=True)
        if args.mqtt_username is not None:
            mqtt_client.username_pw_set(args.mqtt_username, args.mqtt_password)
        for attempt in range(1, MQTT_CONNECT_RETRY_COUNT + 1):
            try:
                mqtt_client.connect(args.MQTT_HOST, args.MQTT_PORT, 60)
                logger.info("MQTT connect attempt %d/%d succeeded", attempt, MQTT_CONNECT_RETRY_COUNT)
                break
            except OSError as e:
                logger.warning(
                    "MQTT connect attempt %d/%d failed: %s", attempt, MQTT_CONNECT_RETRY_COUNT, e
                )
                if attempt < MQTT_CONNECT_RETRY_COUNT:
                    logger.info("Retrying in %ss...", MQTT_CONNECT_RETRY_COOLDOWN_S)
                    time.sleep(MQTT_CONNECT_RETRY_COOLDOWN_S)
                else:
                    raise
        mqtt_client.loop_start()
        mqtt_loop_started = True
        logger.info("Started MQTT loop and connected to %s:%s", args.MQTT_HOST, args.MQTT_PORT)
        status.publish("starting")

        mqtt_bridge = C2mMqttBridge(
            client=mqtt_client, registry=signal_registry,
            default_qos=config.mqtt_qos_default, default_retain=config.mqtt_retain_default,
            signal_rx_timeout_s=config.can_message_rx_timeout_default_s,
            check_incoming_signal_name=config.check_incoming_signal_name,
            signal_name_in_payload=config.signal_name_in_payload,
            tx_is_cyclic=config.mqtt_tx_is_cyclic,
        )
        mqtt_bridge.attach_all_signals()
        logger.info("Attached all signals to MQTT bridge")

        can_bridge = C2mCanBridge(
            registry=signal_registry,
            message_tx_timeout_s=config.can_message_tx_timeout_default_s,
        )
        logger.info("Initialized CAN bridge")

        with Bus(channel=config.can_bus, interface="socketcan", receive_own_messages=False) as bus:
            can_notifiers = [ Notifier(bus=[ bus ], listeners=[]) ]

            can_bridge.initialize(can_notifiers)
            logger.info("Initialized CAN bridge and attached all signals to CAN bridge")
            status.publish("running")
            last_status_at = time.monotonic()
            if config.mqtt_tx_is_cyclic:
                last_mqtt_tx_at = time.monotonic()
            try:
                while True:
                    can_bridge.send_mqtt_signals_to_can()
                    now = time.monotonic()
                    if now - last_status_at >= STATUS_PUBLISH_INTERVAL_S:
                        status.publish("running")
                        last_status_at = now
                    if config.mqtt_tx_is_cyclic:
                        if now - last_mqtt_tx_at >= config.mqtt_signal_tx_cycle_s:
                            mqtt_bridge.publish_all()
                            last_mqtt_tx_at = now
                    time.sleep(config.can_message_tx_cycle_default_s)
            finally:
                # Stop Notifiers before exiting the with block (which closes the bus).
                # Otherwise the Notifier thread keeps reading and hits "Bad file descriptor".
                for n in can_notifiers:
                    try:
                        n.stop()
                        logger.info("Stopped CAN Notifier")
                    except Exception as e:
                        logger.error("Error stopping Notifier: %s", e, exc_info=True)
    except KeyboardInterrupt:
        logger.info("Interrupted by user!")
    except Exception as e:
        logger.error("Error: %s", e, exc_info=True)
    finally:
        # Cleanup in reverse order of initialization
        logger.info("Cleanup started")
        if mqtt_client is not None:
            # Publish stopped so broker has final state (LWT only fires on unclean disconnect)
            if status is not None:
                stopped_at = C2mStatusReporter.utc_iso_now()
                status.publish("stopped", stopped=stopped_at)
                time.sleep(0.2) # allow publish to flush
            if mqtt_loop_started:
                try:
                    mqtt_client.loop_stop()
                    logger.info("Stopped MQTT loop")
                except Exception as e:
                    logger.error("Error stopping MQTT loop: %s", e, exc_info=True)
            try:
                mqtt_client.disconnect()
                logger.info("Disconnected MQTT client")
            except Exception as e:
                logger.error("Error disconnecting MQTT client: %s", e, exc_info=True)

        if mqtt_bridge is not None:
            try:
                mqtt_bridge.deinitialize()
                logger.info("Deinitialized MQTT bridge")
            except Exception as e:
                logger.error("Error deinitializing MQTT bridge: %s", e, exc_info=True)

        if can_bridge is not None:
            try:
                can_bridge.deinitialize()
                logger.info("Deinitialized CAN bridge")
            except Exception as e:
                logger.error("Error deinitializing CAN bridge: %s", e, exc_info=True)

        if signal_registry is not None:
            try:
                signal_registry.clear()
                logger.info("Cleared C2mSignal registry")
            except Exception as e:
                logger.error("Error clearing C2mSignal registry: %s", e, exc_info=True)
        logger.info("Cleanup completed")
        flush_log_handlers()


if __name__ == "__main__":
    main()
