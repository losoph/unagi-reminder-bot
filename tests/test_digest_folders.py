import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN_FOR_UNIT_TESTS")

from data import database  # noqa: E402
import digest_edition  # noqa: E402
import digest_folders  # noqa: E402
import main  # noqa: E402,F401  (регистрирует роутеры)

USER = 501


def _sub(username, period="daily", tag=None):
    return database.add_subscription(
        USER, username, username.title(), period, "2026-01-01 00:00:00", "2026-01-02 00:00:00", tag=tag
    )


class FolderDbTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "folders.db")
        database.init_db()

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_list_folders_orders_saved_then_alphabetical_then_no_folder(self):
        _sub("chan_aa", tag="Zeta")
        _sub("chan_bb", tag="Alpha")
        _sub("chan_cc", tag="Mid")
        _sub("chan_dd")
        database.set_folder_order(USER, ["Mid"])
        names = [f["name"] for f in database.list_subscription_folders(USER)]
        self.assertEqual(names, ["Mid", "Alpha", "Zeta", None])

    def test_pause_and_resume_only_touch_the_folder(self):
        a = _sub("chan_aa", tag="One")
        b = _sub("chan_bb", tag="Two")
        self.assertEqual(database.set_folder_paused(USER, "One", True), 1)
        self.assertTrue(database.get_subscription_by_id(USER, a)["is_paused"])
        self.assertFalse(database.get_subscription_by_id(USER, b)["is_paused"])
        self.assertEqual(database.set_folder_paused(USER, "One", False), 1)

    def test_no_folder_group_is_addressable(self):
        _sub("chan_aa")
        _sub("chan_bb", tag="One")
        self.assertEqual(database.set_folder_paused(USER, None, True), 1)
        self.assertEqual(database.delete_folder_subscriptions(USER, None), 1)
        self.assertEqual(database.count_user_subscriptions(USER), 1)

    def test_move_folder_merges_and_carries_position(self):
        _sub("chan_aa", tag="Old")
        _sub("chan_bb", tag="New")
        database.set_folder_order(USER, ["Other", "Old"])
        self.assertEqual(database.move_folder_subscriptions(USER, "Old", "New"), 1)
        self.assertEqual({f["name"]: f["total"] for f in database.list_subscription_folders(USER)}, {"New": 2})
        self.assertEqual(database.get_folder_order(USER), ["Other", "New"])

    def test_delete_folder_drops_its_order(self):
        _sub("chan_aa", tag="One")
        database.set_folder_order(USER, ["One"])
        database.delete_folder_subscriptions(USER, "One")
        self.assertEqual(database.get_folder_order(USER), [])

    def test_folder_map_skips_unfiled(self):
        a = _sub("chan_aa", tag="One")
        _sub("chan_bb")
        self.assertEqual(database.get_subscription_folder_map(USER), {a: "One"})


class SectionOrderTests(unittest.TestCase):
    def test_sort_sections_by_folder(self):
        sections = [{"sub_id": 1}, {"sub_id": 2}, {"sub_id": 3}, {"sub_id": 4}, {"sub_id": 5}]
        folders = {1: "B", 2: "A", 3: "Z", 4: "C"}
        result = digest_edition.sort_sections_by_folder(sections, folders, ["A", "B"])
        self.assertEqual([s["sub_id"] for s in result], [2, 1, 4, 3, 5])

    def test_period_grouping_keeps_folder_order_inside_period(self):
        sections = [
            {"sub_id": 1, "period": "weekly"},
            {"sub_id": 2, "period": "daily"},
            {"sub_id": 3, "period": "daily"},
        ]
        ordered = digest_edition.sort_sections_by_folder(sections, {1: "A", 2: "B", 3: "A"}, ["A", "B"])
        arranged = digest_edition.arrange_edition_sections(ordered)
        self.assertEqual([s["sub_id"] for s in arranged], [3, 2, 1])


class FolderHandlerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "folders_h.db")
        database.init_db()

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _callback(self, data):
        callback = MagicMock()
        callback.data = data
        callback.from_user.id = USER
        callback.answer = AsyncMock()
        return callback

    def _state(self, names):
        state = MagicMock()
        state.get_data = AsyncMock(return_value={"fld_names": names})
        state.update_data = AsyncMock()
        state.set_state = AsyncMock()
        return state

    def test_move_folder_to_weekly_changes_period_and_keeps_tag(self):
        _sub("chan_aa", "daily", tag="One")
        _sub("chan_bb", "daily", tag="One")
        callback = self._callback("fldwp_0_weekly")
        with patch.object(digest_folders, "edit_or_answer", new=AsyncMock()):
            asyncio.run(digest_folders.move_folder_to_period(callback, self._state(["One"])))
        subs = database.get_folder_subscriptions(USER, "One")
        self.assertEqual({s["period"] for s in subs}, {"weekly"})
        self.assertEqual(len(subs), 2)

    def test_move_to_feed_keeps_tag_and_removes_from_digest(self):
        _sub("chan_aa", "daily", tag="News")
        callback = self._callback("fldfm_0")
        with (
            patch.object(digest_folders, "edit_or_answer", new=AsyncMock()),
            patch("feed.schedule_channel_backfill"),
        ):
            asyncio.run(digest_folders.move_folder_to_feed(callback, self._state(["News"])))
        self.assertEqual(database.count_user_subscriptions(USER), 0)
        feed_subs = database.list_feed_subscriptions(USER)
        self.assertEqual([(s["channel_username"], s["tag"]) for s in feed_subs], [("chan_aa", "News")])

    def test_stale_index_does_not_touch_anything(self):
        _sub("chan_aa", tag="One")
        callback = self._callback("flddy_7")
        with patch.object(digest_folders, "edit_or_answer", new=AsyncMock()):
            asyncio.run(digest_folders.delete_folder(callback, self._state(["One"])))
        self.assertEqual(database.count_user_subscriptions(USER), 1)

    def test_shift_folder_persists_order(self):
        _sub("chan_aa", tag="A")
        _sub("chan_bb", tag="B")
        callback = self._callback("fldv_0")
        state = self._state(["A", "B"])
        with patch.object(digest_folders, "edit_or_answer", new=AsyncMock()):
            asyncio.run(digest_folders.move_folder_down(callback, state))
        self.assertEqual(database.get_folder_order(USER), ["B", "A"])


if __name__ == "__main__":
    unittest.main()
