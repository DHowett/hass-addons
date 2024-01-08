#!/usr/bin/env python
# coding=utf-8

from __future__ import print_function
from __future__ import with_statement

AP_DESCRIPTION="""
Publish Home Assistant MQTT auto discovery topics for Interlogix devices
detected by rtl_433.

Derived from rtl_433_mqtt_hass.py at 7813a6afa5070f5109959cf3b7fbd084590b1e36.
"""

AP_EPILOG="""
* Python 3.x preferred.
* Needs Paho-MQTT https://pypi.python.org/pypi/paho-mqtt

  Debian/raspbian:  apt install python3-paho-mqtt
  Or
  pip install paho-mqtt
* Optional for running as a daemon see PEP 3143 - Standard daemon process library
  (use Python 3.x or pip install python-daemon)
"""
# import daemon


import os
import argparse
import logging
import time
import json
import paho.mqtt.client as mqtt
import re


discovery_timeouts = {}

# Fields that get ignored when publishing to Home Assistant
# (reduces noise to help spot missing field mappings)
SKIP_KEYS = [ "type", "model", "subtype", "channel", "id", "mic", "mod",
                "freq", "sequence_num", "message_type", "exception", "raw_msg" ]

topic_key_device_id_mappings = {
    "type": {
    },
    "model": {
        "Interlogix-Systems": "qol", # Qolsys
    },
    "subtype": {
        "contact": "",
    },
}

mappings = {
    "battery_ok": {
        "device_type": "binary_sensor",
        "object_suffix": "sensor_battery",
        "config": {
            "name": "Battery",
            "payload_on": "0",
            "payload_off": "1",
            "device_class": "battery",
            "state_class": "measurement",
            "entity_category": "diagnostic"
        }
    },

    "switch1": {
        "device_type": "binary_sensor",
        "object_suffix": "sensor",
        "config": {
            "name": "Sensor",
            "payload_on": "OPEN",
            "payload_off": "CLOSED",
            "device_class": "door",
        }
    },

    "switch3": {
        "device_type": "binary_sensor",
        "object_suffix": "tamper",
        "config": {
            "name": "Tamper",
            "payload_on": "OPEN",
            "payload_off": "CLOSED",
            "device_class": "tamper",
        }
    },

}

TOPIC_PARSE_RE = re.compile(r'\[(?P<slash>/?)(?P<token>[^\]:]+):?(?P<default>[^\]:]*)\]')

def mqtt_connect(client, userdata, flags, rc):
    """Callback for MQTT connects."""

    logging.info("MQTT connected: " + mqtt.connack_string(rc))
    if rc != 0:
        logging.error("Could not connect. Error: " + str(rc))
    else:
        logging.info("Subscribing to: " + args.rtl_topic)
        client.subscribe(args.rtl_topic)


def mqtt_disconnect(client, userdata, rc):
    """Callback for MQTT disconnects."""
    logging.info("MQTT disconnected: " + mqtt.connack_string(rc))


def mqtt_message(client, userdata, msg):
    """Callback for MQTT message PUBLISH."""
    logging.debug("MQTT message: " + json.dumps(msg.payload.decode()))

    try:
        # Decode JSON payload
        data = json.loads(msg.payload.decode())

    except json.decoder.JSONDecodeError:
        logging.error("JSON decode error: " + msg.payload.decode())
        return

    topicprefix = "/".join(msg.topic.split("/", 2)[:2])
    bridge_event_to_hass(client, topicprefix, data)


def sanitize(text):
    """Sanitize a name for Graphite/MQTT use."""
    return (text
            .replace(" ", "_")
            .replace("/", "_")
            .replace(".", "_")
            .replace("&", ""))

def rtl_433_device_path(data, topic_prefix):
    """Return rtl_433 device topic to subscribe to for a data element, based on the
    rtl_433 device topic argument, as well as the device identifier"""

    path_elements = []
    last_match_end = 0
    # The default for args.device_topic_suffix is the same topic structure
    # as set by default in rtl433 config
    for match in re.finditer(TOPIC_PARSE_RE, args.device_topic_suffix):
        path_elements.append(args.device_topic_suffix[last_match_end:match.start()])
        key = match.group(2)
        if key in data:
            # If we have this key, prepend a slash if needed
            if match.group(1):
                path_elements.append('/')
            element = sanitize(str(data[key]))
            path_elements.append(element)
        elif match.group(3):
            path_elements.append(match.group(3))
        last_match_end = match.end()

    path = ''.join(list(filter(lambda item: item, path_elements)))
    return f"{topic_prefix}/{path}"


def publish_config(mqttc, topic, model, object_id, mapping, key=None):
    """Publish Home Assistant auto discovery data."""
    global discovery_timeouts

    device_type = mapping["device_type"]
    object_suffix = mapping["object_suffix"]
    object_name = "_".join([object_id.replace("-", "_"), object_suffix])

    path = "/".join([args.discovery_prefix, device_type, object_id, object_name, "config"])

    # check timeout
    now = time.time()
    if path in discovery_timeouts:
        if discovery_timeouts[path] > now:
            logging.debug("Discovery timeout in the future for: " + path)
            return False

    discovery_timeouts[path] = now + args.discovery_interval

    config = mapping["config"].copy()

    # Device Automation configuration is in a different structure compared to
    # all other mqtt discovery types.
    # https://www.home-assistant.io/integrations/device_trigger.mqtt/
    if device_type == 'device_automation':
        config["topic"] = topic
        config["platform"] = 'mqtt'
    else:
        readable_name = mapping["config"]["name"] if "name" in mapping["config"] else key
        config["state_topic"] = topic
        config["unique_id"] = object_name
        config["name"] = readable_name
    config["device"] = {
        "identifiers": [object_id],
        "name": object_id,
        "model": model,
        "manufacturer": "rtl_433"
    }

    if args.force_update:
        config["force_update"] = "true"

    if args.expire_after:
        config["expire_after"] = args.expire_after

    logging.debug(path + ":" + json.dumps(config))

    mqttc.publish(path, json.dumps(config), retain=args.retain)

    return True

def bridge_event_to_hass(mqttc, topic_prefix, data):
    """Translate some rtl_433 sensor data to Home Assistant auto discovery."""

    if "model" not in data:
        # not a device event
        logging.debug("Model is not defined. Not sending event to Home Assistant.")
        return

    model = sanitize(data["model"])

    skipped_keys = []
    published_keys = []

    base_topic = rtl_433_device_path(data, topic_prefix)
    ident = None
    if data["model"] == "Interlogix-Security":
        ident = data["id"]

    if ident:
        device_id = f"qol-{ident}"

    if not device_id:
        # no unique device identifier
        logging.warning("No suitable identifier found for model: %s", model)
        return

    if args.ids and "id" in data and data.get("id") not in args.ids:
        # not in the safe list
        logging.debug("Device (%s) is not in the desired list of device ids: [%s]" % (data["id"], ids))
        return

    # detect known attributes
    for key in data.keys():
        if key in mappings:
            # topic = "/".join([topicprefix,"devices",model,instance,key])
            topic = "/".join([base_topic, key])
            if publish_config(mqttc, topic, model, device_id, mappings[key], key):
                published_keys.append(key)
        else:
            if key not in SKIP_KEYS:
                skipped_keys.append(key)

    if published_keys:
        logging.info("Published %s: %s" % (device_id, ", ".join(published_keys)))

        if skipped_keys:
            logging.info("Skipped %s: %s" % (device_id, ", ".join(skipped_keys)))


def rtl_433_bridge():
    """Run a MQTT Home Assistant auto discovery bridge for rtl_433."""

    mqttc = mqtt.Client()

    if args.debug:
        mqttc.enable_logger()

    if args.user is not None:
        mqttc.username_pw_set(args.user, args.password)

    if args.ca_cert is not None:
        mqttc.tls_set(ca_certs=args.ca_cert)

    mqttc.on_connect = mqtt_connect
    mqttc.on_disconnect = mqtt_disconnect
    mqttc.on_message = mqtt_message
    mqttc.connect_async(args.host, args.port, 60)
    logging.debug("MQTT Client: Starting Loop")
    mqttc.loop_start()

    while True:
        time.sleep(1)


def run():
    """Run main or daemon."""
    # with daemon.DaemonContext(files_preserve=[sock]):
    #  detach_process=True
    #  uid
    #  gid
    #  working_directory
    rtl_433_bridge()


if __name__ == "__main__":
    logging.basicConfig(format='[%(asctime)s] %(levelname)s:%(name)s:%(message)s',datefmt='%Y-%m-%dT%H:%M:%S%z')
    logging.getLogger().setLevel(logging.INFO)

    parser = argparse.ArgumentParser(formatter_class=argparse.RawDescriptionHelpFormatter,
                                     description=AP_DESCRIPTION,
                                     epilog=AP_EPILOG)

    parser.add_argument("-d", "--debug", action="store_true")
    parser.add_argument("-q", "--quiet", action="store_true")
    parser.add_argument("-u", "--user", type=str, help="MQTT username")
    parser.add_argument("-P", "--password", type=str, help="MQTT password")
    parser.add_argument("-H", "--host", type=str, default="127.0.0.1",
                        help="MQTT hostname to connect to (default: %(default)s)")
    parser.add_argument("-p", "--port", type=int, default=1883,
                        help="MQTT port (default: %(default)s)")
    parser.add_argument("-c", "--ca_cert", type=str, help="MQTT TLS CA certificate path")
    parser.add_argument("-r", "--retain", action="store_true")
    parser.add_argument("-f", "--force_update", action="store_true",
                        help="Append 'force_update = true' to all configs.")
    parser.add_argument("-R", "--rtl-topic", type=str,
                        default="rtl_433/+/events",
                        dest="rtl_topic",
                        help="rtl_433 MQTT event topic to subscribe to (default: %(default)s)")
    parser.add_argument("-D", "--discovery-prefix", type=str,
                        dest="discovery_prefix",
                        default="homeassistant",
                        help="Home Assistant MQTT topic prefix (default: %(default)s)")
    # This defaults to the rtl433 config default, so we assemble the same topic structure
    parser.add_argument("-T", "--device-topic_suffix", type=str,
                        dest="device_topic_suffix",
                        default="devices[/type][/model][/subtype][/channel][/id]",
                        help="rtl_433 device topic suffix (default: %(default)s)")
    parser.add_argument("-i", "--interval", type=int,
                        dest="discovery_interval",
                        default=600,
                        help="Interval to republish config topics in seconds (default: %(default)d)")
    parser.add_argument("-x", "--expire-after", type=int,
                        dest="expire_after",
                        help="Number of seconds with no updates after which the sensor becomes unavailable")
    parser.add_argument("-I", "--ids", type=int, nargs="+",
                        help="ID's of devices that will be discovered (omit for all)")
    args = parser.parse_args()

    if args.debug and args.quiet:
        logging.critical("Debug and quiet can not be specified at the same time")
        exit(1)

    if args.debug:
        logging.info("Enabling debug logging")
        logging.getLogger().setLevel(logging.DEBUG)
    if args.quiet:
        logging.getLogger().setLevel(logging.ERROR)

    # allow setting MQTT username and password via environment variables
    if not args.user and 'MQTT_USERNAME' in os.environ:
        args.user = os.environ['MQTT_USERNAME']

    if not args.password and 'MQTT_PASSWORD' in os.environ:
        args.password = os.environ['MQTT_PASSWORD']

    if not args.user or not args.password:
        logging.warning("User or password is not set. Check credentials if subscriptions do not return messages.")

    if args.ids:
        ids = ', '.join(str(id) for id in args.ids)
        logging.info("Only discovering devices with ids: [%s]" % ids)
    else:
        logging.info("Discovering all devices")

    run()
