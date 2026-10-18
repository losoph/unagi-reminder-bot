"""Unit tests for config.py module (CFG1)."""

from dataclasses import FrozenInstanceError
import logging
import os
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from config import (
    BASE_DIR,
    AppConfig,
    BotConfig,
    ChannelSourceConfig,
    DatabaseConfig,
    DbConfig,
    DigestConfig,
    EmailConfig,
    FeedRankingConfig,
    MainConfig,
    Settings,
    TelegraphConfig,
    TimeConfig,
    WatchdogConfig,
    reset,
    reset_settings,
    settings,
)


class ConfigDefaultsAndParsingTests(unittest.TestCase):
    def setUp(self):
        reset_settings()

    def tearDown(self):
        reset_settings()

    def test_default_values_when_env_empty(self):
        """Verify that all default values match original defaults across all 6 domains."""
        # Use an empty mapping to guarantee no env variables leak from the runner environment
        cfg = settings(env={})
        self.assertIsInstance(cfg, Settings)
        self.assertIsInstance(cfg.feed_ranking, FeedRankingConfig)
        self.assertIsInstance(cfg.channel_source, ChannelSourceConfig)
        self.assertIsInstance(cfg.email, EmailConfig)
        self.assertIsInstance(cfg.telegraph, TelegraphConfig)
        self.assertIsInstance(cfg.watchdog, WatchdogConfig)
        self.assertIsInstance(cfg.time, TimeConfig)
        self.assertIsInstance(cfg.database, DatabaseConfig)
        self.assertIsInstance(cfg.bot, BotConfig)
        self.assertIsInstance(cfg.digest, DigestConfig)

        # 1. Feed / Ranking domain
        fr = cfg.feed_ranking
        self.assertEqual(fr.cache_minutes, 15)
        self.assertEqual(fr.min_interval_seconds, 60)
        self.assertEqual(fr.build_timeout_seconds, 120)
        self.assertEqual(fr.publish_timeout_seconds, 30)
        self.assertEqual(fr.autodelivery_check_seconds, 120)
        self.assertEqual(fr.autodelivery_grace_minutes, 90)
        self.assertEqual(fr.ai_timeout_seconds, 20.0)
        self.assertTrue(fr.ai_summary_enabled)
        self.assertEqual(fr.post_chars, 300)
        self.assertEqual(fr.w_reach, 0.30)
        self.assertEqual(fr.w_fwd, 0.25)
        self.assertEqual(fr.w_reach_rel, 0.15)
        self.assertEqual(fr.w_fwd_rel, 0.15)
        self.assertEqual(fr.w_engagement, 0.10)
        self.assertEqual(fr.max_posts, 50)
        self.assertEqual(fr.ad_min_percentile, 0.75)
        self.assertEqual(fr.ad_min_pool, 4)
        self.assertEqual(fr.cluster_min_text, 60)
        self.assertEqual(fr.prioritize_threshold_per_day, 20)
        self.assertEqual(fr.max_posts_per_channel, 5)
        self.assertEqual(fr.forward_standout_ratio, 1.25)
        self.assertEqual(fr.forward_standout_max_extra, 3)
        self.assertEqual(fr.score_w_fwd, 0.40)
        self.assertEqual(fr.score_w_views, 0.25)
        self.assertEqual(fr.score_w_reactions, 0.15)
        self.assertEqual(fr.score_w_replies, 0.05)
        self.assertEqual(fr.score_w_freshness, 0.15)

        # Helper properties for weights
        self.assertEqual(
            fr.weights,
            {
                "reach_abs": 0.30,
                "fwd_abs": 0.25,
                "reach_rel": 0.15,
                "fwd_rel": 0.15,
                "engagement": 0.10,
            },
        )
        self.assertEqual(
            fr.score_weights,
            {
                "fwd": 0.40,
                "views": 0.25,
                "reactions": 0.15,
                "replies": 0.05,
                "freshness": 0.15,
            },
        )

        # 2. Channel source & scraper domain
        cs = cfg.channel_source
        self.assertEqual(cs.mode, "auto")
        self.assertIsNone(cs.api_id)
        self.assertIsNone(cs.api_hash)
        self.assertIsNone(cs.session_string)
        self.assertTrue(cs.web_fallback)
        self.assertEqual(cs.fetch_limit, 1000)
        self.assertEqual(cs.fetch_concurrency, 5)
        self.assertEqual(cs.web_fetch_max_pages, 15)
        self.assertEqual(cs.web_fetch_page_delay, 0.5)

        # 3. Email domain
        em = cfg.email
        self.assertEqual(em.msmtprc_path, "/run/secrets/msmtprc")
        self.assertEqual(em.smtp_host, "")
        self.assertIsNone(em.smtp_port)
        self.assertEqual(em.smtp_security, "")
        self.assertEqual(em.smtp_user, "")
        self.assertEqual(em.smtp_password, "")
        self.assertEqual(em.smtp_from, "")
        self.assertEqual(em.daily_limit_per_user, 5)
        self.assertEqual(em.daily_limit_per_address, 5)

        # 4. Telegraph domain
        tg = cfg.telegraph
        self.assertIsNone(tg.token)
        self.assertEqual(tg.post_chars, 1500)

        # 5. Watchdog domain
        wd = cfg.watchdog
        self.assertEqual(wd.check_interval_seconds, 300)
        self.assertEqual(wd.cycle_stall_seconds, 7200)
        self.assertEqual(wd.backlog_growth_cycles, 3)
        self.assertEqual(wd.admin_user_id, "")
        self.assertEqual(wd.admin_chat_id, "")
        self.assertEqual(wd.admin_chat_ids, ())

        # 6. Time domain
        tm = cfg.time
        self.assertEqual(tm.app_timezone, "Europe/Moscow")
        self.assertEqual(tm.reschedule_followup_window_minutes, 10)
        self.assertEqual(tm.tz, ZoneInfo("Europe/Moscow"))

        # 7. Database domain
        db = cfg.database
        self.assertEqual(db.db_path, str(BASE_DIR / "bot_data.db"))
        self.assertEqual(db.message_retention_days, 30)
        self.assertEqual(db.subscription_failure_retention_days, 90)
        self.assertEqual(db.digest_post_retention_days, 30)
        self.assertEqual(db.max_error_text_length, 800)
        self.assertEqual(db.channel_backlog_max, 500)
        self.assertEqual(db.channel_delivery_batch_size, 50)
        self.assertEqual(db.max_feed_channels, 100)
        self.assertEqual(db.max_digest_subscriptions, 300)
        self.assertEqual(db.max_user_reminders, 1000)
        self.assertEqual(db.max_user_saved_messages, 2000)

        # 8. Bot domain
        bot = cfg.bot
        self.assertIsNone(bot.token)
        self.assertEqual(bot.username, "")
        self.assertIsNone(bot.proxy)
        self.assertEqual(bot.ready_file_path, "/run/unagi/ready")
        self.assertEqual(bot.expensive_command_cooldown_seconds, 60)
        self.assertEqual(bot.telegram_preview_chars, 300)

        # 9. Digest domain
        dg = cfg.digest
        self.assertEqual(dg.due_batch_size, 100)
        self.assertEqual(dg.backlog_retry_seconds, 5)
        self.assertEqual(dg.check_interval_seconds, 1800)
        self.assertEqual(dg.telegraph_threshold, 4)

    def test_property_aliases(self):
        """Verify section property aliases on Settings."""
        cfg = settings(env={})
        self.assertIs(cfg.feed, cfg.feed_ranking)
        self.assertIs(cfg.ranking, cfg.feed_ranking)
        self.assertIs(cfg.scraper, cfg.channel_source)
        self.assertIs(cfg.reminders, cfg.time)
        self.assertIs(cfg.db, cfg.database)
        self.assertIs(cfg.database_cfg, cfg.database)
        self.assertIs(cfg.main, cfg.bot)
        self.assertIs(cfg.bot_cfg, cfg.bot)
        self.assertIs(DbConfig, DatabaseConfig)
        self.assertIs(MainConfig, BotConfig)
        self.assertIs(AppConfig, BotConfig)

    def test_frozen_immutability(self):
        """Verify that configuration dataclasses are frozen."""
        cfg = settings(env={})
        with self.assertRaises(FrozenInstanceError):
            cfg.feed_ranking.cache_minutes = 999  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            cfg.channel_source.mode = "web"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            cfg.email.smtp_host = "test"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            cfg.database.message_retention_days = 999  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            cfg.bot.expensive_command_cooldown_seconds = 10  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            cfg.digest.due_batch_size = 50  # type: ignore[misc]

    def test_custom_env_parsing(self):
        """Verify parsing of custom values from environment."""
        custom_env = {
            "FEED_CACHE_MINUTES": "45",
            "FEED_MIN_INTERVAL_SECONDS": "30",
            "FEED_BUILD_TIMEOUT_SECONDS": "200",
            "FEED_PUBLISH_TIMEOUT_SECONDS": "45",
            "FEED_AUTODELIVERY_CHECK_SECONDS": "60",
            "FEED_AUTODELIVERY_GRACE_MINUTES": "120",
            "FEED_AI_TIMEOUT_SECONDS": "15.5",
            "FEED_AI_SUMMARY": "off",
            "FEED_POST_CHARS": "500",
            "FEED_W_REACH": "0.5",
            "FEED_W_FWD": "0.3",
            "FEED_W_REACH_REL": "0.1",
            "FEED_W_FWD_REL": "0.05",
            "FEED_W_ENGAGEMENT": "0.05",
            "FEED_MAX_POSTS": "80",
            "FEED_AD_MIN_PERCENTILE": "0.85",
            "FEED_AD_MIN_POOL": "6",
            "FEED_CLUSTER_MIN_TEXT": "100",
            "PRIORITIZE_THRESHOLD_PER_DAY": "50",
            "MAX_POSTS_PER_CHANNEL": "10",
            "FORWARD_STANDOUT_RATIO": "1.5",
            "FORWARD_STANDOUT_MAX_EXTRA": "5",
            "SCORE_W_FWD": "0.50",
            "SCORE_W_VIEWS": "0.20",
            "SCORE_W_REACTIONS": "0.10",
            "SCORE_W_REPLIES": "0.10",
            "SCORE_W_FRESHNESS": "0.10",
            "CHANNEL_SOURCE": "mtproto",
            "TELEGRAM_API_ID": "12345",
            "TELEGRAM_API_HASH": "abcdef",
            "TELEGRAM_USER_SESSION": "session_str",
            "MTPROTO_WEB_FALLBACK": "0",
            "MTPROTO_FETCH_LIMIT": "500",
            "DIGEST_FETCH_CONCURRENCY": "8",
            "WEB_FETCH_MAX_PAGES": "25",
            "WEB_FETCH_PAGE_DELAY": "1.2",
            "MSMTPRC_PATH": "/custom/msmtprc",
            "SMTP_HOST": "mail.example.com",
            "SMTP_PORT": "465",
            "SMTP_SECURITY": "SSL",
            "SMTP_USER": "testuser",
            "SMTP_PASSWORD": "secretpassword",
            "SMTP_FROM": "sender@example.com",
            "EMAIL_DAILY_LIMIT_PER_USER": "10",
            "EMAIL_DAILY_LIMIT_PER_ADDRESS": "15",
            "TELEGRAPH_TOKEN": "tg_token_xyz",
            "TELEGRAPH_POST_CHARS": "2000",
            "WATCHDOG_CHECK_INTERVAL_SECONDS": "600",
            "WATCHDOG_CYCLE_STALL_SECONDS": "3600",
            "WATCHDOG_BACKLOG_GROWTH_CYCLES": "5",
            "ADMIN_USER_ID": "101, 102",
            "ADMIN_CHAT_ID": "201",
            "APP_TIMEZONE": "UTC",
            "RESCHEDULE_FOLLOWUP_WINDOW_MINUTES": "15",
            "DB_PATH": "/custom/path/custom_data.db",
            "MESSAGE_RETENTION_DAYS": "60",
            "SUBSCRIPTION_FAILURE_RETENTION_DAYS": "180",
            "DIGEST_POST_RETENTION_DAYS": "45",
            "MAX_ERROR_TEXT_LENGTH": "1200",
            "CHANNEL_BACKLOG_MAX": "800",
            "CHANNEL_DELIVERY_BATCH_SIZE": "100",
            "MAX_FEED_CHANNELS": "150",
            "MAX_DIGEST_SUBSCRIPTIONS": "400",
            "MAX_USER_REMINDERS": "1500",
            "MAX_USER_SAVED_MESSAGES": "3000",
            "BOT_TOKEN": "custom_token_123",
            "BOT_USERNAME": "@unagi_test_bot",
            "TELEGRAM_PROXY": "socks5://127.0.0.1:9050",
            "READY_FILE_PATH": "/tmp/custom_ready",
            "EXPENSIVE_COMMAND_COOLDOWN_SECONDS": "120",
            "TELEGRAM_PREVIEW_CHARS": "450",
            "DIGEST_DUE_BATCH_SIZE": "250",
            "DIGEST_BACKLOG_RETRY_SECONDS": "15",
            "DIGEST_CHECK_INTERVAL_SECONDS": "900",
            "DIGEST_TELEGRAPH_THRESHOLD": "8",
        }

        with patch.dict(os.environ, custom_env, clear=True):
            cfg = settings()
            fr = cfg.feed_ranking
            self.assertEqual(fr.cache_minutes, 45)
            self.assertEqual(fr.min_interval_seconds, 30)
            self.assertEqual(fr.build_timeout_seconds, 200)
            self.assertEqual(fr.publish_timeout_seconds, 45)
            self.assertEqual(fr.autodelivery_check_seconds, 60)
            self.assertEqual(fr.autodelivery_grace_minutes, 120)
            self.assertEqual(fr.ai_timeout_seconds, 15.5)
            self.assertFalse(fr.ai_summary_enabled)
            self.assertEqual(fr.post_chars, 500)
            self.assertEqual(fr.w_reach, 0.5)
            self.assertEqual(fr.max_posts, 80)
            self.assertEqual(fr.ad_min_percentile, 0.85)
            self.assertEqual(fr.ad_min_pool, 6)
            self.assertEqual(fr.cluster_min_text, 100)
            self.assertEqual(fr.prioritize_threshold_per_day, 50)
            self.assertEqual(fr.max_posts_per_channel, 10)
            self.assertEqual(fr.forward_standout_ratio, 1.5)
            self.assertEqual(fr.forward_standout_max_extra, 5)
            self.assertEqual(fr.score_w_fwd, 0.50)

            cs = cfg.channel_source
            self.assertEqual(cs.mode, "mtproto")
            self.assertEqual(cs.api_id, "12345")
            self.assertEqual(cs.api_hash, "abcdef")
            self.assertEqual(cs.session_string, "session_str")
            self.assertFalse(cs.web_fallback)
            self.assertEqual(cs.fetch_limit, 500)
            self.assertEqual(cs.fetch_concurrency, 8)
            self.assertEqual(cs.web_fetch_max_pages, 25)
            self.assertEqual(cs.web_fetch_page_delay, 1.2)

            em = cfg.email
            self.assertEqual(em.msmtprc_path, "/custom/msmtprc")
            self.assertEqual(em.smtp_host, "mail.example.com")
            self.assertEqual(em.smtp_port, 465)
            self.assertEqual(em.smtp_security, "ssl")
            self.assertEqual(em.smtp_user, "testuser")
            self.assertEqual(em.smtp_password, "secretpassword")
            self.assertEqual(em.smtp_from, "sender@example.com")
            self.assertEqual(em.daily_limit_per_user, 10)
            self.assertEqual(em.daily_limit_per_address, 15)

            tg = cfg.telegraph
            self.assertEqual(tg.token, "tg_token_xyz")
            self.assertEqual(tg.post_chars, 2000)

            wd = cfg.watchdog
            self.assertEqual(wd.check_interval_seconds, 600)
            self.assertEqual(wd.cycle_stall_seconds, 3600)
            self.assertEqual(wd.backlog_growth_cycles, 5)
            self.assertEqual(wd.admin_user_id, "101, 102")
            self.assertEqual(wd.admin_chat_ids, (101, 102))

            tm = cfg.time
            self.assertEqual(tm.app_timezone, "UTC")
            self.assertEqual(tm.reschedule_followup_window_minutes, 15)
            self.assertEqual(tm.tz, ZoneInfo("UTC"))

            db = cfg.database
            self.assertEqual(db.db_path, "/custom/path/custom_data.db")
            self.assertEqual(db.message_retention_days, 60)
            self.assertEqual(db.subscription_failure_retention_days, 180)
            self.assertEqual(db.digest_post_retention_days, 45)
            self.assertEqual(db.max_error_text_length, 1200)
            self.assertEqual(db.channel_backlog_max, 800)
            self.assertEqual(db.channel_delivery_batch_size, 100)
            self.assertEqual(db.max_feed_channels, 150)
            self.assertEqual(db.max_digest_subscriptions, 400)
            self.assertEqual(db.max_user_reminders, 1500)
            self.assertEqual(db.max_user_saved_messages, 3000)

            bot = cfg.bot
            self.assertEqual(bot.token, "custom_token_123")
            self.assertEqual(bot.username, "unagi_test_bot")
            self.assertEqual(bot.proxy, "socks5://127.0.0.1:9050")
            self.assertEqual(bot.ready_file_path, "/tmp/custom_ready")
            self.assertEqual(bot.expensive_command_cooldown_seconds, 120)
            self.assertEqual(bot.telegram_preview_chars, 450)

            dg = cfg.digest
            self.assertEqual(dg.due_batch_size, 250)
            self.assertEqual(dg.backlog_retry_seconds, 15)
            self.assertEqual(dg.check_interval_seconds, 900)
            self.assertEqual(dg.telegraph_threshold, 8)

    def test_clamping_and_bounds(self):
        """Verify that min values and clamping are preserved as in original modules."""
        env_with_low_values = {
            "FEED_AI_TIMEOUT_SECONDS": "0.001",
            "FEED_POST_CHARS": "-5",
            "FEED_MAX_POSTS": "-10",
            "FEED_AD_MIN_POOL": "0",
            "FEED_CLUSTER_MIN_TEXT": "-20",
            "MTPROTO_FETCH_LIMIT": "5",
            "WATCHDOG_CHECK_INTERVAL_SECONDS": "2",
            "WATCHDOG_CYCLE_STALL_SECONDS": "10",
            "WATCHDOG_BACKLOG_GROWTH_CYCLES": "1",
            "DIGEST_DUE_BATCH_SIZE": "-5",
            "DIGEST_BACKLOG_RETRY_SECONDS": "0",
        }
        cfg = settings(env=env_with_low_values)
        self.assertEqual(cfg.feed_ranking.ai_timeout_seconds, 0.1)
        self.assertEqual(cfg.feed_ranking.post_chars, 1)
        self.assertEqual(cfg.feed_ranking.max_posts, 0)
        self.assertEqual(cfg.feed_ranking.ad_min_pool, 1)
        self.assertEqual(cfg.feed_ranking.cluster_min_text, 0)
        self.assertEqual(cfg.channel_source.fetch_limit, 20)
        self.assertEqual(cfg.watchdog.check_interval_seconds, 10)
        self.assertEqual(cfg.watchdog.cycle_stall_seconds, 60)
        self.assertEqual(cfg.watchdog.backlog_growth_cycles, 2)
        self.assertEqual(cfg.digest.due_batch_size, 1)
        self.assertEqual(cfg.digest.backlog_retry_seconds, 1)

    def test_telegram_proxy_fallback(self):
        """PROXY_URL is used when TELEGRAM_PROXY is not set; TELEGRAM_PROXY takes precedence."""
        cfg1 = settings(env={"PROXY_URL": "http://proxy.fallback:8080"})
        self.assertEqual(cfg1.bot.proxy, "http://proxy.fallback:8080")

        cfg2 = settings(
            env={
                "TELEGRAM_PROXY": "http://telegram.proxy:9000",
                "PROXY_URL": "http://proxy.fallback:8080",
            }
        )
        self.assertEqual(cfg2.bot.proxy, "http://telegram.proxy:9000")

    def test_invalid_channel_source_raises(self):
        """CHANNEL_SOURCE outside of {auto, mtproto, web} must raise ValueError."""
        with self.assertRaises(ValueError) as cm:
            settings(env={"CHANNEL_SOURCE": "invalid_mode"})
        self.assertIn("CHANNEL_SOURCE must be one of: auto, mtproto, web", str(cm.exception))

    def test_admin_ids_parsing_and_warning(self):
        """Admin IDs should parse comma-separated ints and warn on invalid entries."""
        with self.assertLogs("config", level=logging.WARNING) as cm:
            cfg = settings(env={"ADMIN_USER_ID": "100, bad_id, 200", "ADMIN_CHAT_ID": ""})
            self.assertEqual(cfg.watchdog.admin_chat_ids, (100, 200))
        self.assertTrue(any("Некорректный ID администратора в конфиге: bad_id" in msg for msg in cm.output))

    def test_admin_chat_id_fallback(self):
        """ADMIN_CHAT_ID is used when ADMIN_USER_ID is empty."""
        cfg = settings(env={"ADMIN_USER_ID": "", "ADMIN_CHAT_ID": "999"})
        self.assertEqual(cfg.watchdog.admin_chat_ids, (999,))

    def test_lru_cache_and_reset(self):
        """settings() returns identical instance unless env changes or reset is called."""
        with patch.dict(os.environ, {"FEED_CACHE_MINUTES": "15"}):
            s1 = settings()
            s2 = settings()
            self.assertIs(s1, s2)

            # Modify os.environ via patch
            with patch.dict(os.environ, {"FEED_CACHE_MINUTES": "25"}):
                s3 = settings()
                self.assertIsNot(s1, s3)
                self.assertEqual(s3.feed_ranking.cache_minutes, 25)

            # Reverts back
            s4 = settings()
            self.assertIs(s1, s4)

            # Explicit reset clears cache
            reset()
            s5 = settings()
            self.assertEqual(s5.feed_ranking.cache_minutes, 15)

            # Test settings.cache_clear alias
            settings.cache_clear()
            s6 = settings()
            self.assertEqual(s6.feed_ranking.cache_minutes, 15)


if __name__ == "__main__":
    unittest.main()
