# =============================================================================
# HYDRA-UMC-BRIDGE-AMR - Observer robustness: invalid, oversized and hostile messages
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""What the observer does with messages it cannot or should not read, without any hardware or broker."""

import json
import os
import unittest
from unittest import mock

from hydra_umc_sdk.bridge_contract import MachineState

from hydra_umc_bridge_amr import Vda5050Target
from hydra_umc_bridge_amr.observer import MAX_MESSAGE_BYTES_ENV, Vda5050Observer

TARGET = Vda5050Target("vda5050", "v2", "hydra-umc", "amr-1")


def good_state(**extra: object) -> dict:
    body = {"manufacturer": "hydra-umc", "serialNumber": "amr-1", "operatingMode": "AUTOMATIC", "driving": False}
    body.update(extra)
    return body


class ObserverRobustnessTests(unittest.TestCase):
    def make(self, **kwargs: object) -> Vda5050Observer:
        self.now = [10.0]
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(MAX_MESSAGE_BYTES_ENV, None)
            return Vda5050Observer(TARGET, clock=lambda: self.now[0], **kwargs)  # type: ignore[arg-type]

    def feed(self, observer: Vda5050Observer, payload: bytes) -> None:
        observer.on_message(observer.state_topic, payload)

    def test_invalid_json_and_non_objects_leave_the_vehicle_unknown(self):
        observer = self.make()
        for payload in (b"", b"{", b"\xff\xfe\x00", b"[]", b"null", b"42", b'"text"'):
            self.feed(observer, payload)
        self.assertEqual(observer.machine_state(), MachineState.OFFLINE)

    def test_deeply_nested_json_is_ignored_not_fatal(self):
        observer = self.make()
        self.feed(observer, b"[" * 200000 + b"]" * 200000)
        self.assertEqual(observer.machine_state(), MachineState.OFFLINE)

    def test_a_good_message_after_garbage_is_still_taken(self):
        observer = self.make()
        self.feed(observer, b"{")
        self.feed(observer, json.dumps(good_state()).encode())
        self.assertEqual(observer.machine_state(), MachineState.IDLE)

    def test_without_a_cap_a_large_state_is_parsed_as_before(self):
        observer = self.make()
        self.feed(observer, json.dumps(good_state(padding="x" * 2_000_000)).encode())
        self.assertEqual(observer.machine_state(), MachineState.IDLE)

    def test_a_configured_cap_ignores_a_larger_message_before_parsing_it(self):
        observer = self.make(max_message_bytes=1024)
        self.feed(observer, json.dumps(good_state(padding="x" * 5000)).encode())
        self.assertEqual(observer.machine_state(), MachineState.OFFLINE)
        self.feed(observer, json.dumps(good_state()).encode())
        self.assertEqual(observer.machine_state(), MachineState.IDLE)

    def test_an_oversized_message_does_not_replace_the_last_good_state(self):
        observer = self.make(max_message_bytes=1024)
        self.feed(observer, json.dumps(good_state()).encode())
        self.feed(observer, json.dumps(good_state(driving=True, padding="x" * 5000)).encode())
        self.assertEqual(observer.machine_state(), MachineState.IDLE)

    def test_the_cap_can_come_from_the_environment(self):
        with mock.patch.dict(os.environ, {MAX_MESSAGE_BYTES_ENV: "512"}):
            observer = Vda5050Observer(TARGET, clock=lambda: 1.0)
        self.feed(observer, json.dumps(good_state(padding="x" * 2000)).encode())
        self.assertEqual(observer.machine_state(), MachineState.OFFLINE)

    def test_a_nonsense_cap_is_refused(self):
        for value in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                self.make(max_message_bytes=value)  # type: ignore[arg-type]
        with mock.patch.dict(os.environ, {MAX_MESSAGE_BYTES_ENV: "many"}):
            with self.assertRaises(ValueError):
                Vda5050Observer(TARGET)


if __name__ == "__main__":
    unittest.main()
