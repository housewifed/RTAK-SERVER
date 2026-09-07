"""Tests for password hashing, sessions, and role checks."""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from takcore.auth import (  # noqa: E402
    SessionManager, hash_password, role_allows, verify_password,
)


class TestPassword(unittest.TestCase):
    def test_hash_verify(self):
        h = hash_password("s3cret")
        self.assertTrue(verify_password("s3cret", h))
        self.assertFalse(verify_password("wrong", h))

    def test_salt_is_random(self):
        self.assertNotEqual(hash_password("x"), hash_password("x"))

    def test_bad_stored_format(self):
        self.assertFalse(verify_password("x", "not-a-valid-hash"))


class TestSessions(unittest.TestCase):
    def test_create_get_destroy(self):
        sm = SessionManager()
        tok = sm.create("alice", "admin")
        s = sm.get(tok)
        self.assertEqual(s["username"], "alice")
        self.assertEqual(s["role"], "admin")
        sm.destroy(tok)
        self.assertIsNone(sm.get(tok))

    def test_unknown_token(self):
        self.assertIsNone(SessionManager().get("nope"))
        self.assertIsNone(SessionManager().get(None))

    def test_expiry(self):
        sm = SessionManager()
        tok = sm.create("bob", "viewer")
        sm._sessions[tok]["expires"] = time.time() - 1
        self.assertIsNone(sm.get(tok))


class TestRoles(unittest.TestCase):
    def test_hierarchy(self):
        self.assertTrue(role_allows("admin", "viewer"))
        self.assertTrue(role_allows("admin", "operator"))
        self.assertTrue(role_allows("operator", "viewer"))
        self.assertTrue(role_allows("viewer", "viewer"))
        self.assertFalse(role_allows("viewer", "operator"))
        self.assertFalse(role_allows("operator", "admin"))
        self.assertFalse(role_allows("unknown", "viewer"))


if __name__ == "__main__":
    unittest.main()
