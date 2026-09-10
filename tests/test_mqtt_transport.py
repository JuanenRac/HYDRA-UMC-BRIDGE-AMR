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


class FakePublishResult:
    """Stands in for paho-mqtt's own real MQTTMessageInfo - only the `.rc`
    field this module actually reads (AMR-01's own regression)."""

    def __init__(self, rc: int = 0):
        self.rc = rc


class FakeMqttClient:
    def __init__(self):
        self.published: list[tuple[str, str]] = []
        self.raise_on_publish: OSError | None = None
        self.next_rc: int = 0  # 0 == paho-mqtt's own real MQTT_ERR_SUCCESS

    def publish(self, topic: str, payload: str):
        if self.raise_on_publish:
            raise self.raise_on_publish
        if self.next_rc == 0:
            self.published.append((topic, payload))
        return FakePublishResult(self.next_rc)


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
        accepted = AmrDispatch(True, "MOVE_TO_DESTINATION", "cell and external machine are ready", 1.5, 2.5, "site-a")
        result = Vda5050Publisher().publish(client, TARGET, 7, "2026-01-01T00:00:00Z", accepted)
        self.assertTrue(result.published)
        self.assertEqual(result.topic, "vda5050/v2/hydra-umc/amr-1/order")
        topic, payload_raw = client.published[0]
        payload = json.loads(payload_raw)
        self.assertEqual(payload["headerId"], 7)
        self.assertEqual(payload["manufacturer"], "hydra-umc")
        self.assertEqual(payload["serialNumber"], "amr-1")
        self.assertEqual(len(payload["nodes"]), 1)
        # AMR-02 regression: a real VDA 5050 nodePosition requires mapId
        # alongside x/y (github.com/VDA5050/VDA5050/blob/2.0.0/json_schemas/order.schema).
        self.assertEqual(payload["nodes"][0]["nodePosition"], {"x": 1.5, "y": 2.5, "mapId": "site-a"})
        self.assertEqual(payload["nodes"][0]["actions"][0]["actionType"], "MOVE_TO_DESTINATION")
        self.assertEqual(payload["edges"], [])

    def test_a_movement_dispatch_missing_map_id_is_refused_regression_for_amr_02(self):
        # Exercises the publisher's own defensive guard directly (a real
        # AmrCoordinator always supplies map_id alongside local_x/local_y -
        # see FrameTransform's own required field - but the publisher must
        # not trust that and emit a spec-invalid message if it's ever
        # missing anyway).
        client = FakeMqttClient()
        accepted = AmrDispatch(True, "MOVE_TO_DESTINATION", "cell and external machine are ready", 1.5, 2.5, None)
        result = Vda5050Publisher().publish(client, TARGET, 7, "2026-01-01T00:00:00Z", accepted)
        self.assertFalse(result.published)
        self.assertIn("map_id", result.reason)
        self.assertEqual(client.published, [])

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

    def test_a_disconnected_client_rc_is_reported_not_silently_true_regression_for_amr_01(self):
        # AMR-01:
        # paho-mqtt's own real Client.publish() does not raise when there
        # is no live connection - it returns rc=MQTT_ERR_NO_CONN (4).
        client = FakeMqttClient()
        client.next_rc = 4
        accepted = AmrDispatch(True, "MOVE_TO_HOME", "cell and external machine are ready")
        result = Vda5050Publisher().publish(client, TARGET, 4, "2026-01-01T00:00:00Z", accepted)
        self.assertFalse(result.published)
        self.assertIn("rc=4", result.reason)
        self.assertEqual(client.published, [])

    def test_a_successful_rc_is_reported_honestly_as_queued_not_agv_accepted(self):
        client = FakeMqttClient()
        accepted = AmrDispatch(True, "MOVE_TO_HOME", "cell and external machine are ready")
        result = Vda5050Publisher().publish(client, TARGET, 4, "2026-01-01T00:00:00Z", accepted)
        self.assertTrue(result.published)
        self.assertIn("queued", result.reason)
        self.assertNotIn("accepted", result.reason.lower())

    def test_end_to_end_through_the_real_coordinator_gate_before_publishing(self):
        # Confirms the publisher composes with AmrCoordinator.dispatch()
        # exactly as a future adapter would use it, not just in isolation.
        from hydra_umc_bridge_amr import BridgeJob, CellState, JobPhase, MachineState

        transform = FrameTransform(0.0, 0.0, 0.0, "site-a")
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


class ConnectWithRetryTests(unittest.TestCase):
    """connect_with_retry() is pure - no real paho-mqtt/broker needed to
    prove the real startup-race tolerance that was missing here (this
    bridge used to fail outright if it started before
    HYDRA-UMC-MQTT-BROKER was listening yet)."""

    def test_succeeds_on_the_first_try_without_sleeping(self):
        from hydra_umc_bridge_amr import connect_with_retry

        sleeps: list = []
        connect_with_retry(lambda: None, sleep=sleeps.append)
        self.assertEqual(sleeps, [])

    def test_retries_a_transient_connection_failure_then_succeeds(self):
        from hydra_umc_bridge_amr import connect_with_retry

        attempts = {"n": 0}
        sleeps: list = []

        def flaky_connect() -> None:
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise ConnectionRefusedError("broker not up yet")

        connect_with_retry(flaky_connect, max_attempts=5, retry_delay_seconds=1.5, sleep=sleeps.append)
        self.assertEqual(attempts["n"], 3)
        self.assertEqual(sleeps, [1.5, 1.5])

    def test_gives_up_after_max_attempts_with_a_clear_error(self):
        from hydra_umc_bridge_amr import connect_with_retry

        def always_fails() -> None:
            raise ConnectionRefusedError("broker still not up")

        with self.assertRaises(RuntimeError) as context:
            connect_with_retry(always_fails, max_attempts=3, retry_delay_seconds=0.01, sleep=lambda _: None)
        self.assertIn("after 3 attempts", str(context.exception))
        self.assertIn("broker still not up", str(context.exception))

    def test_a_non_os_error_is_never_retried(self):
        from hydra_umc_bridge_amr import connect_with_retry

        def broken_connect() -> None:
            raise ValueError("not an OSError - a real bug, not a broker being down")

        with self.assertRaises(ValueError):
            connect_with_retry(broken_connect, sleep=lambda _: None)


if __name__ == "__main__":
    unittest.main()
