import os
import re
import unittest
from html.parser import HTMLParser

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN_FOR_UNIT_TESTS")

from aiogram.filters import Command  # noqa: E402

import bot_help  # noqa: E402
import main  # noqa: E402


def registered_commands() -> set[str]:
    """Every command name any message handler of the dispatcher (or its routers) reacts to."""
    names: set[str] = set()
    routers = [main.dp]
    while routers:
        router = routers.pop()
        routers.extend(router.sub_routers)
        for handler in router.message.handlers:
            for flt in handler.filters or []:
                if isinstance(flt.callback, Command):
                    names.update(c for c in flt.callback.commands if isinstance(c, str))
    return names


def registered_callbacks() -> list:
    callbacks = []
    routers = [main.dp]
    while routers:
        router = routers.pop()
        routers.extend(router.sub_routers)
        callbacks.extend(router.callback_query.handlers)
    return callbacks


class _Balance(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            self.errors.append(tag)


class HelpTextTests(unittest.TestCase):
    def test_help_fits_one_telegram_message(self):
        self.assertLess(len(bot_help.HELP_TEXT), 4096)
        self.assertLess(len(bot_help.WELCOME_TEXT), 1024)

    def test_help_html_is_balanced(self):
        for text in (bot_help.HELP_TEXT, bot_help.SETTINGS_TEXT):
            checker = _Balance()
            checker.feed(text)
            checker.close()
            self.assertEqual((checker.errors, checker.stack), ([], []))

    def test_every_command_in_help_is_registered(self):
        mentioned = set(re.findall(r"(?<![<\w])/([a-z_]+)", bot_help.HELP_TEXT))
        self.assertTrue(mentioned)
        self.assertEqual(mentioned - registered_commands(), set())

    def test_menu_commands_are_registered_and_documented(self):
        registered = registered_commands()
        for name, description in bot_help.BOT_COMMANDS:
            self.assertRegex(name, r"^[a-z0-9_]{1,32}$")
            self.assertLessEqual(len(description), 256)
            self.assertIn(name, registered)
            self.assertIn(f"/{name}", bot_help.HELP_TEXT)

    def test_new_commands_exist(self):
        self.assertTrue({"settings", "cancel", "help"} <= registered_commands())


class MenuKeyboardTests(unittest.TestCase):
    def keyboards(self):
        return [
            bot_help.build_main_menu_keyboard(),
            bot_help.build_settings_keyboard(True),
            bot_help.build_help_keyboard(),
        ]

    def test_callbacks_fit_and_have_handlers(self):
        handlers = registered_callbacks()

        class FakeCallback:
            def __init__(self, data):
                self.data = data

        for markup in self.keyboards():
            for row in markup.inline_keyboard:
                for button in row:
                    data = button.callback_data
                    self.assertLessEqual(len(data.encode()), 64)
                    # The magic-filter of at least one callback handler accepts this data.
                    matched = False
                    for handler in handlers:
                        for flt in handler.filters or []:
                            try:
                                if flt.callback(FakeCallback(data)) is True:
                                    matched = True
                            except Exception:
                                continue
                    self.assertTrue(matched, data)

    def test_settings_without_email_hides_email_button(self):
        datas = [b.callback_data for row in bot_help.build_settings_keyboard(False).inline_keyboard for b in row]
        self.assertNotIn("email_menu", datas)
        self.assertIn("prefs_menu", datas)


if __name__ == "__main__":
    unittest.main()
