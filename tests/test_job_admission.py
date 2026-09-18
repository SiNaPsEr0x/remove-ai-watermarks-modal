import unittest
from unittest.mock import patch

import modal_app as m
from tests.helpers import MemoryStore


class JobAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.store_patch = patch.object(m, "job_admission_store", self.store)
        self.store_patch.start()

    def tearDown(self):
        self.store_patch.stop()

    def test_same_user_cannot_exceed_active_limit(self):
        first, rejected = m.reserve_job_admission("alice")
        self.assertIsNotNone(first)
        self.assertIsNone(rejected)
        second, rejected = m.reserve_job_admission("alice")
        self.assertIsNone(second)
        self.assertEqual("user", rejected)

    def test_global_limit_rejects_other_users_without_leaking_user_slot(self):
        reservations = []
        for i in range(m.MAX_ACTIVE_JOBS_GLOBAL):
            reservation, rejected = m.reserve_job_admission(f"user{i}")
            self.assertIsNotNone(reservation)
            self.assertIsNone(rejected)
            reservations.append(reservation)
        overflow, rejected = m.reserve_job_admission("overflow")
        self.assertIsNone(overflow)
        self.assertEqual("global", rejected)
        self.assertNotIn("user:overflow:0", self.store.keys())

    def test_release_makes_capacity_reusable(self):
        first, _ = m.reserve_job_admission("alice")
        self.assertIsNotNone(first)
        m.release_job_admission(first)
        second, rejected = m.reserve_job_admission("alice")
        self.assertIsNotNone(second)
        self.assertIsNone(rejected)

    def test_stale_reservation_is_reclaimed_after_lease(self):
        self.store["user:alice:0"] = {"id": "old", "username": "alice", "created_at": 0.0}
        with patch.object(m, "JOB_ADMISSION_LEASE_SECONDS", 10), patch.object(m.time, "time", return_value=100.0):
            reservation, rejected = m.reserve_job_admission("alice")
        self.assertIsNotNone(reservation)
        self.assertIsNone(rejected)
        self.assertNotEqual("old", self.store.get("user:alice:0")["id"])


if __name__ == "__main__":
    unittest.main()
