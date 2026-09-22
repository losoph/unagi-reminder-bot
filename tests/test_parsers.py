import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from channel_links import find_channel_username, is_private_invite
from time_parser import parse_reminder_time

TZ = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 9, 22, 21, 30, tzinfo=TZ)  # Tuesday evening


def at(month, day, hour, minute=0, year=2026):
    return datetime(year, month, day, hour, minute, tzinfo=TZ)


class ReminderTimeParserTests(unittest.TestCase):
    def check(self, text, expected):
        self.assertEqual(parse_reminder_time(text, NOW), expected, text)

    def test_day_word_with_bare_hour(self):
        self.check("завтра 11", at(9, 23, 11))
        self.check("Завтра в 11", at(9, 23, 11))
        self.check("завтра в 11:30", at(9, 23, 11, 30))
        self.check("послезавтра в 8", at(9, 24, 8))

    def test_numeric_dates(self):
        self.check("24/09 в 9", at(9, 24, 9))
        self.check("24.09 9:00", at(9, 24, 9))
        self.check("24.09.2026 18:30", at(9, 24, 18, 30))
        self.check("1.10.27 10:15", at(10, 1, 10, 15, year=2027))
        self.check("в 24.09", at(9, 24, 9))

    def test_date_without_year_in_past_rolls_to_next_year(self):
        self.check("10.01 в 12", at(1, 10, 12, year=2027))

    def test_month_names(self):
        self.check("1 октября в 12", at(10, 1, 12))
        self.check("5 мая", at(5, 5, 9, year=2027))
        self.check("3 марта 18:00", at(3, 3, 18, year=2027))

    def test_relative(self):
        self.check("через 2 дня", at(9, 24, 21, 30))
        self.check("через 2 дня в 10", at(9, 24, 10))
        self.check("через 3 часа", at(9, 23, 0, 30))
        self.check("через 20 минут", at(9, 22, 21, 50))
        self.check("через час", at(9, 22, 22, 30))
        self.check("через полчаса", at(9, 22, 22, 0))
        self.check("через неделю", at(9, 29, 21, 30))
        self.check("через месяц", at(10, 22, 21, 30))
        self.check("через пару дней", at(9, 24, 21, 30))

    def test_weekdays(self):
        self.check("в пятницу", at(9, 25, 9))
        self.check("в пятницу в 10", at(9, 25, 10))
        self.check("пн 9:00", at(9, 28, 9))
        # Same weekday means next week, like the «В понедельник» button.
        self.check("во вторник", at(9, 29, 9))

    def test_time_only_rolls_to_tomorrow_when_passed(self):
        self.check("в 18", at(9, 23, 18))
        self.check("23:00", at(9, 22, 23))
        self.check("11", at(9, 23, 11))

    def test_hour_with_day_part(self):
        self.check("завтра в 9 утра", at(9, 23, 9))
        self.check("завтра в 3 дня", at(9, 23, 15))
        self.check("в 7 вечера", at(9, 23, 19))
        self.check("завтра утром", at(9, 23, 9))
        self.check("завтра вечером", at(9, 23, 20))

    def test_explicit_today_in_past_is_returned_for_caller_to_reject(self):
        self.assertLess(parse_reminder_time("сегодня в 9", NOW), NOW)

    def test_rejects_anything_not_fully_understood(self):
        for text in [
            "", "спасибо", "завтра созвон", "25:00", "31.02", "завтра послезавтра",
            "через 2 часа в 10", "через 2 дня 24.09", "в 10 в 11", "через пол дня", "x" * 80,
        ]:
            self.assertIsNone(parse_reminder_time(text, NOW), text)

    def test_result_is_whole_minutes_in_same_timezone(self):
        result = parse_reminder_time("через 1 минуту", NOW.replace(second=42, microsecond=7))
        self.assertEqual((result.second, result.microsecond, result.tzinfo), (0, 0, TZ))


class ChannelLinkTests(unittest.TestCase):
    def test_links(self):
        cases = {
            "https://t.me/durov": "durov",
            "t.me/durov": "durov",
            "http://www.t.me/Durov_Channel": "Durov_Channel",
            "https://t.me/s/durov": "durov",
            "https://t.me/durov/123": "durov",
            "https://t.me/durov?start=abc": "durov",
            "https://telegram.me/durov": "durov",
            "telegram.dog/durov": "durov",
            "tg://resolve?domain=durov": "durov",
            "Глянь канал https://t.me/s/durov/42 — топ": "durov",
            "@durov": "durov",
            "durov": "durov",
        }
        for text, expected in cases.items():
            self.assertEqual(find_channel_username(text), expected, text)

    def test_hidden_text_link_url(self):
        self.assertEqual(find_channel_username("тут", extra_urls=["https://t.me/durov"]), "durov")

    def test_not_a_channel(self):
        for text in [
            "купить молоко", "@dur", "https://t.me/+AbCdEf123", "https://t.me/joinchat/AbCdEf",
            "https://t.me/addstickers/pack", "https://example.com/durov", "https://t.me/c/123/45",
            "почта @durov в тексте", "bad__name",
        ]:
            self.assertIsNone(find_channel_username(text), text)

    def test_private_invite(self):
        self.assertTrue(is_private_invite("https://t.me/+AbCdEf123"))
        self.assertTrue(is_private_invite("t.me/joinchat/AbCdEf"))
        self.assertFalse(is_private_invite("https://t.me/durov"))


if __name__ == "__main__":
    unittest.main()
