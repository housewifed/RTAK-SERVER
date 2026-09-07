import unittest

from takcore.auth import must_refuse_start


class TestAdminSeed(unittest.TestCase):
    def test_refuse_when_auth_on_no_users_no_password(self):
        self.assertTrue(must_refuse_start(True, "", 0))

    def test_ok_when_password_present(self):
        self.assertFalse(must_refuse_start(True, "s3cret", 0))

    def test_ok_when_users_already_exist(self):
        self.assertFalse(must_refuse_start(True, "", 5))

    def test_ok_when_auth_disabled(self):
        self.assertFalse(must_refuse_start(False, "", 0))


if __name__ == "__main__":
    unittest.main()
