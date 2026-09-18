import unittest
from pathlib import Path
from unittest.mock import patch

import modal_app as m
from tests.helpers import MemoryStore


class JobAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.store_patch = patch.object(m, "job_admission_store", self.store)
        self.store_patch.start()
        self.release_patch = patch.object(m, "JOB_ADMISSION_RELEASE", "test-current-deploy")
        self.release_patch.start()

    def tearDown(self):
        self.release_patch.stop()
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
        self.assertNotIn("release:test-current-deploy:user:overflow:0", self.store.keys())

    def test_release_makes_capacity_reusable(self):
        first, _ = m.reserve_job_admission("alice")
        self.assertIsNotNone(first)
        m.release_job_admission(first)
        second, rejected = m.reserve_job_admission("alice")
        self.assertIsNotNone(second)
        self.assertIsNone(rejected)

    def test_stale_reservation_is_reclaimed_after_lease(self):
        key = "release:test-current-deploy:user:alice:0"
        self.store[key] = {"id": "old", "username": "alice", "created_at": 0.0}
        with patch.object(m, "JOB_ADMISSION_LEASE_SECONDS", 10), patch.object(m.time, "time", return_value=100.0):
            reservation, rejected = m.reserve_job_admission("alice")
        self.assertIsNotNone(reservation)
        self.assertIsNone(rejected)
        self.assertNotEqual("old", self.store.get(key)["id"])

    def test_previous_deploy_slots_do_not_block_current_release(self):
        stale = {"id": "old", "username": "alice", "created_at": 0.0}
        self.store["release:previous-deploy:user:alice:0"] = stale
        self.store["release:previous-deploy:global:0"] = stale

        reservation, rejected = m.reserve_job_admission("alice")

        self.assertIsNotNone(reservation)
        self.assertIsNone(rejected)
        self.assertTrue(reservation["user_slot"].startswith("release:test-current-deploy:"))
        self.assertTrue(reservation["global_slot"].startswith("release:test-current-deploy:"))

    def test_production_admission_is_serialized_by_modal_coordinator(self):
        source = Path(m.__file__).read_text(encoding="utf-8")
        coordinator = source.split("def job_admission_control(", 1)[0]
        coordinator = coordinator.rsplit("@app.function", 1)[1]
        self.assertIn("max_containers=1", coordinator)
        self.assertIn("@modal.concurrent(max_inputs=1)", coordinator)
        self.assertIn("reserve_job_admission_remote(user[\"username\"])", source)
        self.assertIn("release_job_admission_remote(admission)", source)


if __name__ == "__main__":
    unittest.main()
