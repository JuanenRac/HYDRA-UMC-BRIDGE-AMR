# =============================================================================
# HYDRA-UMC-BRIDGE-AMR - Header ids, timestamps, instant cancel and unencodable orders
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
import json
import math
import unittest

from hydra_umc_bridge_amr import AmrDispatch, Vda5050Publisher, Vda5050Target

TARGET = Vda5050Target("vda5050", "v2", "hydra-umc", "amr-1")
MOVE = AmrDispatch(True, "MOVE_TO_DESTINATION", "ok", 1.0, 2.0, "site-a")
CANCEL = AmrDispatch(True, "CANCEL_ORDER", "ok", channel="instantActions")
NOW = "2026-01-01T00:00:00Z"


class Client:
    def __init__(self, rc=0):
        self.published, self.rc = [], rc

    def publish(self, topic, payload):
        self.published.append((topic, payload))
        return type("Info", (), {"rc": self.rc})()


class HeaderIdTests(unittest.TestCase):
    def test_the_publisher_counts_header_ids_separately_for_each_topic(self):
        client, publisher = Client(), Vda5050Publisher(clock=lambda: NOW)
        publisher.publish(client, TARGET, None, None, MOVE)
        publisher.publish(client, TARGET, None, None, MOVE)
        publisher.publish(client, TARGET, None, None, CANCEL)
        ids = [(topic.rsplit("/", 1)[1], json.loads(payload)["headerId"]) for topic, payload in client.published]
        self.assertEqual(ids, [("order", 1), ("order", 2), ("instantActions", 1)])

    def test_an_explicit_header_id_must_be_a_non_negative_integer(self):
        for bad in (-1, True, False, "1", 1.5, None.__class__):
            client = Client()
            result = Vda5050Publisher().publish(client, TARGET, bad, NOW, MOVE)
            self.assertFalse(result.published, repr(bad))
            self.assertEqual(client.published, [])
        self.assertTrue(Vda5050Publisher().publish(Client(), TARGET, 0, NOW, MOVE).published)

    def test_an_explicit_timestamp_must_be_iso_8601_in_utc(self):
        for bad in ("", "yesterday", "2026-01-01T00:00:00", "2026-01-01T00:00:00+02:00", "2026-01-01", 5, ["2026-01-01T00:00:00Z"]):
            client = Client()
            self.assertFalse(Vda5050Publisher().publish(client, TARGET, 1, bad, MOVE).published, repr(bad))
            self.assertEqual(client.published, [])
        for good in ("2026-01-01T00:00:00Z", "2026-01-01T00:00:00.123Z", "2026-01-01T00:00:00+00:00"):
            self.assertTrue(Vda5050Publisher().publish(Client(), TARGET, 1, good, MOVE).published, good)


class IdentifierTests(unittest.TestCase):
    def test_two_publishers_never_reuse_an_order_or_action_id(self):
        first, second = Client(), Client()
        Vda5050Publisher().publish(first, TARGET, 1, NOW, MOVE)
        Vda5050Publisher().publish(second, TARGET, 1, NOW, MOVE)
        a, b = json.loads(first.published[0][1]), json.loads(second.published[0][1])
        self.assertNotEqual(a["orderId"], b["orderId"])
        self.assertNotEqual(a["nodes"][0]["nodeId"], b["nodes"][0]["nodeId"])

    def test_a_named_order_keeps_its_name(self):
        client = Client()
        Vda5050Publisher().publish(client, TARGET, 1, NOW, MOVE, order_id="job-42")
        self.assertEqual(json.loads(client.published[0][1])["orderId"], "job-42")

    def test_a_cancel_travels_on_the_instant_channel_as_a_hard_blocking_action(self):
        client = Client()
        result = Vda5050Publisher().publish(client, TARGET, 1, NOW, CANCEL)
        self.assertTrue(result.published)
        topic, payload = client.published[0]
        self.assertTrue(topic.endswith("/instantActions"))
        action = json.loads(payload)["actions"][0]
        self.assertEqual((action["actionType"], action["blockingType"]), ("cancelOrder", "HARD"))

    def test_an_instant_dispatch_with_no_wire_action_is_refused(self):
        client = Client()
        result = Vda5050Publisher().publish(client, TARGET, 1, NOW, AmrDispatch(True, "MOVE_TO_HOME", "ok", channel="instantActions"))
        self.assertFalse(result.published)
        self.assertEqual(client.published, [])


class UnsendableOrderTests(unittest.TestCase):
    def test_a_coordinate_json_cannot_carry_is_refused_not_sent(self):
        for x, y in ((math.nan, 1.0), (1.0, math.inf), (-math.inf, 0.0)):
            client = Client()
            result = Vda5050Publisher().publish(client, TARGET, 1, NOW, AmrDispatch(True, "MOVE_TO_DESTINATION", "ok", x, y, "site-a"))
            self.assertFalse(result.published, (x, y))
            self.assertEqual(client.published, [])

    def test_a_movement_without_a_map_is_not_a_valid_node_position(self):
        for map_id in (None, ""):
            client = Client()
            result = Vda5050Publisher().publish(client, TARGET, 1, NOW, AmrDispatch(True, "MOVE_TO_DESTINATION", "ok", 1.0, 2.0, map_id))
            self.assertFalse(result.published, repr(map_id))
            self.assertEqual(client.published, [])

    def test_a_publish_the_client_did_not_queue_is_not_reported_as_published(self):
        for rc in (1, 4, 7):
            self.assertFalse(Vda5050Publisher().publish(Client(rc=rc), TARGET, 1, NOW, MOVE).published, rc)

    def test_a_transport_error_is_reported(self):
        class Broken:
            def publish(self, topic, payload):
                raise OSError("socket closed")

        result = Vda5050Publisher().publish(Broken(), TARGET, 1, NOW, MOVE)
        self.assertFalse(result.published)
        self.assertIn("socket closed", result.reason)


if __name__ == "__main__":
    unittest.main()
