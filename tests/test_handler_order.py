"""tools/handler_order.py: сверка порядка диспетчеризации хендлеров при распиле main.py."""

import importlib.util
import unittest
from pathlib import Path

from aiogram import F, Dispatcher, Router
from aiogram.filters import Command

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("handler_order", ROOT / "tools" / "handler_order.py")
handler_order = importlib.util.module_from_spec(spec)
spec.loader.exec_module(handler_order)


async def noop(*_args):
    return None


def named(name):
    async def handler(*_args):
        return None

    handler.__name__ = name
    return handler


def build(moved_filters=(), kept_filters=()):
    """dp с «оставшимися» хендлерами; moved — во вложенном роутере (как после распила)."""
    dp = Dispatcher()
    for name, flt in kept_filters:
        dp.callback_query(flt)(named(name))
    if moved_filters:
        sub = Router(name="bookmarks")
        for name, flt in moved_filters:
            sub.callback_query(flt)(named(name))
        dp.include_router(sub)
    return dp


class HandlerOrderTests(unittest.TestCase):
    def test_describe_filter_extracts_literals(self):
        dp = Dispatcher()
        dp.callback_query(F.data == "menu_kb")(named("a"))
        dp.callback_query(F.data.startswith("bkdel_"))(named("b"))
        dp.callback_query(F.data.in_({"x", "y"}))(named("c"))
        dp.message(Command("save", "s"))(named("d"))
        rows = {row[2]: row[3] for row in handler_order.collect(dp)}
        self.assertEqual(handler_order.literals(rows["a"]), ["menu_kb"])
        self.assertEqual(handler_order.literals(rows["b"]), ["bkdel_"])
        self.assertEqual(handler_order.literals(rows["c"]), ["x", "y"])
        self.assertEqual(handler_order.literals(rows["d"]), ["save", "s"])

    def test_collect_follows_dispatch_order_and_router_paths(self):
        dp = build(moved_filters=[("m", F.data == "m")], kept_filters=[("k", F.data == "k")])
        callbacks = [(row[1], row[2]) for row in handler_order.collect(dp) if row[0] == "callback_query"]  # у Dispatcher есть и служебный хендлер update
        self.assertEqual(callbacks, [("dp", "k"), ("dp/bookmarks", "m")])

    def test_disjoint_literals_after_a_move_do_not_conflict(self):
        before = handler_order.collect(build(kept_filters=[("m", F.data == "bkdel_1"), ("k", F.data == "other")]))
        after = handler_order.collect(build(moved_filters=[("m", F.data == "bkdel_1")], kept_filters=[("k", F.data == "other")]))
        report, failed = handler_order.compare(before, after)
        self.assertFalse(failed, report)
        self.assertIn("перенесено в другой роутер: 1", report)

    def test_overlapping_prefix_after_a_move_is_reported(self):
        before = handler_order.collect(build(kept_filters=[("m", F.data.startswith("bk_")), ("k", F.data.startswith("bk_extra"))]))
        after = handler_order.collect(build(moved_filters=[("m", F.data.startswith("bk_"))], kept_filters=[("k", F.data.startswith("bk_extra"))]))
        report, failed = handler_order.compare(before, after)
        self.assertTrue(failed)
        self.assertTrue(any("пересекаются литералы" in line for line in report), report)

    def test_lost_handler_is_reported(self):
        before = handler_order.collect(build(kept_filters=[("a", F.data == "a"), ("b", F.data == "b")]))
        after = handler_order.collect(build(kept_filters=[("a", F.data == "a")]))
        report, failed = handler_order.compare(before, after)
        self.assertTrue(failed)
        self.assertTrue(report[0].startswith("НАБОР ХЕНДЛЕРОВ ИЗМЕНИЛСЯ"), report)

    def test_in_filter_with_unrelated_values_does_not_conflict(self):
        before = handler_order.collect(build(kept_filters=[("m", F.data == "menu_kb"), ("k", F.data.in_({"snooze_1h", "snooze_3h"}))]))
        after = handler_order.collect(build(moved_filters=[("m", F.data == "menu_kb")], kept_filters=[("k", F.data.in_({"snooze_1h", "snooze_3h"}))]))
        _report, failed = handler_order.compare(before, after)
        self.assertFalse(failed)


if __name__ == "__main__":
    unittest.main()
