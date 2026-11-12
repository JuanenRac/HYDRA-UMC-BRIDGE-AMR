# =============================================================================
# HYDRA-UMC-BRIDGE-AMR - how the real MQTT client subscribes and routes each message to the right part of the bridge
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""open_mqtt_client wires the job entry, the zone interlock, the remote settings, the observer and the state reporter onto one client. Here paho is a
small fake, so what is subscribed after a connect, which part gets which topic, and what happens on a refused or unanswered connection are checked
without a broker (the broker tests of the repository cover the real wire)."""

import logging
import sys
import unittest
from unittest import mock

from hydra_umc_bridge_amr import mqtt_transport

from test_global_stop import FakeMqttModule, guard


def paho_modules(client_class=None):
    module = type("ClientModule", (), {"CallbackAPIVersion": FakeMqttModule.CallbackAPIVersion, "Client": client_class or FakeMqttModule.Client})
    paho = type("Paho", (), {"mqtt": type("Mqtt", (), {"client": module})()})
    return {"paho": paho, "paho.mqtt": paho.mqtt, "paho.mqtt.client": module}


class Part:
    """A part of the bridge that owns some topics: it records what it was given."""

    def __init__(self, prefix, subscriptions=None):
        self.prefix, self._subscriptions, self.received, self.steps = prefix, subscriptions, [], 0

    def subscriptions(self):
        return [(self.prefix + "/#", 1)] if self._subscriptions is None else self._subscriptions

    def handles(self, topic):
        return topic.startswith(self.prefix)

    def on_message(self, *args, **kwargs):
        self.received.append((args, kwargs))

    def step(self, client):
        self.steps += 1

    def maybe_publish(self, client):
        self.steps += 1


class Observer(Part):
    pass


def message(topic, payload=b"{}", retain=False):
    return type("Message", (), {"topic": topic, "payload": payload, "retain": retain})()


def disabled_guard():
    return guard(enabled=False)


def open_client(**parts):
    with mock.patch.dict(sys.modules, paho_modules()):
        return mqtt_transport.open_mqtt_client("127.0.0.1", 1883, global_stop=disabled_guard(), connack_timeout_seconds=1.0, **parts)


class SubscriptionTests(unittest.TestCase):
    def test_every_part_is_subscribed_after_the_connect(self):
        job, zones, remote, observer = Part("job"), Part("zones"), Part("settings"), Observer("state")
        client = open_client(job_entry=job, zone_interlock=zones, remote_settings=remote, observer=observer)
        topics = [entry for subscribed in client.subscribed for entry in (subscribed[0] if isinstance(subscribed[0], list) else [subscribed[0]])]
        for prefix in ("job", "zones", "settings", "state"):
            self.assertTrue(any(str(topic[0] if isinstance(topic, tuple) else topic).startswith(prefix) for topic in topics), prefix)

    def test_a_part_with_nothing_to_subscribe_adds_no_subscription(self):
        client = open_client(job_entry=Part("job", subscriptions=[]), zone_interlock=Part("zones", subscriptions=[]), remote_settings=Part("settings", subscriptions=[]))
        self.assertEqual(client.subscribed, [])


class RoutingTests(unittest.TestCase):
    def test_a_job_message_goes_to_the_job_entry_with_its_retained_flag(self):
        job = Part("job")
        client = open_client(job_entry=job)
        client.on_message(client, None, message("job/new", b"x", retain=True))
        self.assertEqual(job.received, [((client, "job/new", b"x"), {"retained": True})])

    def test_a_settings_message_goes_to_the_remote_settings_even_when_retained(self):
        remote = Part("settings")
        client = open_client(remote_settings=remote, job_entry=Part("job"))
        client.on_message(client, None, message("settings/amr", b"{}", retain=True))
        self.assertEqual(remote.received, [((client, b"{}"), {})])

    def test_an_occupancy_message_updates_the_interlock_and_steps_it(self):
        zones = Part("zones")
        client = open_client(zone_interlock=zones)
        client.on_message(client, None, message("zones/a", b"1"))
        self.assertEqual((len(zones.received), zones.steps), (1, 1))

    def test_a_state_message_goes_to_the_observer_then_steps_the_interlock_and_publishes_the_report(self):
        observer, zones, reporter = Observer("state"), Part("zones"), Part("report")
        client = open_client(observer=observer, zone_interlock=zones, state_reporter=reporter)
        client.on_message(client, None, message("state/amr-1", b"{}"))
        self.assertEqual(len(observer.received), 1)
        self.assertEqual((zones.steps, reporter.steps), (1, 1))

    def test_a_topic_nobody_owns_is_ignored(self):
        job = Part("job")
        client = open_client(job_entry=job)
        client.on_message(client, None, message("something/else"))
        self.assertEqual(job.received, [])

    def test_a_message_goes_to_one_part_only(self):
        first, second = Part("shared"), Part("shared")
        client = open_client(remote_settings=first, job_entry=second)
        client.on_message(client, None, message("shared/x"))
        self.assertEqual((len(first.received), len(second.received)), (1, 0))

    def test_without_any_part_no_handler_is_installed(self):
        self.assertIsNone(open_client().on_message)


class ConnectionTests(unittest.TestCase):
    def _open(self, client_class, **options):
        with mock.patch.dict(sys.modules, paho_modules(client_class)):
            return mqtt_transport.open_mqtt_client("127.0.0.1", 1883, global_stop=disabled_guard(), **options)

    def test_a_refused_login_is_an_error_and_the_loop_is_stopped(self):
        stopped = []

        class Refusing(FakeMqttModule.Client):
            def loop_start(self):
                self.on_connect(self, None, None, type("Reason", (), {"is_failure": True, "__str__": lambda s: "Not authorized"})(), None)

            def loop_stop(self):
                stopped.append(True)

        with self.assertRaises(RuntimeError) as caught:
            self._open(Refusing, connack_timeout_seconds=1.0)
        self.assertIn("Not authorized", str(caught.exception))
        self.assertEqual(stopped, [True])

    def test_a_broker_that_never_answers_is_an_error_and_the_loop_is_stopped(self):
        stopped = []

        class Silent(FakeMqttModule.Client):
            def loop_start(self):
                pass

            def loop_stop(self):
                stopped.append(True)

        with self.assertRaises(RuntimeError) as caught:
            self._open(Silent, connack_timeout_seconds=0.05)
        self.assertIn("did not answer", str(caught.exception))
        self.assertEqual(stopped, [True])

    def test_a_password_without_a_user_name_is_refused(self):
        with self.assertRaises(ValueError):
            self._open(None, password="secret")

    def test_a_missing_paho_is_a_clear_error(self):
        with mock.patch.dict(sys.modules, {"paho": None, "paho.mqtt": None, "paho.mqtt.client": None}):
            with self.assertRaises(RuntimeError) as caught:
                mqtt_transport.open_mqtt_client("127.0.0.1", 1883, global_stop=disabled_guard())
        self.assertIn("paho-mqtt is not installed", str(caught.exception))

    def test_losing_the_connection_is_logged_and_left_to_the_client_to_repair(self):
        client = open_client()
        with self.assertLogs(mqtt_transport._LOG, level=logging.WARNING) as logs:
            client.on_disconnect(client, None, 0)
        self.assertIn("reconnects on its own", logs.output[0])


if __name__ == "__main__":
    unittest.main()
