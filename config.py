"""Central configuration module for Unagi.

Provides frozen dataclasses grouping application configuration by domain:
- Feed & Ranking (FeedRankingConfig)
- Channel Source & Scraper (ChannelSourceConfig)
- Email (EmailConfig)
- Telegraph (TelegraphConfig)
- Watchdog (WatchdogConfig)
- Time & Timezone (TimeConfig)
- Database (DatabaseConfig)
- Telegram Bot & Core (BotConfig)
- Digest Scheduling & Dispatch (DigestConfig)

Settings are parsed from environment variables upon calling `settings()`.
Results are cached via LRU cache and invalidated when environment variables change
or when `reset_settings()` / `reset()` is called.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
import logging
import os
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent


def _parse_int(val: str | None, default: int, min_val: int | None = None) -> int:
    if val is None or val == "":
        res = default
    else:
        res = int(val)
    if min_val is not None:
        return max(min_val, res)
    return res


def _parse_float(val: str | None, default: float, min_val: float | None = None) -> float:
    if val is None or val == "":
        res = default
    else:
        res = float(val)
    if min_val is not None:
        return max(min_val, res)
    return res


def _parse_bool(val: str | None, default: bool = True) -> bool:
    if val is None or val == "":
        return default
    return val.strip().lower() in {"1", "true", "yes"}


def _parse_admin_ids(admin_user_id: str, admin_chat_id: str) -> tuple[int, ...]:
    raw = admin_user_id or admin_chat_id or ""
    ids: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if part:
            try:
                ids.append(int(part))
            except ValueError:
                logger.warning("Некорректный ID администратора в конфиге: %s", part)
    return tuple(ids)


@dataclass(frozen=True)
class FeedRankingConfig:
    """Settings for feed generation, AI summary, and ranking algorithms."""

    # feed.py
    cache_minutes: int = 15
    min_interval_seconds: int = 60
    build_timeout_seconds: int = 120
    publish_timeout_seconds: int = 30
    autodelivery_check_seconds: int = 120
    autodelivery_grace_minutes: int = 90

    # feed_ai.py
    ai_timeout_seconds: float = 20.0
    ai_summary_enabled: bool = True

    # feed_render.py
    post_chars: int = 300

    # feed_ranking.py weights
    w_reach: float = 0.30
    w_fwd: float = 0.25
    w_reach_rel: float = 0.15
    w_fwd_rel: float = 0.15
    w_engagement: float = 0.10

    # feed_ranking.py limits & thresholds
    max_posts: int = 50
    ad_min_percentile: float = 0.75
    ad_min_pool: int = 4
    cluster_min_text: int = 60

    # ranking.py
    prioritize_threshold_per_day: int = 20
    max_posts_per_channel: int = 5
    forward_standout_ratio: float = 1.25
    forward_standout_max_extra: int = 3
    score_w_fwd: float = 0.40
    score_w_views: float = 0.25
    score_w_reactions: float = 0.15
    score_w_replies: float = 0.05
    score_w_freshness: float = 0.15

    @property
    def weights(self) -> dict[str, float]:
        """Dictionary of weights for feed post ranking."""
        return {
            "reach_abs": self.w_reach,
            "fwd_abs": self.w_fwd,
            "reach_rel": self.w_reach_rel,
            "fwd_rel": self.w_fwd_rel,
            "engagement": self.w_engagement,
        }

    @property
    def score_weights(self) -> dict[str, float]:
        """Dictionary of weights for scoring in ranking.py."""
        return {
            "fwd": self.score_w_fwd,
            "views": self.score_w_views,
            "reactions": self.score_w_reactions,
            "replies": self.score_w_replies,
            "freshness": self.score_w_freshness,
        }


# Aliases for convenience
FeedConfig = FeedRankingConfig
RankingConfig = FeedRankingConfig


@dataclass(frozen=True)
class ChannelSourceConfig:
    """Settings for Telegram channel ingestion (MTProto and Web scraper fallback)."""

    mode: str = "auto"
    api_id: str | None = None
    api_hash: str | None = None
    session_string: str | None = None
    web_fallback: bool = True
    fetch_limit: int = 1000
    fetch_concurrency: int = 5

    # scraper.py
    web_fetch_max_pages: int = 15
    web_fetch_page_delay: float = 0.5


ScraperConfig = ChannelSourceConfig


@dataclass(frozen=True)
class EmailConfig:
    """Settings for SMTP and msmtp email delivery."""

    msmtprc_path: str = "/run/secrets/msmtprc"
    smtp_host: str = ""
    smtp_port: int | None = None
    smtp_security: str = ""
    smtp_user: str = ""
    smtp_password: str = field(default="", repr=False)
    smtp_from: str = ""
    daily_limit_per_user: int = 5
    daily_limit_per_address: int = 5


@dataclass(frozen=True)
class TelegraphConfig:
    """Settings for publishing to Telegraph."""

    token: str | None = field(default=None, repr=False)
    post_chars: int = 1500


@dataclass(frozen=True)
class WatchdogConfig:
    """Settings for system health watchdog loop."""

    check_interval_seconds: int = 300
    cycle_stall_seconds: int = 7200
    backlog_growth_cycles: int = 3
    admin_user_id: str = ""
    admin_chat_id: str = ""
    admin_chat_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class TimeConfig:
    """Settings for timezone and reminder timing."""

    app_timezone: str = "Europe/Moscow"
    reschedule_followup_window_minutes: int = 10

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.app_timezone)


RemindersConfig = TimeConfig


@dataclass(frozen=True)
class DatabaseConfig:
    """Settings for SQLite database storage and data retention quotas."""

    db_path: str = str(BASE_DIR / "bot_data.db")
    message_retention_days: int = 30
    subscription_failure_retention_days: int = 90
    digest_post_retention_days: int = 30
    max_error_text_length: int = 800
    channel_backlog_max: int = 500
    channel_delivery_batch_size: int = 50
    max_feed_channels: int = 100
    max_digest_subscriptions: int = 300
    max_user_reminders: int = 1000
    max_user_saved_messages: int = 2000


DbConfig = DatabaseConfig


@dataclass(frozen=True)
class BotConfig:
    """Settings for Telegram bot core and operational handlers."""

    token: str | None = field(default=None, repr=False)
    username: str = ""
    proxy: str | None = None
    ready_file_path: str = "/run/unagi/ready"
    expensive_command_cooldown_seconds: int = 60
    telegram_preview_chars: int = 300


MainConfig = BotConfig
AppConfig = BotConfig


@dataclass(frozen=True)
class DigestConfig:
    """Settings for digest scheduling loop and dispatch."""

    due_batch_size: int = 100
    backlog_retry_seconds: int = 5
    check_interval_seconds: int = 1800
    telegraph_threshold: int = 4


@dataclass(frozen=True)
class Settings:
    """Composite settings containing all configuration sections."""

    feed_ranking: FeedRankingConfig
    channel_source: ChannelSourceConfig
    email: EmailConfig
    telegraph: TelegraphConfig
    watchdog: WatchdogConfig
    time: TimeConfig
    database: DatabaseConfig
    bot: BotConfig
    digest: DigestConfig

    @property
    def feed(self) -> FeedRankingConfig:
        return self.feed_ranking

    @property
    def ranking(self) -> FeedRankingConfig:
        return self.feed_ranking

    @property
    def scraper(self) -> ChannelSourceConfig:
        return self.channel_source

    @property
    def reminders(self) -> TimeConfig:
        return self.time

    @property
    def db(self) -> DatabaseConfig:
        return self.database

    @property
    def database_cfg(self) -> DatabaseConfig:
        return self.database

    @property
    def main(self) -> BotConfig:
        return self.bot

    @property
    def bot_cfg(self) -> BotConfig:
        return self.bot


def _build_feed_ranking_config(env: Mapping[str, str]) -> FeedRankingConfig:
    ai_summary_raw = env.get("FEED_AI_SUMMARY", "on").strip().lower()
    return FeedRankingConfig(
        cache_minutes=_parse_int(env.get("FEED_CACHE_MINUTES"), 15),
        min_interval_seconds=_parse_int(env.get("FEED_MIN_INTERVAL_SECONDS"), 60),
        build_timeout_seconds=_parse_int(env.get("FEED_BUILD_TIMEOUT_SECONDS"), 120),
        publish_timeout_seconds=_parse_int(env.get("FEED_PUBLISH_TIMEOUT_SECONDS"), 30),
        autodelivery_check_seconds=_parse_int(env.get("FEED_AUTODELIVERY_CHECK_SECONDS"), 120),
        autodelivery_grace_minutes=_parse_int(env.get("FEED_AUTODELIVERY_GRACE_MINUTES"), 90),
        ai_timeout_seconds=_parse_float(env.get("FEED_AI_TIMEOUT_SECONDS"), 20.0, min_val=0.1),
        ai_summary_enabled=(ai_summary_raw != "off"),
        post_chars=_parse_int(env.get("FEED_POST_CHARS"), 300, min_val=1),
        w_reach=_parse_float(env.get("FEED_W_REACH"), 0.30),
        w_fwd=_parse_float(env.get("FEED_W_FWD"), 0.25),
        w_reach_rel=_parse_float(env.get("FEED_W_REACH_REL"), 0.15),
        w_fwd_rel=_parse_float(env.get("FEED_W_FWD_REL"), 0.15),
        w_engagement=_parse_float(env.get("FEED_W_ENGAGEMENT"), 0.10),
        max_posts=_parse_int(env.get("FEED_MAX_POSTS"), 50, min_val=0),
        ad_min_percentile=_parse_float(env.get("FEED_AD_MIN_PERCENTILE"), 0.75),
        ad_min_pool=_parse_int(env.get("FEED_AD_MIN_POOL"), 4, min_val=1),
        cluster_min_text=_parse_int(env.get("FEED_CLUSTER_MIN_TEXT"), 60, min_val=0),
        prioritize_threshold_per_day=_parse_int(env.get("PRIORITIZE_THRESHOLD_PER_DAY"), 20),
        max_posts_per_channel=_parse_int(env.get("MAX_POSTS_PER_CHANNEL"), 5),
        forward_standout_ratio=_parse_float(env.get("FORWARD_STANDOUT_RATIO"), 1.25),
        forward_standout_max_extra=_parse_int(env.get("FORWARD_STANDOUT_MAX_EXTRA"), 3),
        score_w_fwd=_parse_float(env.get("SCORE_W_FWD"), 0.40),
        score_w_views=_parse_float(env.get("SCORE_W_VIEWS"), 0.25),
        score_w_reactions=_parse_float(env.get("SCORE_W_REACTIONS"), 0.15),
        score_w_replies=_parse_float(env.get("SCORE_W_REPLIES"), 0.05),
        score_w_freshness=_parse_float(env.get("SCORE_W_FRESHNESS"), 0.15),
    )


def _build_channel_source_config(env: Mapping[str, str]) -> ChannelSourceConfig:
    mode = env.get("CHANNEL_SOURCE", "auto").strip().lower()
    if mode not in {"auto", "mtproto", "web"}:
        raise ValueError("CHANNEL_SOURCE must be one of: auto, mtproto, web")

    return ChannelSourceConfig(
        mode=mode,
        api_id=env.get("TELEGRAM_API_ID") or None,
        api_hash=env.get("TELEGRAM_API_HASH") or None,
        session_string=env.get("TELEGRAM_USER_SESSION") or None,
        web_fallback=_parse_bool(env.get("MTPROTO_WEB_FALLBACK"), True),
        fetch_limit=_parse_int(env.get("MTPROTO_FETCH_LIMIT"), 1000, min_val=20),
        fetch_concurrency=_parse_int(env.get("DIGEST_FETCH_CONCURRENCY"), 5),
        web_fetch_max_pages=_parse_int(env.get("WEB_FETCH_MAX_PAGES"), 15),
        web_fetch_page_delay=_parse_float(env.get("WEB_FETCH_PAGE_DELAY"), 0.5),
    )


def _build_email_config(env: Mapping[str, str]) -> EmailConfig:
    port_val = env.get("SMTP_PORT")
    port = int(port_val) if port_val and port_val.strip() else None
    return EmailConfig(
        msmtprc_path=env.get("MSMTPRC_PATH", "/run/secrets/msmtprc"),
        smtp_host=env.get("SMTP_HOST", ""),
        smtp_port=port,
        smtp_security=env.get("SMTP_SECURITY", "").strip().lower(),
        smtp_user=env.get("SMTP_USER", ""),
        smtp_password=env.get("SMTP_PASSWORD", ""),
        smtp_from=env.get("SMTP_FROM", ""),
        daily_limit_per_user=_parse_int(env.get("EMAIL_DAILY_LIMIT_PER_USER"), 5),
        daily_limit_per_address=_parse_int(env.get("EMAIL_DAILY_LIMIT_PER_ADDRESS"), 5),
    )


def _build_telegraph_config(env: Mapping[str, str]) -> TelegraphConfig:
    return TelegraphConfig(
        token=env.get("TELEGRAPH_TOKEN") or None,
        post_chars=_parse_int(env.get("TELEGRAPH_POST_CHARS"), 1500),
    )


def _build_watchdog_config(env: Mapping[str, str]) -> WatchdogConfig:
    admin_user_id = env.get("ADMIN_USER_ID", "")
    admin_chat_id = env.get("ADMIN_CHAT_ID", "")
    admin_chat_ids = _parse_admin_ids(admin_user_id, admin_chat_id)
    return WatchdogConfig(
        check_interval_seconds=_parse_int(env.get("WATCHDOG_CHECK_INTERVAL_SECONDS"), 300, min_val=10),
        cycle_stall_seconds=_parse_int(env.get("WATCHDOG_CYCLE_STALL_SECONDS"), 7200, min_val=60),
        backlog_growth_cycles=_parse_int(env.get("WATCHDOG_BACKLOG_GROWTH_CYCLES"), 3, min_val=2),
        admin_user_id=admin_user_id,
        admin_chat_id=admin_chat_id,
        admin_chat_ids=admin_chat_ids,
    )


def _build_time_config(env: Mapping[str, str]) -> TimeConfig:
    return TimeConfig(
        app_timezone=env.get("APP_TIMEZONE", "Europe/Moscow"),
        reschedule_followup_window_minutes=_parse_int(env.get("RESCHEDULE_FOLLOWUP_WINDOW_MINUTES"), 10),
    )


def _build_database_config(env: Mapping[str, str]) -> DatabaseConfig:
    db_path = env.get("DB_PATH") or str(BASE_DIR / "bot_data.db")
    return DatabaseConfig(
        db_path=db_path,
        message_retention_days=_parse_int(env.get("MESSAGE_RETENTION_DAYS"), 30),
        subscription_failure_retention_days=_parse_int(env.get("SUBSCRIPTION_FAILURE_RETENTION_DAYS"), 90),
        digest_post_retention_days=_parse_int(env.get("DIGEST_POST_RETENTION_DAYS"), 30),
        max_error_text_length=_parse_int(env.get("MAX_ERROR_TEXT_LENGTH"), 800),
        channel_backlog_max=_parse_int(env.get("CHANNEL_BACKLOG_MAX"), 500),
        channel_delivery_batch_size=_parse_int(env.get("CHANNEL_DELIVERY_BATCH_SIZE"), 50),
        max_feed_channels=_parse_int(env.get("MAX_FEED_CHANNELS"), 100),
        max_digest_subscriptions=_parse_int(env.get("MAX_DIGEST_SUBSCRIPTIONS"), 300),
        max_user_reminders=_parse_int(env.get("MAX_USER_REMINDERS"), 1000),
        max_user_saved_messages=_parse_int(env.get("MAX_USER_SAVED_MESSAGES"), 2000),
    )


def _build_bot_config(env: Mapping[str, str]) -> BotConfig:
    proxy = env.get("TELEGRAM_PROXY") or env.get("PROXY_URL") or None
    raw_username = env.get("BOT_USERNAME", "")
    username = raw_username.lstrip("@") if raw_username else ""
    return BotConfig(
        token=env.get("BOT_TOKEN") or None,
        username=username,
        proxy=proxy,
        ready_file_path=env.get("READY_FILE_PATH") or "/run/unagi/ready",
        expensive_command_cooldown_seconds=_parse_int(env.get("EXPENSIVE_COMMAND_COOLDOWN_SECONDS"), 60),
        telegram_preview_chars=_parse_int(env.get("TELEGRAM_PREVIEW_CHARS"), 300),
    )


def _build_digest_config(env: Mapping[str, str]) -> DigestConfig:
    return DigestConfig(
        due_batch_size=_parse_int(env.get("DIGEST_DUE_BATCH_SIZE"), 100, min_val=1),
        backlog_retry_seconds=_parse_int(env.get("DIGEST_BACKLOG_RETRY_SECONDS"), 5, min_val=1),
        check_interval_seconds=_parse_int(env.get("DIGEST_CHECK_INTERVAL_SECONDS"), 1800),
        telegraph_threshold=_parse_int(env.get("DIGEST_TELEGRAPH_THRESHOLD"), 4),
    )


@lru_cache(maxsize=128)
def _build_settings_cached(env_items: tuple[tuple[str, str], ...]) -> Settings:
    env = dict(env_items)
    return Settings(
        feed_ranking=_build_feed_ranking_config(env),
        channel_source=_build_channel_source_config(env),
        email=_build_email_config(env),
        telegraph=_build_telegraph_config(env),
        watchdog=_build_watchdog_config(env),
        time=_build_time_config(env),
        database=_build_database_config(env),
        bot=_build_bot_config(env),
        digest=_build_digest_config(env),
    )


def settings(env: Mapping[str, str] | None = None) -> Settings:
    """Factory function returning the application Settings.

    If `env` is None, reads from `os.environ`.
    Results are cached with LRU cache; cache is bypassed/updated when environment
    changes, or reset via `reset_settings()` / `reset()`.
    """
    if env is None:
        items = tuple(sorted(os.environ.items()))
    else:
        items = tuple(sorted(env.items()))
    return _build_settings_cached(items)


def reset_settings() -> None:
    """Clear cached settings."""
    _build_settings_cached.cache_clear()


reset = reset_settings
settings.cache_clear = reset_settings  # type: ignore[attr-defined]
settings.reset = reset_settings  # type: ignore[attr-defined]
