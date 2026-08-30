# =============================================================================
# HYDRA-UMC-BRIDGE-AMR - Real VDA 5050 MQTT transport tests
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Tests the real VDA 5050 publisher against an in-memory fake MQTT client.

No real broker or paho-mqtt install is needed: Vda5050Publisher is written
against the small MqttPublisher protocol, so a plain fake proves the real
topic/payload shape is correct independent of paho-mqtt - only
open_mqtt_client() itself needs it, and it isn't exercised here.
"""

import json
import unittest

from hydra_umc_bridge_amr import AmrCoordinator, AmrDispatch, FrameTransform, Vda5050Publisher, Vda5050Target


class FakeMqttClient:
    def __init__(self):
        self.published: list[tuple[str, str]] = []
        self.raise_on_publish: OSError | None = None

    def publish(self, topic: str, payload: str):
        if self.raise_on_publish:
            raise self.raise_on_publish
        self.published.append((topic, payload))


TARGET = Vda5050Target("vda5050", "v2", "hydra-umc", "amr-1")


class Vda5050TargetTests(unittest.TestCase):
    def test_topic_matches_the_real_documented_template(self):
        # interfaceName/majorVersion/manufacturer/serialNumber/topic -
        # github.com/VDA5050/VDA5050 section 4.2, e.g. "vda5050/v3/KIT/0001/order".
        self.assertEqual(TARGET.topic("order"), "vda5050/v2/hydra-umc/amr-1/order")
        self.assertEqual(TARGET.topic("instantActions"), "vda5050/v2/hydra-umc/amr-1/instantActions")


class Vda5050PublisherTests(unittest.TestCase):
    def test_a_rejected_dispatch_is_never_published(self):
        client = FakeMqttClient()
        rejected = AmrDispatch(False, "MOVE_TO_DESTINATION", "cell is FAULT, not READY")
        result = Vda5050Publisher().publish(client, TARGET, 1, "2026-01-01T00:00:00Z", rejected)
        self.assertFalse(result.published)
        self.assertEqual(client.published, [])

    def test_a_movement_order_publishes_to_the_real_order_topic_with_a_single_destination_node(self):
        client = FakeMqttClient()
        accepted = AmrDispatch(True, "MOVE_TO_DESTINATION", "cell and external machine are ready", 1.5, 2.5)
        result = Vda5050Publisher().publish(client, TARGET, 7, "2026-01-01T00:00:00Z", accepted)
        self.assertTrue(result.published)
        self.assertEqual(result.topic, "vda5050/v2/hydra-umc/amr-1/order")
        topic, payload_raw = client.published[0]
        payload = json.loads(payload_raw)
        self.assertEqual(payload["headerId"], 7)
        self.assertEqual(payload["manufacturer"], "hydra-umc")
        self.assertEqual(payload["serialNumber"], "amr-1")
        self.assertEqual(len(payload["nodes"]), 1)
        self.assertEqual(payload["nodes"][0]["nodePosition"], {"x": 1.5, "y": 2.5})
        self.assertEqual(payload["nodes"][0]["actions"][0]["actionType"], "MOVE_TO_DESTINATION")
        self.assertEqual(payload["edges"], [])

    def test_a_non_movement_order_omits_nodeposition_rather_than_inventing_a_coordinate(self):
        client = FakeMqttClient()
        accepted = AmrDispatch(True, "PICK_LOAD", "cell and external machine are ready", None, None)
        Vda5050Publisher().publish(client, TARGET, 2, "2026-01-01T00:00:00Z", accepted)
        payload = json.loads(client.published[0][1])
        self.assertNotIn("nodePosition", payload["nodes"][0])

    def test_cancel_order_publishes_to_the_real_separate_instant_actions_topic(self):
        # VDA 5050's own real channel split - order and instantActions are
        # genuinely separate MQTT topics, not two labels on one channel.
        client = FakeMqttClient()
        cancel = AmrDispatch(True, "CANCEL_ORDER", "abort requests are always forwarded", channel="instantActions")
        result = Vda5050Publisher().publish(client, TARGET, 3, "2026-01-01T00:00:00Z", cancel)
        self.assertEqual(result.topic, "vda5050/v2/hydra-umc/amr-1/instantActions")
        payload = json.loads(client.published[0][1])
        self.assertNotIn("nodes", payload)
        self.assertEqual(payload["actions"][0]["actionType"], "CANCEL_ORDER")
        self.assertEqual(payload["actions"][0]["blockingType"], "HARD")

    def test_a_transport_failure_is_reported_not_swallowed(self):
        client = FakeMqttClient()
        client.raise_on_publish = OSError("broker unreachable")
        accepted = AmrDispatch(True, "MOVE_TO_HOME", "cell and external machine are ready")
        result = Vda5050Publisher().publish(client, TARGET, 4, "2026-01-01T00:00:00Z", accepted)
        self.assertFalse(result.published)
        self.assertIn("broker unreachable", result.reason)

    def test_end_to_end_through_the_real_coordinator_gate_before_publishing(self):
        # Confirms the publisher composes with AmrCoordinator.dispatch()
        # exactly as a future adapter would use it, not just in isolation.
        from hydra_umc_bridge_amr import BridgeJob, CellState, JobPhase, MachineState

        transform = FrameTransform(0.0, 0.0, 0.0)
        job = BridgeJob("job-1", "idempotency-1", "amr-1", JobPhase.PROCESS, MachineState.IDLE, {"x": "3", "y": "4"})
        dispatch = AmrCoordinator().dispatch(job, CellState.READY, transform)
        client = FakeMqttClient()
        result = Vda5050Publisher().publish(client, TARGET, 1, "2026-01-01T00:00:00Z", dispatch)
        self.assertTrue(result.published)


class OpenMqttClientTests(unittest.TestCase):
    def test_missing_paho_mqtt_raises_a_clear_runtime_error_not_an_import_error(self):
        from hydra_umc_bridge_amr import open_mqtt_client

        try:
            import paho.mqtt.client  # noqa: F401

            self.skipTest("paho-mqtt is installed in this environment - nothing to prove here")
        except ImportError:
            pass
        with self.assertRaises(RuntimeError) as context:
            open_mqtt_client("localhost")
        self.assertIn("paho-mqtt is not installed", str(context.exception))


if __name__ == "__main__":
    unittest.main()
