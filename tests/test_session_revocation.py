import unittest
from unittest.mock import patch

import modal_app as m
from tests.helpers import FakeRequest, MemoryStore


class SessionRevocationTests(unittest.TestCase):
    def setUp(self):
        self.users = MemoryStore()
        self.audit = MemoryStore()
        self.users_patch = patch.object(m, "users_store", self.users)
        self.audit_patch = patch.object(m, "audit_store", self.audit)
        self.env_patch = patch.dict(m.os.environ, {"SESSION_SECRET": "s" * 64})
        self.users_patch.start()
        self.audit_patch.start()
        self.env_patch.start()

    def tearDown(self):
        self.env_patch.stop()
        self.audit_patch.stop()
        self.users_patch.stop()

    def add_user(self, username="alice", generation=0, include_generation=True):
        record = {
            "username": username,
            "enabled": True,
            "deleted": False,
            "role": "user",
        }
        if include_generation:
            record["auth_generation"] = generation
        self.users[m.user_key(username)] = record
        return record

    def make_session(self, username="alice", generation=0):
        token = m.create_session_token(username, generation)
        self.assertTrue(m.register_session(token, username, {"ip": "203.0.113.1"}))
        return token

    def test_missing_server_side_session_record_is_rejected(self):
        self.add_user()
        token = m.create_session_token("alice", 0)
        self.assertIsNone(m.current_user_from_request(FakeRequest(token)))

    def test_logout_revokes_only_the_current_session(self):
        self.add_user()
        token1 = self.make_session()
        token2 = self.make_session()
        request1 = FakeRequest(token1)
        request2 = FakeRequest(token2)
        self.assertTrue(m.close_session(request1))
        self.assertIsNone(m.current_user_from_request(request1))
        self.assertIsNotNone(m.current_user_from_request(request2))

    def test_password_reset_invalidates_all_old_user_tokens_and_allows_fresh_login(self):
        record = self.add_user()
        token1 = self.make_session()
        token2 = self.make_session()
        updated = m.reset_user_password_record(record, "new-password-123")
        self.users[m.user_key("alice")] = updated
        self.assertIsNone(m.current_user_from_request(FakeRequest(token1)))
        self.assertIsNone(m.current_user_from_request(FakeRequest(token2)))
        fresh = self.make_session(generation=updated["auth_generation"])
        self.assertIsNotNone(m.current_user_from_request(FakeRequest(fresh)))

    def test_legacy_generation_zero_token_remains_compatible_until_reset(self):
        self.add_user(include_generation=False)
        token = self.make_session(generation=0)
        self.assertIsNotNone(m.current_user_from_request(FakeRequest(token)))


if __name__ == "__main__":
    unittest.main()
