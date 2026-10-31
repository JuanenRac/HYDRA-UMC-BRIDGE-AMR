# =============================================================================
# HYDRA-UMC-BRIDGE-AMR - Global stop tests
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The VDA 5050 publisher and the announcement of the global stop: off by default, SAFE_STOP refuses what moves the vehicle, READY resumes, an unreadable state fails closed."""

import json
import os
import unittest
from unittest import mock

from hydra_umc_bridge_amr import AmrDispatch, Vda5050Publisher, Vda5050Target
from hydra_umc_bridge_amr.global_stop import DEFAULT_TOPIC, GlobalStopGuard, GlobalStopSettings, process_wide_guard, settings_from_environment

TARGET = Vda5050Target("vda5050", "v2", "hydra-umc", "amr-1")
SAFE_STOP = json.dumps({"schema_version": "1.0", "state": "SAFE_STOP", "source": "hydra-umc-server"}).encode()
READY = json.dumps({"schema_version": "1.0", "state": "READY", "source": "hydra-umc-server"}).encode()
ENVIRONMENT = "HYDRA_UMC_AMR_GLOBAL_STOP"


class Client:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def publish(self, topic: str, payload: str) -> object:
        self.sent.append((topic, payload))
        return type("Info", (), {"rc": 0})()


MOVE = AmrDispatch(True, "MOVE_TO_DESTINATION", "ok", 1.5, 2.5, "site-a")
PICK = AmrDispatch(True, "PICK_LOAD", "ok")
CANCEL = AmrDispatch(True, "CANCEL_ORDER", "ok", channel="instantActions")
PAUSE = AmrDispatch(True, "START_PAUSE", "ok", channel="instantActions")
RESUME = AmrDispatch(True, "STOP_PAUSE", "ok", channel="instantActions")


def guard(enabled: bool = True, strict: bool = False) -> GlobalStopGuard:
    return GlobalStopGuard(GlobalStopSettings(enabled, DEFAULT_TOPIC, strict))


class SettingsTests(unittest.TestCase):
    def test_the_default_is_off_and_the_topic_is_the_servers(self) -> None:
        settings = GlobalStopSettings()
        self.assertFalse(settings.enabled)
        self.assertFalse(settings.strict)
        self.assertEqual(settings.topic, DEFAULT_TOPIC)
        self.assertEqual(DEFAULT_TOPIC, "hydra/swarm/safety/estop")

    def test_a_topic_with_wildcards_is_refused(self) -> None:
        for bad in ("", "hydra/#", "hydra/+/estop", "/hydra", "hydra/"):
            with self.subTest(topic=bad), self.assertRaises(ValueError):
                GlobalStopSettings(True, bad)

    def test_the_environment_switches_it_on(self) -> None:
        wanted = {ENVIRONMENT: "1", ENVIRONMENT + "_TOPIC": "plant/safety/estop", ENVIRONMENT + "_STRICT": "true"}
        with mock.patch.dict(os.environ, wanted):
            settings = settings_from_environment("AMR")
        self.assertEqual((settings.enabled, settings.topic, settings.strict), (True, "plant/safety/estop", True))
        with mock.patch.dict(os.environ):
            os.environ.pop(ENVIRONMENT, None)
            self.assertFalse(settings_from_environment("AMR").enabled)

    def test_the_process_wide_guard_is_one_object(self) -> None:
        self.assertIs(process_wide_guard("AMR"), process_wide_guard("AMR"))
        self.assertFalse(process_wide_guard("AMR").enabled)


class GuardTests(unittest.TestCase):
    def test_stop_then_ready(self) -> None:
        g = guard()
        self.assertFalse(g.blocks())
        g.observe(SAFE_STOP)
        self.assertTrue(g.blocks())
        g.observe(READY)
        self.assertFalse(g.blocks())

    def test_anything_unclear_is_a_stop_while_enabled(self) -> None:
        for payload in (b"", b"not json", b"[]", b'{"state": "INHIBITED"}', b'{"state": "FAULT"}', b'{"state": "ready"}', b'{"x": 1}'):
            with self.subTest(payload=payload):
                g = guard()
                g.observe(READY)
                g.observe(payload)
                self.assertTrue(g.blocks())

    def test_strict_also_refuses_before_the_first_message(self) -> None:
        self.assertFalse(guard().blocks())
        strict = guard(strict=True)
        self.assertTrue(strict.blocks())
        strict.observe(READY)
        self.assertFalse(strict.blocks())

    def test_a_switched_off_guard_never_blocks(self) -> None:
        g = guard(enabled=False)
        g.observe(SAFE_STOP)
        self.assertFalse(g.blocks())


class PublisherTests(unittest.TestCase):
    def publish(self, publisher: Vda5050Publisher, dispatch: AmrDispatch):
        client = Client()
        result = publisher.publish(client, TARGET, None, None, dispatch)  # type: ignore[arg-type]
        return result, client

    def test_off_by_default_nothing_changes(self) -> None:
        g = guard(enabled=False)
        g.observe(SAFE_STOP)
        result, client = self.publish(Vda5050Publisher(global_stop=g), MOVE)
        self.assertTrue(result.published, result.reason)
        self.assertEqual(len(client.sent), 1)

    def test_a_movement_and_a_load_are_refused_during_a_stop(self) -> None:
        g = guard()
        g.observe(SAFE_STOP)
        publisher = Vda5050Publisher(global_stop=g)
        for dispatch in (MOVE, PICK, RESUME):
            with self.subTest(action=dispatch.action):
                result, client = self.publish(publisher, dispatch)
                self.assertFalse(result.published)
                self.assertIn("global stop", result.reason)
                self.assertEqual(client.sent, [])

    def test_the_abort_actions_always_go_out(self) -> None:
        g = guard()
        g.observe(SAFE_STOP)
        publisher = Vda5050Publisher(global_stop=g)
        for dispatch in (CANCEL, PAUSE):
            with self.subTest(action=dispatch.action):
                result, client = self.publish(publisher, dispatch)
                self.assertTrue(result.published, result.reason)
                self.assertEqual(len(client.sent), 1)

    def test_ready_resumes(self) -> None:
        g = guard()
        g.observe(SAFE_STOP)
        publisher = Vda5050Publisher(global_stop=g)
        self.assertFalse(self.publish(publisher, MOVE)[0].published)
        g.observe(READY)
        self.assertTrue(self.publish(publisher, MOVE)[0].published)

    def test_an_unreadable_announcement_fails_closed(self) -> None:
        g = guard()
        g.observe(READY)
        g.observe(b"\xff\xfe")
        result, client = self.publish(Vda5050Publisher(global_stop=g), MOVE)
        self.assertFalse(result.published)
        self.assertEqual(client.sent, [])

    def test_strict_refuses_before_the_first_announcement(self) -> None:
        result, _ = self.publish(Vda5050Publisher(global_stop=guard(strict=True)), MOVE)
        self.assertFalse(result.published)

    def test_a_rejected_dispatch_keeps_its_own_reason(self) -> None:
        g = guard()
        g.observe(SAFE_STOP)
        result, _ = self.publish(Vda5050Publisher(global_stop=g), AmrDispatch(False, "MOVE_TO_DESTINATION", "gate said no"))
        self.assertEqual(result.reason, "gate said no")


class FakeMqttModule:
    """Just enough of paho to see what `open_mqtt_client` subscribes to and how it routes a message."""

    class CallbackAPIVersion:
        VERSION2 = 2

    class Client:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.subscribed: list[object] = []
            self.on_connect = None
            self.on_message = None
            self.on_disconnect = None

        def reconnect_delay_set(self, **_kwargs: object) -> None:
            pass

        def connect(self, *_args: object, **_kwargs: object) -> None:
            pass

        def subscribe(self, topic: object, qos: int = 0) -> None:
            self.subscribed.append((topic, qos))

        def loop_start(self) -> None:
            assert self.on_connect is not None
            self.on_connect(self, None, None, type("Reason", (), {"is_failure": False})(), None)

        def loop_stop(self) -> None:
            pass


class ClientTests(unittest.TestCase):
    def open(self, g: GlobalStopGuard, observer=None):
        from hydra_umc_bridge_amr import mqtt_transport

        paho = type("Paho", (), {"mqtt": type("Mqtt", (), {"client": FakeMqttModule})()})
        with mock.patch.dict("sys.modules", {"paho": paho, "paho.mqtt": paho.mqtt, "paho.mqtt.client": FakeMqttModule}):
            return mqtt_transport.open_mqtt_client("127.0.0.1", 1883, global_stop=g, observer=observer, connack_timeout_seconds=1.0)

    def message(self, topic: str, payload: bytes):
        return type("Message", (), {"topic": topic, "payload": payload, "retain": True})()

    def test_it_subscribes_and_follows_the_announcement_when_on(self) -> None:
        g = guard()
        client = self.open(g)
        self.assertIn((DEFAULT_TOPIC, 1), client.subscribed)  # type: ignore[attr-defined]
        client.on_message(client, None, self.message(DEFAULT_TOPIC, SAFE_STOP))  # type: ignore[attr-defined]
        self.assertTrue(g.blocks())
        client.on_message(client, None, self.message(DEFAULT_TOPIC, READY))  # type: ignore[attr-defined]
        self.assertFalse(g.blocks())

    def test_it_does_not_subscribe_when_off(self) -> None:
        client = self.open(guard(enabled=False))
        self.assertEqual(client.subscribed, [])  # type: ignore[attr-defined]
        self.assertIsNone(client.on_message)  # type: ignore[attr-defined]

    def test_the_observer_still_gets_its_own_messages(self) -> None:
        seen: list[str] = []

        class Observer:
            def subscriptions(self) -> list[tuple[str, int]]:
                return [("vda5050/v2/hydra-umc/amr-1/state", 0)]

            def on_message(self, topic: str, payload: bytes) -> None:
                seen.append(topic)

        g = guard()
        client = self.open(g, Observer())
        client.on_message(client, None, self.message("vda5050/v2/hydra-umc/amr-1/state", b"{}"))  # type: ignore[attr-defined]
        client.on_message(client, None, self.message(DEFAULT_TOPIC, SAFE_STOP))  # type: ignore[attr-defined]
        self.assertEqual(seen, ["vda5050/v2/hydra-umc/amr-1/state"])
        self.assertTrue(g.blocks())


if __name__ == "__main__":
    unittest.main()
