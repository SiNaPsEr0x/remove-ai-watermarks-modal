import unittest
from unittest.mock import patch

import modal_app as m
from tests.helpers import MemoryStore


class AuditRetentionTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.store_patch = patch.object(m, "audit_store", self.store)
        self.store_patch.start()
        self.old_cursor = m._event_slot_cursor

    def tearDown(self):
        m._event_slot_cursor = self.old_cursor
        self.store_patch.stop()

    def test_failed_login_events_stay_bounded_without_admin_read(self):
        with patch.object(m, "ACTIVITY_STORE_LIMIT", 3):
            m._event_slot_cursor = 0
            for i in range(20):
                m.record_event("login_failed", username=f"user{i}")
        keys = [key for key in self.store.keys() if key.startswith(m.EVENT_RING_PREFIX)]
        self.assertEqual(3, len(keys))

    def test_legacy_events_remain_visible_but_are_not_extended(self):
        self.store["event:legacy"] = {"epoch": 1.0, "action": "legacy"}
        with patch.object(m, "ACTIVITY_STORE_LIMIT", 2):
            m._event_slot_cursor = 0
            m.record_event("new")
        legacy_keys = [key for key in self.store.keys() if key.startswith(m.LEGACY_EVENT_PREFIX)]
        self.assertEqual(["event:legacy"], legacy_keys)
        self.assertIn("legacy", [row.get("action") for row in m.recent_events(10)])

    def test_non_event_audit_records_are_preserved(self):
        self.store["session:abc"] = {"session_id": "abc"}
        self.store["geo:203.0.113.1"] = {"country_code": "IT"}
        with patch.object(m, "ACTIVITY_STORE_LIMIT", 2):
            m._event_slot_cursor = 0
            for _ in range(10):
                m.record_event("x")
        self.assertIn("session:abc", self.store.keys())
        self.assertIn("geo:203.0.113.1", self.store.keys())

    def test_recent_events_returns_newest_ring_entries(self):
        self.store["event-ring:0000"] = {"epoch": 1.0, "action": "old"}
        self.store["event-ring:0001"] = {"epoch": 3.0, "action": "newest"}
        self.store["event-ring:0002"] = {"epoch": 2.0, "action": "middle"}
        self.assertEqual(["newest", "middle"], [row["action"] for row in m.recent_events(2)])


if __name__ == "__main__":
    unittest.main()
