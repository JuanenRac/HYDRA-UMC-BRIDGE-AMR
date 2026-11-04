# =============================================================================
# HYDRA-UMC-BRIDGE-AMR - tests of the switches announced by the server (no broker)
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The retained settings of the server: listened to only when the host allows it, validated, and applied to the state reporter and the zone interlock."""

import json
import unittest

from hydra_umc_bridge_amr.remote_settings import REMOTE_ENVIRONMENT, SETTINGS_TOPIC, RemoteSettings, parse_remote_settings, remote_settings_allowed
from hydra_umc_bridge_amr.state_report import StateReporter
from hydra_umc_bridge_amr.observer import Vda5050Observer

from test_route_telemetry_interlock import TARGET, agv_state, interlock, observer_with


def message(**settings):
    return json.dumps({"schema": 1, "settings": settings}).encode()


class FakeClient:
    def __init__(self):
        self.subscribed = []

    def subscribe(self, subscriptions):
        self.subscribed.append(subscriptions)


class ParseTests(unittest.TestCase):
    def test_the_host_allows_remote_settings_only_with_its_own_switch(self):
        self.assertFalse(remote_settings_allowed({}))
        self.assertFalse(remote_settings_allowed({REMOTE_ENVIRONMENT: "0"}))
        self.assertTrue(remote_settings_allowed({REMOTE_ENVIRONMENT: "1"}))

    def test_a_valid_message_gives_the_known_switches_and_ignores_the_rest(self):
        found = parse_remote_settings(message(publishState=True, publishOpcua=False, zoneInterlock=True, zoneUnknown="ignore", other=3))
        self.assertEqual(found, {"publishState": True, "publishOpcua": False, "zoneInterlock": True, "zoneUnknown": "ignore"})

    def test_anything_else_changes_nothing(self):
        for payload in (b"", b"nope", b"[]", b'{"schema":2,"settings":{}}', message(publishState="yes"), message(zoneUnknown="maybe"), message(zoneUnknown=True), b" " * 5000):
            self.assertIsNone(parse_remote_settings(payload), payload[:30])


class ApplyTests(unittest.TestCase):
    def make(self, allowed=True):
        observer = observer_with(agv_state())
        reporter = StateReporter(observer, "amr-1", publish_state=False, publish_opcua=False)
        zone = interlock(observer, enabled=False)
        return RemoteSettings(zone, reporter, allowed=allowed), reporter, zone

    def test_without_the_host_switch_nothing_is_subscribed_or_applied(self):
        remote, reporter, zone = self.make(allowed=False)
        self.assertEqual(remote.subscriptions(), [])
        self.assertFalse(remote.handles(SETTINGS_TOPIC))
        self.assertEqual(remote.on_message(FakeClient(), message(publishState=True, zoneInterlock=True)), {})
        self.assertFalse(reporter.publish_state)
        self.assertFalse(zone.enabled)

    def test_the_state_reporter_follows_the_server_in_both_directions(self):
        remote, reporter, _ = self.make()
        self.assertEqual(remote.subscriptions(), [(SETTINGS_TOPIC, 1)])
        remote.on_message(None, message(publishState=True, publishOpcua=True))
        self.assertTrue(reporter.publish_state and reporter.publish_opcua)
        remote.on_message(None, message(publishState=False))
        self.assertFalse(reporter.publish_state)
        self.assertTrue(reporter.publish_opcua)

    def test_switching_the_interlock_on_subscribes_to_the_zones_and_off_stops_it(self):
        remote, _, zone = self.make()
        client = FakeClient()
        remote.on_message(client, message(zoneInterlock=True))
        self.assertTrue(zone.enabled)
        self.assertEqual(client.subscribed, [zone.subscriptions()])
        remote.on_message(client, message(zoneInterlock=True))
        self.assertEqual(len(client.subscribed), 1, "already on: no second subscription")
        remote.on_message(client, message(zoneInterlock=False))
        self.assertFalse(zone.enabled)

    def test_the_policy_for_an_unknown_occupancy_changes(self):
        remote, _, zone = self.make()
        remote.on_message(None, message(zoneUnknown="ignore"))
        self.assertFalse(zone._blocked("press"))
        remote.on_message(None, message(zoneUnknown="pause"))
        self.assertTrue(zone._blocked("press"))
        with self.assertRaises(ValueError):
            zone.set_unknown_policy("maybe")

    def test_an_invalid_message_leaves_everything_as_it_was(self):
        remote, reporter, zone = self.make()
        self.assertEqual(remote.on_message(None, message(publishState=True, zoneUnknown="maybe")), {})
        self.assertFalse(reporter.publish_state)
        self.assertFalse(zone.enabled)

    def test_a_missing_part_is_skipped(self):
        remote = RemoteSettings(None, None, allowed=True)
        self.assertEqual(remote.on_message(None, message(publishState=True, zoneInterlock=True)), {"publishState": True, "zoneInterlock": True})


if __name__ == "__main__":
    unittest.main()
