# =============================================================================
# HYDRA-UMC-BRIDGE-AMR - The global stop against a real MQTT broker
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The VDA 5050 client follows the retained announcement of the global stop through a real HYDRA-UMC-MQTT-BROKER on a free local port (see broker_harness.py).

The tests skip themselves when node, paho-mqtt or the installed broker checkout are not there, so the ordinary run does not need a broker.
"""

import json
import os
import time
import unittest

import broker_harness as harness

from hydra_umc_bridge_amr import AmrDispatch, Vda5050Publisher, Vda5050Target
from hydra_umc_bridge_amr.global_stop import DEFAULT_TOPIC, GlobalStopGuard, GlobalStopSettings
from hydra_umc_bridge_amr.mqtt_transport import open_mqtt_client

TARGET = Vda5050Target("vda5050", "v2", "hydra-umc", "amr-1")
SAFE_STOP = json.dumps({"schema_version": "1.0", "state": "SAFE_STOP", "source": "hydra-umc-server"})
READY = json.dumps({"schema_version": "1.0", "state": "READY", "source": "hydra-umc-server"})
MOVE = AmrDispatch(True, "MOVE_TO_DESTINATION", "ok", 1.5, 2.5, "site-a")
CANCEL = AmrDispatch(True, "CANCEL_ORDER", "ok", channel="instantActions")


class GlobalStopOverBroker(unittest.TestCase):
    def setUp(self) -> None:
        reason = harness.skip_reason()
        if reason:
            self.skipTest(reason)
        self._closers = []
        self.broker = harness.BrokerProcess()
        self.broker.start()
        self.addCleanup(self._cleanup)

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

    def start(self, settings: GlobalStopSettings):
        guard = GlobalStopGuard(settings)
        client = open_mqtt_client(
            "127.0.0.1",
            self.broker.port,
            client_id=f"it-amr-stop-{os.getpid()}",
            max_connect_attempts=5,
            connect_retry_delay_seconds=0.5,
            global_stop=guard,
        )
        self._closers.append(lambda: (client.loop_stop(), client.disconnect()))  # type: ignore[attr-defined]
        return guard, client, Vda5050Publisher(global_stop=guard)

    def wait_for(self, condition, timeout: float = 15.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return True
            time.sleep(0.1)
        return False

    def test_a_retained_stop_keeps_the_order_off_the_wire_until_ready(self) -> None:
        server = self.collector("server")
        server.publish(DEFAULT_TOPIC, SAFE_STOP, qos=1, retain=True)  # the stop sits on the broker before the client exists
        guard, client, publisher = self.start(GlobalStopSettings(True))
        self.assertTrue(self.wait_for(lambda: guard.state == "SAFE_STOP"), "the retained stop never reached the client")
        listener = self.collector("order")
        listener.subscribe(TARGET.topic("order"))
        listener.subscribe(TARGET.topic("instantActions"))
        refused = publisher.publish(client, TARGET, None, None, MOVE)  # type: ignore[arg-type]
        self.assertFalse(refused.published)
        self.assertIsNone(listener.get(1.5, TARGET.topic("order")), "an order reached the network during a stop")
        # the cancel goes out
        cancelled = publisher.publish(client, TARGET, None, None, CANCEL)  # type: ignore[arg-type]
        self.assertTrue(cancelled.published, cancelled.reason)
        self.assertIsNotNone(listener.get(10.0, TARGET.topic("instantActions")))
        # READY ends it
        server.publish(DEFAULT_TOPIC, READY, qos=1, retain=True)
        self.assertTrue(self.wait_for(lambda: guard.state == "READY"))
        resumed = publisher.publish(client, TARGET, None, None, MOVE)  # type: ignore[arg-type]
        self.assertTrue(resumed.published, resumed.reason)
        self.assertIsNotNone(listener.get(10.0, TARGET.topic("order")))

    def test_a_live_stop_arrives_at_every_qos(self) -> None:
        guard, client, publisher = self.start(GlobalStopSettings(True))
        server = self.collector("server")
        for qos in (0, 1, 2):
            with self.subTest(qos=qos):
                server.publish(DEFAULT_TOPIC, READY, qos=1, retain=True)
                self.assertTrue(self.wait_for(lambda: guard.state == "READY"))
                server.publish(DEFAULT_TOPIC, SAFE_STOP, qos=qos, retain=True)
                self.assertTrue(self.wait_for(guard.blocks))
                self.assertFalse(publisher.publish(client, TARGET, None, None, MOVE).published)  # type: ignore[arg-type]

    def test_the_switch_off_ignores_a_retained_stop(self) -> None:
        server = self.collector("server")
        server.publish(DEFAULT_TOPIC, SAFE_STOP, qos=1, retain=True)
        guard, client, publisher = self.start(GlobalStopSettings(False))
        self.assertTrue(publisher.publish(client, TARGET, None, None, MOVE).published)  # type: ignore[arg-type]
        self.assertEqual(guard.state, "UNKNOWN")

    def test_an_unreadable_announcement_fails_closed_through_the_broker(self) -> None:
        server = self.collector("server")
        server.publish(DEFAULT_TOPIC, "{ not json", qos=1, retain=True)
        guard, client, publisher = self.start(GlobalStopSettings(True))
        self.assertTrue(self.wait_for(lambda: guard.state == "UNREADABLE"))
        self.assertFalse(publisher.publish(client, TARGET, None, None, MOVE).published)  # type: ignore[arg-type]

    def test_the_stop_survives_the_loss_of_the_broker(self) -> None:
        server = self.collector("server")
        server.publish(DEFAULT_TOPIC, SAFE_STOP, qos=1, retain=True)
        guard, _client, _publisher = self.start(GlobalStopSettings(True))
        self.assertTrue(self.wait_for(lambda: guard.state == "SAFE_STOP"))
        port = self.broker.port
        self.broker.stop()
        time.sleep(1.0)
        self.assertTrue(guard.blocks(), "the stop must survive the loss of the broker")
        self.broker = harness.BrokerProcess(port)
        self.broker.start()
        self.assertTrue(guard.blocks())


if __name__ == "__main__":
    unittest.main()
