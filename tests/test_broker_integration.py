# =============================================================================
# HYDRA-UMC-BRIDGE-AMR - Integration with a real MQTT broker
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The VDA 5050 client of this bridge against a real HYDRA-UMC-MQTT-BROKER on a free local port (see broker_harness.py).

This bridge does not use the shared command dispatcher: it publishes orders and follows the vehicle's `state` and `connection` topics. The tests skip themselves when
node, paho-mqtt or the installed broker checkout are not there, so the ordinary run does not need a broker.
"""

import json
import os
import time
import unittest

import broker_harness as harness
from hydra_umc_sdk.bridge_contract import MachineState

from hydra_umc_bridge_amr import AmrDispatch, Vda5050Publisher, Vda5050Target
from hydra_umc_bridge_amr.mqtt_transport import open_mqtt_client
from hydra_umc_bridge_amr.observer import Vda5050Observer

TARGET = Vda5050Target("vda5050", "v2", "hydra-umc", "amr-1")
STATE = {"manufacturer": "hydra-umc", "serialNumber": "amr-1", "operatingMode": "AUTOMATIC", "driving": False}


def connection(state: str) -> str:
    return json.dumps({"manufacturer": "hydra-umc", "serialNumber": "amr-1", "connectionState": state})


class AmrOverBroker(unittest.TestCase):
    def setUp(self) -> None:
        reason = harness.skip_reason()
        if reason:
            self.skipTest(reason)
        self._closers = []
        self.broker = harness.BrokerProcess()
        self.broker.start()
        self.addCleanup(self._cleanup)
        self.observer = Vda5050Observer(TARGET)
        self.client = open_mqtt_client(
            "127.0.0.1",
            self.broker.port,
            client_id=f"it-amr-{os.getpid()}",
            max_connect_attempts=5,
            connect_retry_delay_seconds=0.5,
            observer=self.observer,
        )
        self._closers.append(lambda: (self.client.loop_stop(), self.client.disconnect()))  # type: ignore[attr-defined]

    def _cleanup(self) -> None:
        for closer in reversed(self._closers):
            try:
                closer()
            except Exception:  # noqa: BLE001 - cleanup of a test must not hide its result
                pass
        self.broker.stop()

    def collector(self, name: str) -> harness.Collector:
        collector = harness.Collector(self.broker.port, f"it-{name}-{os.getpid()}")
        self._closers.append(collector.close)
        return collector

    def wait_for(self, condition, timeout: float = 20.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return True
            time.sleep(0.1)
        return False

    def test_an_order_reaches_a_subscriber_through_the_broker(self) -> None:
        listener = self.collector("order")
        listener.subscribe(TARGET.topic("order"))
        dispatch = AmrDispatch(True, "MOVE_TO_DESTINATION", "ok", 1.5, 2.5, "site-a")
        result = Vda5050Publisher().publish(self.client, TARGET, None, None, dispatch)  # type: ignore[arg-type]
        self.assertTrue(result.published, result.reason)
        message = listener.get(10.0, TARGET.topic("order"))
        self.assertIsNotNone(message)
        assert message is not None
        body = json.loads(message[1])
        self.assertEqual((body["manufacturer"], body["serialNumber"], body["orderUpdateId"]), ("hydra-umc", "amr-1", 0))
        self.assertEqual(message[2], 0, "the publisher sends at QoS 0 and the broker must not raise it")

    def test_the_observer_follows_the_retained_state_and_connection_of_the_vehicle(self) -> None:
        agv = self.collector("agv")
        agv.publish(TARGET.topic("connection"), connection("ONLINE"), qos=1, retain=True)
        agv.publish(TARGET.topic("state"), json.dumps(STATE), qos=0, retain=True)
        self.assertTrue(self.wait_for(lambda: self.observer.machine_state() == MachineState.IDLE), "the observer never saw the vehicle")
        self.assertEqual(self.observer.connection_state, "ONLINE")

    def test_the_last_will_of_the_vehicle_makes_it_offline_for_the_observer(self) -> None:
        import paho.mqtt.client as mqtt

        agv = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"it-agv-{os.getpid()}")
        agv.will_set(TARGET.topic("connection"), connection("CONNECTIONBROKEN"), qos=1, retain=True)
        agv.connect("127.0.0.1", self.broker.port)
        agv.loop_start()
        self.addCleanup(agv.loop_stop)
        agv.publish(TARGET.topic("connection"), connection("ONLINE"), qos=1, retain=True).wait_for_publish(10)
        agv.publish(TARGET.topic("state"), json.dumps(STATE), qos=0, retain=True).wait_for_publish(10)
        self.assertTrue(self.wait_for(lambda: self.observer.machine_state() == MachineState.IDLE))
        agv.loop_stop()
        agv._sock.close()  # an unclean loss: no DISCONNECT, so the broker publishes the will
        self.assertTrue(self.wait_for(lambda: self.observer.connection_state == "CONNECTIONBROKEN"), "the will never reached the observer")
        self.assertEqual(self.observer.machine_state(), MachineState.OFFLINE)

    def test_orders_and_observation_come_back_after_the_broker_goes_down_and_up(self) -> None:
        self.broker.stop()
        time.sleep(2.0)
        self.broker.start()
        agv = self.collector("agvafter")
        # the observer subscribes again by itself after the reconnect: wait until the broker has it, then feed it
        def fed() -> bool:
            agv.publish(TARGET.topic("connection"), connection("ONLINE"), qos=1, retain=True)
            agv.publish(TARGET.topic("state"), json.dumps(STATE), qos=0, retain=True)
            return self.observer.machine_state() == MachineState.IDLE

        self.assertTrue(self.wait_for(fed, 60.0), "the observer did not resubscribe after the broker came back")
        listener = self.collector("orderafter")
        listener.subscribe(TARGET.topic("order"))
        result = Vda5050Publisher().publish(self.client, TARGET, None, None, AmrDispatch(True, "MOVE_TO_DESTINATION", "ok", 1.0, 1.0, "site-a"))  # type: ignore[arg-type]
        self.assertTrue(result.published, result.reason)
        self.assertIsNotNone(listener.get(10.0, TARGET.topic("order")), "an order published after the reconnect never arrived")


if __name__ == "__main__":
    unittest.main()
