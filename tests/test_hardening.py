# =============================================================================
# HYDRA-UMC-BRIDGE-AMR - A client the broker accepts, an AGV that is listened to, orders that are valid JSON
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The broker only knows a client by its id, the AGV says on its own topics whether it can take an order, and a number that is not finite is not JSON."""

import json
import ssl
import sys
import types
import unittest
from unittest import mock

from hydra_umc_sdk.bridge_contract import BridgeJob, CellState, JobPhase, MachineState

from hydra_umc_bridge_amr import (
    AmrCoordinator,
    AmrDispatch,
    FrameTransform,
    Vda5050Observer,
    Vda5050Publisher,
    Vda5050Target,
    connect_with_retry,
    open_mqtt_client,
)

TARGET = Vda5050Target("vda5050", "v2", "hydra-umc", "amr-1")


class Recorder:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload):
        self.published.append((topic, payload))
        return types.SimpleNamespace(rc=0)


def move(x=1.0, y=2.0):
    return AmrDispatch(True, "MOVE_TO_DESTINATION", "ok", x, y, "site-a")


class WireIdentifiersTests(unittest.TestCase):
    def test_action_and_node_ids_carry_the_session_token_so_a_restart_cannot_repeat_them(self):
        ids = []
        for _ in range(2):
            recorder = Recorder()
            Vda5050Publisher().publish(recorder, TARGET, 1, "2026-01-01T00:00:00Z", AmrDispatch(True, "PICK_LOAD", "ok"))
            node = json.loads(recorder.published[0][1])["nodes"][0]
            ids.append((node["nodeId"], node["actions"][0]["actionId"]))
        self.assertNotEqual(ids[0], ids[1])   # two publishers = two runs of the bridge, same header id 1
        self.assertTrue(all(identifier.startswith("pick_load-") for pair in ids for identifier in pair))

    def test_header_id_and_timestamp_are_made_by_the_publisher_when_left_out(self):
        publisher, recorder = Vda5050Publisher(clock=lambda: "2026-05-06T07:08:09.123Z"), Recorder()
        for _ in range(3):
            self.assertTrue(publisher.publish(recorder, TARGET, None, None, move()).published)
        bodies = [json.loads(payload) for _, payload in recorder.published]
        self.assertEqual([body["headerId"] for body in bodies], [1, 2, 3])
        self.assertEqual(bodies[0]["timestamp"], "2026-05-06T07:08:09.123Z")

    def test_header_ids_count_per_topic(self):
        publisher, recorder = Vda5050Publisher(), Recorder()
        publisher.publish(recorder, TARGET, None, None, move())
        publisher.publish(recorder, TARGET, None, None, AmrDispatch(True, "CANCEL_ORDER", "ok", channel="instantActions"))
        self.assertEqual([json.loads(payload)["headerId"] for _, payload in recorder.published], [1, 1])

    def test_a_header_id_or_timestamp_that_vda_5050_would_not_accept_is_refused(self):
        for header_id, timestamp in ((-1, "2026-01-01T00:00:00Z"), (True, "2026-01-01T00:00:00Z"), ("1", "2026-01-01T00:00:00Z"),
                                     (1, "yesterday"), (1, ""), (1, "2026-01-01T00:00:00"), (1, "2026-01-01T00:00:00+02:00"), (1, 5)):
            with self.subTest(header_id=header_id, timestamp=timestamp):
                recorder = Recorder()
                self.assertFalse(Vda5050Publisher().publish(recorder, TARGET, header_id, timestamp, move()).published)
                self.assertEqual(recorder.published, [])


class JsonValidityTests(unittest.TestCase):
    def test_a_coordinate_that_overflows_is_refused_by_the_transform(self):
        transform = FrameTransform(origin_x=-1e308, origin_y=0.0, heading_rad=0.0, map_id="site-a")
        job = BridgeJob("j", "k", "s", JobPhase.PROCESS, MachineState.IDLE, {"x": "1e308", "y": "0"})
        refused = AmrCoordinator().dispatch(job, CellState.READY, transform, MachineState.IDLE)
        self.assertFalse(refused.accepted)
        self.assertIn("real numbers", refused.reason)
        with self.assertRaises(ValueError):
            transform.to_local(1e308, 0.0)

    def test_a_non_finite_number_never_reaches_the_wire(self):
        recorder = Recorder()
        result = Vda5050Publisher().publish(recorder, TARGET, 1, "2026-01-01T00:00:00Z", move(float("inf"), 0.0))
        self.assertFalse(result.published)
        self.assertIn("JSON", result.reason)
        self.assertEqual(recorder.published, [])

    def test_a_target_outside_the_configured_area_is_refused(self):
        transform = FrameTransform(0.0, 0.0, 0.0, "site-a", bounds=(0.0, 50.0, 0.0, 30.0))
        coordinator = AmrCoordinator()

        def job(x, y):
            return BridgeJob("j", "k", "s", JobPhase.PROCESS, MachineState.IDLE, {"x": str(x), "y": str(y)})

        self.assertTrue(coordinator.dispatch(job(10, 10), CellState.READY, transform, MachineState.IDLE).accepted)
        for x, y in ((51, 10), (-1, 10), (10, 31), (10, -0.5), (1e9, 1e9)):
            with self.subTest(x=x, y=y):
                self.assertFalse(coordinator.dispatch(job(x, y), CellState.READY, transform, MachineState.IDLE).accepted)

    def test_nonsense_bounds_cannot_be_built(self):
        for bounds in ((0, 0, 0, 1), (5, 1, 0, 1), (0, 1, 0), (0, float("nan"), 0, 1)):
            with self.subTest(bounds=bounds), self.assertRaises(ValueError):
                FrameTransform(0.0, 0.0, 0.0, "site-a", bounds=bounds)  # type: ignore[arg-type]


class ConnectionTests(unittest.TestCase):
    def _paho(self, on_loop_start):
        class Client:
            instances = []

            def __init__(self, *args, **kwargs):
                self.args, self.kwargs = args, kwargs
                self.subscribed = []
                self.on_connect = self.on_disconnect = self.on_message = None
                self.loop_stopped = False
                Client.instances.append(self)

            def username_pw_set(self, *a, **k):
                pass

            def tls_set(self, *a, **k):
                pass

            def reconnect_delay_set(self, *a, **k):
                pass

            def connect(self, *a, **k):
                pass

            def subscribe(self, topics):
                self.subscribed.append(topics)

            def loop_start(self):
                on_loop_start(self)

            def loop_stop(self):
                self.loop_stopped = True

        client_module = types.ModuleType("paho.mqtt.client")
        client_module.Client = Client
        client_module.CallbackAPIVersion = types.SimpleNamespace(VERSION2=2)
        mqtt_package = types.ModuleType("paho.mqtt")
        mqtt_package.client = client_module
        paho = types.ModuleType("paho")
        paho.mqtt = mqtt_package
        return Client, {"paho": paho, "paho.mqtt": mqtt_package, "paho.mqtt.client": client_module}

    def test_the_client_has_the_id_the_broker_authenticates_by(self):
        Client, modules = self._paho(lambda c: c.on_connect(c, None, None, types.SimpleNamespace(is_failure=False)))
        with mock.patch.dict(sys.modules, modules):
            open_mqtt_client("127.0.0.1")
            open_mqtt_client("127.0.0.1", client_id="hydra-umc-bridge-amr-2")
        self.assertEqual(Client.instances[0].kwargs["client_id"], "hydra-umc-bridge-amr")
        self.assertEqual(Client.instances[1].kwargs["client_id"], "hydra-umc-bridge-amr-2")
        self.assertEqual(Client.instances[0].args, (2,))   # callback API version 2

    def test_a_refused_login_is_an_error_at_start_not_a_silent_failure_of_every_publish(self):
        Client, modules = self._paho(
            lambda c: c.on_connect(c, None, None, types.SimpleNamespace(is_failure=True, __str__=lambda self: "Not authorized"))
        )
        with mock.patch.dict(sys.modules, modules), self.assertRaises(RuntimeError) as context:
            open_mqtt_client("127.0.0.1")
        self.assertIn("refused", str(context.exception))
        self.assertTrue(Client.instances[0].loop_stopped)

    def test_a_broker_that_never_answers_is_an_error_after_the_timeout(self):
        Client, modules = self._paho(lambda c: None)
        with mock.patch.dict(sys.modules, modules), self.assertRaises(RuntimeError) as context:
            open_mqtt_client("127.0.0.1", connack_timeout_seconds=0.05)
        self.assertIn("did not answer", str(context.exception))

    def test_with_an_observer_the_agv_topics_are_subscribed_on_every_connect_and_messages_reach_it(self):
        Client, modules = self._paho(lambda c: None)
        observer = Vda5050Observer(TARGET)

        def connect_twice(client):
            client.on_connect(client, None, None, types.SimpleNamespace(is_failure=False))

        Client2, modules2 = self._paho(connect_twice)
        with mock.patch.dict(sys.modules, modules2):
            client = open_mqtt_client("127.0.0.1", observer=observer)
            client.on_connect(client, None, None, types.SimpleNamespace(is_failure=False))   # a reconnect
        self.assertEqual(client.subscribed, [observer.subscriptions(), observer.subscriptions()])
        self.assertEqual(
            observer.subscriptions(), [("vda5050/v2/hydra-umc/amr-1/connection", 1), ("vda5050/v2/hydra-umc/amr-1/state", 0)]
        )
        client.on_message(client, None, types.SimpleNamespace(topic=observer.state_topic, payload=json.dumps(state()).encode()))
        self.assertEqual(observer.machine_state(), MachineState.IDLE)

    def test_a_tls_failure_is_not_retried(self):
        attempts = []

        def connect():
            attempts.append(1)
            raise ssl.SSLCertVerificationError("certificate verify failed")

        with self.assertRaises(RuntimeError) as context:
            connect_with_retry(connect, max_attempts=5, sleep=lambda _: None)
        self.assertEqual(len(attempts), 1)
        self.assertIn("TLS", str(context.exception))

    def test_a_plain_network_failure_is_still_retried(self):
        attempts = []

        def connect():
            attempts.append(1)
            raise ConnectionRefusedError("broker not up yet")

        with self.assertRaises(RuntimeError):
            connect_with_retry(connect, max_attempts=3, sleep=lambda _: None)
        self.assertEqual(len(attempts), 3)


def state(**overrides):
    body = {
        "manufacturer": "hydra-umc", "serialNumber": "amr-1", "operatingMode": "AUTOMATIC", "driving": False, "paused": False,
        "actionStates": [], "nodeStates": [], "errors": [], "lastNodeId": "n1",
        "safetyState": {"eStop": "NONE", "fieldViolation": False},
    }
    body.update(overrides)
    return body


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.now = [100.0]
        self.observer = Vda5050Observer(TARGET, max_age_seconds=30.0, clock=lambda: self.now[0])

    def feed(self, **overrides):
        self.observer.on_message(self.observer.state_topic, json.dumps(state(**overrides)).encode())

    def test_nothing_heard_is_offline(self):
        self.assertEqual(self.observer.machine_state(), MachineState.OFFLINE)

    def test_the_state_the_agv_reports_is_what_the_gate_gets(self):
        cases = [
            ({}, MachineState.IDLE),
            ({"driving": True}, MachineState.RUNNING),
            ({"actionStates": [{"actionStatus": "RUNNING"}]}, MachineState.RUNNING),
            ({"actionStates": [{"actionStatus": "FINISHED"}]}, MachineState.IDLE),
            ({"nodeStates": [{"nodeId": "n2"}]}, MachineState.RUNNING),
            ({"paused": True}, MachineState.HOLDING),
            ({"safetyState": {"eStop": "MANUAL", "fieldViolation": False}}, MachineState.SAFE_STOP),
            ({"safetyState": {"eStop": "NONE", "fieldViolation": True}}, MachineState.FAULT),
            ({"errors": [{"errorLevel": "FATAL"}]}, MachineState.FAULT),
            ({"errors": [{"errorLevel": "WARNING"}]}, MachineState.IDLE),
            ({"operatingMode": "MANUAL"}, MachineState.OFFLINE),
        ]
        for overrides, expected in cases:
            with self.subTest(overrides=overrides):
                self.feed(**overrides)
                self.assertEqual(self.observer.machine_state(), expected)

    def test_a_state_that_stops_arriving_goes_stale(self):
        self.feed()
        self.now[0] += 29
        self.assertEqual(self.observer.machine_state(), MachineState.IDLE)
        self.now[0] += 2
        self.assertEqual(self.observer.machine_state(), MachineState.OFFLINE)

    def test_a_connection_that_is_not_online_makes_the_vehicle_offline_until_it_is_back(self):
        self.feed()
        self.observer.on_message(self.observer.connection_topic, json.dumps({"manufacturer": "hydra-umc", "serialNumber": "amr-1", "connectionState": "CONNECTIONBROKEN"}).encode())
        self.assertEqual(self.observer.machine_state(), MachineState.OFFLINE)
        self.assertEqual(self.observer.connection_state, "CONNECTIONBROKEN")
        self.observer.on_message(self.observer.connection_topic, json.dumps({"manufacturer": "hydra-umc", "serialNumber": "amr-1", "connectionState": "ONLINE"}).encode())
        self.assertEqual(self.observer.machine_state(), MachineState.IDLE)

    def test_another_vehicle_a_foreign_topic_and_garbage_are_ignored(self):
        self.observer.on_message(self.observer.state_topic, json.dumps(state(serialNumber="amr-2")).encode())
        self.observer.on_message("vda5050/v2/hydra-umc/amr-1/order", json.dumps(state()).encode())
        for garbage in (b"", b"not json", b"[" * 50000, b"[]", b"\xff\xfe", b"null"):
            self.observer.on_message(self.observer.state_topic, garbage)
        self.assertEqual(self.observer.machine_state(), MachineState.OFFLINE)

    def test_the_last_node_is_known_once_reported(self):
        self.assertIsNone(self.observer.last_node_id)
        self.feed(lastNodeId="station-4")
        self.assertEqual(self.observer.last_node_id, "station-4")

    def test_a_nonsense_age_limit_is_refused(self):
        for age in (0, -1, True, "30", float("nan")):
            with self.subTest(age=age), self.assertRaises(ValueError):
                Vda5050Observer(TARGET, max_age_seconds=age)  # type: ignore[arg-type]

    def test_the_gate_refuses_a_job_when_the_vehicle_reports_it_is_busy_whatever_the_job_claims(self):
        job = BridgeJob("j", "k", "s", JobPhase.PROCESS, MachineState.IDLE, {"x": "1", "y": "1"})
        transform = FrameTransform(0.0, 0.0, 0.0, "site-a")
        self.feed(driving=True)
        refused = AmrCoordinator().dispatch(job, CellState.READY, transform, self.observer.machine_state())
        self.assertFalse(refused.accepted)
        self.assertIn("RUNNING", refused.reason)


if __name__ == "__main__":
    unittest.main()
