# =============================================================================
# HYDRA-UMC-BRIDGE-AMR - Observer: the factsheet of the vehicle
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The factsheet is off by default, belongs to one vehicle and never turns "unknown" into a yes."""

import json
import os
import unittest
from unittest import mock

from hydra_umc_bridge_amr import Vda5050Target
from hydra_umc_bridge_amr.observer import FACTSHEET_ENV, MAX_MESSAGE_BYTES_ENV, Vda5050Observer

TARGET = Vda5050Target("vda5050", "v2", "hydra-umc", "amr-1")


def sheet(types=("pick", "drop"), **extra):
    body = {
        "manufacturer": "hydra-umc",
        "serialNumber": "amr-1",
        "protocolFeatures": {"agvActions": [{"actionType": t, "actionScopes": ["NODE"]} for t in types]},
    }
    body.update(extra)
    return json.dumps(body).encode()


class FactsheetTests(unittest.TestCase):
    def make(self, **kwargs):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(FACTSHEET_ENV, None)
            os.environ.pop(MAX_MESSAGE_BYTES_ENV, None)
            return Vda5050Observer(TARGET, **kwargs)

    def test_off_by_default_the_subscriptions_and_the_answers_are_unchanged(self):
        observer = self.make()
        self.assertEqual(observer.subscriptions(), [(observer.connection_topic, 1), (observer.state_topic, 0)])
        observer.on_message(observer.factsheet_topic, sheet())
        self.assertIsNone(observer.factsheet)
        self.assertIsNone(observer.supports_action("pick"))

    def test_the_environment_switches_it_on_only_with_1(self):
        for raw, expected in (("1", True), ("0", False), ("true", False), ("", False)):
            with mock.patch.dict(os.environ, {FACTSHEET_ENV: raw}):
                observer = Vda5050Observer(TARGET)
            self.assertEqual((observer.factsheet_topic, 1) in observer.subscriptions(), expected, raw)

    def test_on_it_subscribes_with_qos_1_and_keeps_the_action_types(self):
        observer = self.make(factsheet=True)
        self.assertEqual(observer.subscriptions()[-1], ("vda5050/v2/hydra-umc/amr-1/factsheet", 1))
        observer.on_message(observer.factsheet_topic, sheet(("pick", "drop", "cancelOrder")))
        self.assertEqual(observer.supported_action_types, frozenset({"pick", "drop", "cancelOrder"}))
        self.assertIs(observer.supports_action("pick"), True)
        self.assertIs(observer.supports_action("detectObject"), False)

    def test_no_factsheet_or_one_without_actions_is_unknown_not_a_yes(self):
        observer = self.make(factsheet=True)
        self.assertIsNone(observer.supports_action("pick"))
        observer.on_message(observer.factsheet_topic, sheet(()))
        self.assertIsNone(observer.supported_action_types)
        self.assertIsNone(observer.supports_action("pick"))
        observer.on_message(observer.factsheet_topic, json.dumps({"manufacturer": "hydra-umc", "serialNumber": "amr-1", "protocolFeatures": {"agvActions": "all"}}).encode())
        self.assertIsNone(observer.supports_action("pick"))

    def test_another_vehicle_unreadable_and_oversized_messages_are_ignored(self):
        observer = self.make(factsheet=True, max_message_bytes=200)
        observer.on_message(observer.factsheet_topic, sheet(serialNumber="amr-2"))
        observer.on_message(observer.factsheet_topic, b"{")
        observer.on_message(observer.factsheet_topic, b"[]")
        observer.on_message(observer.factsheet_topic, sheet(("pick",) * 40))
        self.assertIsNone(observer.factsheet)

    def test_the_factsheet_is_a_copy_and_does_not_disturb_the_machine_state(self):
        observer = self.make(factsheet=True)
        observer.on_message(observer.factsheet_topic, sheet())
        copy = observer.factsheet
        copy["protocolFeatures"]["agvActions"].clear()
        self.assertEqual(observer.supported_action_types, frozenset({"pick", "drop"}))
        self.assertEqual(observer.machine_state().name, "OFFLINE")  # a factsheet says nothing about the state


if __name__ == "__main__":
    unittest.main()
