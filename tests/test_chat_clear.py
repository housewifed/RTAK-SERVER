"""Deleting chat history.

Chat is the one table with no retention sweep - positions are pruned after
POSITION_RETENTION_DAYS, chat is not - so without an explicit delete the log
only ever grows. The chat panel's Clear button (admin only) calls
DELETE /api/chat?all=1; DELETE /api/chat?id=<n> removes a single message.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from takcore.store import Store  # noqa: E402


class TestChatDeletion(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        for i in range(3):
            self.store.add_chat(f"USER-{i}", f"uid-{i}", "All Chat Rooms",
                                f"message {i}")

    def test_history_exposes_ids_so_one_message_can_be_removed(self):
        rows = self.store.chat_history()
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(isinstance(r["id"], int) for r in rows))

    def test_delete_one_message(self):
        target = self.store.chat_history()[1]
        self.assertTrue(self.store.delete_chat(target["id"]))
        left = [r["message"] for r in self.store.chat_history()]
        self.assertEqual(left, ["message 0", "message 2"])

    def test_delete_unknown_id_reports_false(self):
        self.assertFalse(self.store.delete_chat(9999))
        self.assertEqual(len(self.store.chat_history()), 3)

    def test_clear_chat_removes_everything_and_counts(self):
        self.assertEqual(self.store.clear_chat(), 3)
        self.assertEqual(self.store.chat_history(), [])

    def test_clear_chat_on_empty_history_is_harmless(self):
        self.store.clear_chat()
        self.assertEqual(self.store.clear_chat(), 0)

    def test_new_messages_still_arrive_after_a_clear(self):
        self.store.clear_chat()
        self.store.add_chat("USER-9", "uid-9", "All Chat Rooms", "after")
        self.assertEqual([r["message"] for r in self.store.chat_history()],
                         ["after"])


if __name__ == "__main__":
    unittest.main()
