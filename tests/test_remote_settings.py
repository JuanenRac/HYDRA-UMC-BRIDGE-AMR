# =============================================================================
# HYDRA-UMC-BRIDGE-AMR - tests of the switches announced by the server (no broker)
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The retained settings of the server: listened to only when the host allows it, validated, and applied to the state reporter and the zone interlock."""

import json
import os
import tempfile
import unittest

from hydra_umc_sdk.signed_commands import sign_command

from hydra_umc_bridge_amr.remote_settings import KEY_ENVIRONMENT, REMOTE_ENVIRONMENT, SETTINGS_TOPIC, RemoteSettings, SettingsVerifier, parse_remote_settings, remote_settings_allowed
from hydra_umc_bridge_amr.state_report import StateReporter

from test_route_telemetry_interlock import agv_state, interlock, observer_with


KEY = b"k" * 64
NOW_MS = 1_800_000_000_000
_counter = [1000]


def message(*, key=KEY, seq=None, issued=NOW_MS, expires=60_000, **settings):
    """A settings message signed like the server signs it; `seq` counts up unless given."""
    if seq is None:
        _counter[0] += 1
        seq = _counter[0]
    body = sign_command(SETTINGS_TOPIC, {"schema": 2, "settings": settings, "seq": seq}, key, now_ms=issued, expires_in_ms=expires)
    return json.dumps(body).encode()


def verifier(key=KEY, now_ms=NOW_MS):
    return SettingsVerifier(key, clock=lambda: now_ms / 1000)


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
        found = parse_remote_settings(message(publishState=True, publishOpcua=False, zoneInterlock=True, zoneUnknown="ignore", other=3), verifier())
        self.assertEqual(found, {"publishState": True, "publishOpcua": False, "zoneInterlock": True, "zoneUnknown": "ignore"})

    def test_anything_else_changes_nothing(self):
        for payload in (b"", b"nope", b"[]", b'{"schema":2,"settings":{}}', message(publishState="yes"), message(zoneUnknown="maybe"), message(zoneUnknown=True), b" " * 5000):
            self.assertIsNone(parse_remote_settings(payload, verifier()), payload[:30])

    def test_an_unsigned_or_old_format_message_is_refused(self):
        for payload in (json.dumps({"schema": 1, "settings": {"boardCode": "required", "publishState": True}}).encode(), json.dumps({"schema": 2, "settings": {"boardCode": "required", "publishState": True}, "seq": 1}).encode()):
            self.assertIsNone(parse_remote_settings(payload, verifier()))

    def test_without_a_verifier_nothing_is_accepted(self):
        self.assertIsNone(parse_remote_settings(message(publishState=True)))

    def test_a_signature_made_with_another_key_or_altered_afterwards_is_refused(self):
        self.assertIsNone(parse_remote_settings(message(key=b'z' * 64, publishState=True), verifier()))
        forged = json.loads(message(publishState=True))
        forged["settings"] = {"x": 1}
        self.assertIsNone(parse_remote_settings(json.dumps(forged).encode(), verifier()))

    def test_an_expired_message_is_refused_and_a_fresh_one_is_accepted(self):
        self.assertIsNone(parse_remote_settings(message(expires=1_000, publishState=True), verifier(now_ms=NOW_MS + 5_000)))
        self.assertEqual(parse_remote_settings(message(expires=60_000, publishState=True), verifier(now_ms=NOW_MS + 5_000)), {"publishState": True})

    def test_a_message_from_the_future_or_asking_too_long_a_life_is_refused(self):
        self.assertIsNone(parse_remote_settings(message(issued=NOW_MS + 60_000, publishState=True), verifier()))
        self.assertIsNone(parse_remote_settings(message(expires=3_600_000, publishState=True), verifier()))

    def test_an_older_message_is_a_replay_and_the_same_one_again_changes_nothing_new(self):
        judge = verifier()
        newer, older = message(seq=50, publishState=True), message(seq=49, publishState=True)
        self.assertEqual(parse_remote_settings(newer, judge), {"publishState": True})
        self.assertIsNone(parse_remote_settings(older, judge))
        self.assertEqual(parse_remote_settings(newer, judge), {"publishState": True})

    def test_the_key_file_is_the_only_source_of_the_key(self):
        self.assertFalse(SettingsVerifier.from_environment({}).usable)
        self.assertFalse(SettingsVerifier.from_environment({KEY_ENVIRONMENT: os.path.join(tempfile.gettempdir(), "no-such-key-file")}).usable)
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "settings.key")
            with open(path, "wb") as handle:
                handle.write(KEY + b"\n")
            os.chmod(path, 0o600)
            self.assertTrue(SettingsVerifier.from_environment({KEY_ENVIRONMENT: path}).usable)
            with open(path, "wb") as handle:
                handle.write(b"short")
            self.assertFalse(SettingsVerifier.from_environment({KEY_ENVIRONMENT: path}).usable)


class ApplyTests(unittest.TestCase):
    def make(self, allowed=True):
        observer = observer_with(agv_state())
        reporter = StateReporter(observer, "amr-1", publish_state=False, publish_opcua=False)
        zone = interlock(observer, enabled=False)
        return RemoteSettings(zone, reporter, allowed=allowed, verifier=verifier()), reporter, zone

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

    def test_with_the_host_switch_but_no_key_nothing_is_subscribed_or_applied(self):
        observer = observer_with(agv_state())
        reporter = StateReporter(observer, "amr-1", publish_state=False, publish_opcua=False)
        remote = RemoteSettings(None, reporter, allowed=True, verifier=SettingsVerifier(None))
        self.assertEqual(remote.subscriptions(), [])
        self.assertEqual(remote.on_message(None, message(publishState=True)), {})
        self.assertFalse(reporter.publish_state)

    def test_a_replayed_message_does_not_undo_a_newer_one(self):
        remote, reporter, _ = self.make()
        self.assertEqual(remote.on_message(None, message(seq=10, publishState=True)), {"publishState": True})
        self.assertEqual(remote.on_message(None, message(seq=9, publishState=False)), {})
        self.assertTrue(reporter.publish_state)

    def test_a_missing_part_is_skipped(self):
        remote = RemoteSettings(None, None, allowed=True, verifier=verifier())
        self.assertEqual(remote.on_message(None, message(publishState=True, zoneInterlock=True)), {"publishState": True, "zoneInterlock": True})


if __name__ == "__main__":
    unittest.main()
