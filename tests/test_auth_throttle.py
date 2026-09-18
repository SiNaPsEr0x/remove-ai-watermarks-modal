import unittest
from unittest.mock import patch

import modal_app as m
from tests.helpers import FakeRequest, MemoryStore


class AuthThrottleTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.store_patch = patch.object(m, "auth_throttle_store", self.store)
        self.store_patch.start()

    def tearDown(self):
        self.store_patch.stop()

    def test_account_limit_blocks_next_attempt(self):
        for _ in range(m.MAX_AUTH_ATTEMPTS_PER_ACCOUNT):
            reservation, limited_by, _ = m.reserve_auth_attempt("alice", "203.0.113.1", now=10.0)
            self.assertIsNotNone(reservation)
            self.assertIsNone(limited_by)
        reservation, limited_by, _ = m.reserve_auth_attempt("alice", "203.0.113.2", now=10.0)
        self.assertIsNone(reservation)
        self.assertEqual("account", limited_by)

    def test_ip_limit_blocks_password_spraying(self):
        for i in range(m.MAX_AUTH_ATTEMPTS_PER_IP):
            reservation, limited_by, _ = m.reserve_auth_attempt(f"user{i}", "203.0.113.9", now=10.0)
            self.assertIsNotNone(reservation)
            self.assertIsNone(limited_by)
        reservation, limited_by, _ = m.reserve_auth_attempt("overflow", "203.0.113.9", now=10.0)
        self.assertIsNone(reservation)
        self.assertEqual("ip", limited_by)

    def test_new_window_allows_attempts_again(self):
        for _ in range(m.MAX_AUTH_ATTEMPTS_PER_ACCOUNT):
            reservation, _, _ = m.reserve_auth_attempt("alice", "203.0.113.1", now=1.0)
            self.assertIsNotNone(reservation)
        blocked, _, _ = m.reserve_auth_attempt("alice", "203.0.113.2", now=1.0)
        self.assertIsNone(blocked)
        reservation, limited_by, _ = m.reserve_auth_attempt(
            "alice", "203.0.113.2", now=m.AUTH_RATE_WINDOW_SECONDS + 1
        )
        self.assertIsNotNone(reservation)
        self.assertIsNone(limited_by)

    def test_success_release_frees_current_slots(self):
        reservation, _, _ = m.reserve_auth_attempt("alice", "203.0.113.1", now=10.0)
        self.assertIsNotNone(reservation)
        m.release_auth_attempt(reservation)
        self.assertNotIn(reservation["account_slot"], self.store.keys())
        self.assertNotIn(reservation["ip_slot"], self.store.keys())

    def test_trusted_client_ip_uses_modal_peer_not_forwarded_header(self):
        request = FakeRequest(
            host="198.51.100.10",
            headers={"x-forwarded-for": "203.0.113.99", "x-real-ip": "203.0.113.98"},
        )
        self.assertEqual("198.51.100.10", m.trusted_client_ip(request))

    def test_release_failure_does_not_raise(self):
        class BrokenStore(MemoryStore):
            def get(self, key, default=None):
                raise RuntimeError("store unavailable")

        with patch.object(m, "auth_throttle_store", BrokenStore()):
            m.release_auth_attempt(
                {"id": "abc", "account_slot": "account:x:0:0", "ip_slot": "ip:x:0:0"}
            )


if __name__ == "__main__":
    unittest.main()
