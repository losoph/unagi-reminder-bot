import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from config import settings

# SEC1 (п.6): файлы БД содержат токен Telegraph, почту и коды подтверждения —
# закрываем их от других локальных пользователей на уровне процесса и файла.
os.umask(0o077)

UTC = timezone.utc
DB_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"
LEGACY_STORAGE_OFFSET_HOURS = 3
MESSAGE_RETENTION_DAYS = settings().database.message_retention_days
SUBSCRIPTION_FAILURE_RETENTION_DAYS = settings().database.subscription_failure_retention_days
DIGEST_POST_RETENTION_DAYS = settings().database.digest_post_retention_days
MAX_ERROR_TEXT_LENGTH = settings().database.max_error_text_length
CHANNEL_BACKLOG_MAX = settings().database.channel_backlog_max
CHANNEL_DELIVERY_BATCH_SIZE = settings().database.channel_delivery_batch_size
MAX_FEED_CHANNELS = settings().database.max_feed_channels
CLEANUP_BATCH_SIZE = 500
# SEC1 (п.2/п.9): квоты на пользователя, чтобы один аккаунт не выел память.
MAX_DIGEST_SUBSCRIPTIONS = settings().database.max_digest_subscriptions
MAX_USER_REMINDERS = settings().database.max_user_reminders
MAX_USER_SAVED_MESSAGES = settings().database.max_user_saved_messages

CHANNEL_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{4,32}$")


def is_valid_channel_username(username: str | None) -> bool:
    """Публичное имя канала: только латиница/цифры/подчёркивание, 4–32 символа."""
    if not username:
        return False
    return bool(CHANNEL_USERNAME_RE.match(username))


BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = settings().database.db_path


_SECURED_DB_PATHS: set[str] = set()


@contextmanager
def get_connection():
    if DB_PATH != ":memory:":
        Path(DB_PATH).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        # Б1/Б2: journal_mode=WAL персистентен в файле и выставляется в init_db();
        # на соединении остаются только пер-соединенческие настройки.
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("PRAGMA cache_size = -16000")
        _secure_db_files_once()
        yield conn
    finally:
        conn.close()


def _secure_db_files() -> None:
    """SEC1 (п.6): закрыть правами 0600 основной файл БД и WAL/SHM-спутники."""
    if DB_PATH == ":memory:":
        return
    for suffix in ("", "-wal", "-shm"):
        path = f"{DB_PATH}{suffix}"
        try:
            if os.path.exists(path):
                os.chmod(path, 0o600)
        except OSError:
            pass
    _SECURED_DB_PATHS.add(DB_PATH)


def _secure_db_files_once() -> None:
    """Не более одного chmod-прохода на путь БД за процесс."""
    if DB_PATH == ":memory:" or DB_PATH in _SECURED_DB_PATHS:
        return
    _secure_db_files()



def serialize_datetime(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("Datetime must be timezone-aware")
    return dt.astimezone(UTC).replace(tzinfo=None).strftime(DB_DATETIME_FORMAT)


def parse_db_datetime(value: str | None) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=UTC)
    return datetime.strptime(value, DB_DATETIME_FORMAT).replace(tzinfo=UTC)


def utc_now() -> datetime:
    return datetime.now(UTC)


def normalize_feed_datetime(val: datetime | str) -> str:
    """Normalize datetime or string to DB_DATETIME_FORMAT UTC string.

    Accepts timezone-aware datetime or string in DB_DATETIME_FORMAT / ISO format.
    Raises ValueError on naive datetime or invalid format.
    """
    if isinstance(val, datetime):
        if val.tzinfo is None:
            raise ValueError(f"Datetime must be timezone-aware: {val}")
        return serialize_datetime(val)
    if isinstance(val, str):
        val_clean = val.strip()
        try:
            dt = datetime.strptime(val_clean, DB_DATETIME_FORMAT).replace(tzinfo=UTC)
            return serialize_datetime(dt)
        except ValueError:
            pass
        try:
            dt = datetime.fromisoformat(val_clean)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return serialize_datetime(dt)
        except ValueError:
            pass
        raise ValueError(f"Invalid datetime format: {val}")
    raise ValueError(f"Expected datetime or str, got {type(val)}")


def normalize_channel_username(channel_username: str) -> str:
    if isinstance(channel_username, str):
        return channel_username.lstrip('@').lower()
    return ""


def truncate_error_text(error_text: str | None) -> str | None:
    if error_text is None:
        return None
    if len(error_text) <= MAX_ERROR_TEXT_LENGTH:
        return error_text
    return error_text[: MAX_ERROR_TEXT_LENGTH - 1] + "…"


def _get_meta(cursor: sqlite3.Cursor, key: str) -> str | None:
    row = cursor.execute("SELECT value FROM app_meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _set_meta(cursor: sqlite3.Cursor, key: str, value: str) -> None:
    cursor.execute(
        """
        INSERT INTO app_meta (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )


def _migrate_legacy_local_timestamps_to_utc(cursor: sqlite3.Cursor) -> None:
    if _get_meta(cursor, "timestamps_storage") == "utc_v1":
        return

    for table_name, columns in (
        ("scheduled_messages", ("send_at", "last_attempt_at")),
        ("subscriptions", ("last_scraped_at", "next_send_at", "last_attempt_at")),
        ("saved_messages", ("saved_at",)),
    ):
        for column_name in columns:
            cursor.execute(
                f"""
                UPDATE {table_name}
                SET {column_name} = datetime({column_name}, ?)
                WHERE {column_name} IS NOT NULL
                """,
                (f"-{LEGACY_STORAGE_OFFSET_HOURS} hours",),
            )

    _set_meta(cursor, "timestamps_storage", "utc_v1")


# SEC2 (п.13): индекс пересоздаётся только при реальном расхождении определения,
# а не на каждом старте. IF NOT EXISTS в sqlite_master не сохраняется, поэтому
# нормализация его отбрасывает.
_ACTIVE_SUBSCRIPTION_INDEX_NAME = "uq_subscriptions_user_channel_period_active"
_ACTIVE_SUBSCRIPTION_INDEX_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_subscriptions_user_channel_period_active
ON subscriptions(user_id, channel_username, period)
WHERE is_disabled = 0
"""


def _normalize_index_sql(sql: str | None) -> str:
    normalized = " ".join((sql or "").split())
    normalized = re.sub(r"\bif\s+not\s+exists\b", "", normalized, flags=re.IGNORECASE)
    return " ".join(normalized.split()).lower()


def _ensure_active_subscription_unique_index(cursor: sqlite3.Cursor) -> bool:
    """Create the partial unique index or repair it if its definition drifted."""
    row = cursor.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
        (_ACTIVE_SUBSCRIPTION_INDEX_NAME,),
    ).fetchone()
    if row and _normalize_index_sql(row[0]) == _normalize_index_sql(_ACTIVE_SUBSCRIPTION_INDEX_SQL):
        return False
    if row:
        cursor.execute(f"DROP INDEX IF EXISTS {_ACTIVE_SUBSCRIPTION_INDEX_NAME}")
    cursor.execute(_ACTIVE_SUBSCRIPTION_INDEX_SQL)
    return True


def _deduplicate_active_subscriptions(cursor: sqlite3.Cursor) -> int:
    duplicate_groups = cursor.execute(
        """
        SELECT user_id, channel_username, period
        FROM subscriptions
        WHERE is_disabled = 0
        GROUP BY user_id, channel_username, period
        HAVING COUNT(*) > 1
        """
    ).fetchall()

    deleted_rows = 0
    for user_id, channel_username, period in duplicate_groups:
        rows = cursor.execute(
            """
            SELECT id, channel_title, created_at, last_scraped_at, next_send_at, digest_status,
                   failure_count, last_error, last_attempt_at
            FROM subscriptions
            WHERE user_id = ? AND channel_username = ? AND period = ? AND is_disabled = 0
            ORDER BY
                COALESCE(last_attempt_at, next_send_at, last_scraped_at, created_at) DESC,
                id DESC
            """,
            (user_id, channel_username, period),
        ).fetchall()

        keep_id, keep_title, keep_created_at, keep_last_scraped_at, keep_next_send_at, _, _, _, keep_last_attempt_at = rows[0]

        for duplicate in rows[1:]:
            _, title, created_at, last_scraped_at, next_send_at, _, _, _, last_attempt_at = duplicate
            if not keep_title and title:
                keep_title = title
            if created_at and (not keep_created_at or created_at < keep_created_at):
                keep_created_at = created_at
            if last_scraped_at and (not keep_last_scraped_at or last_scraped_at > keep_last_scraped_at):
                keep_last_scraped_at = last_scraped_at
            if next_send_at and (not keep_next_send_at or next_send_at > keep_next_send_at):
                keep_next_send_at = next_send_at
            if last_attempt_at and (not keep_last_attempt_at or last_attempt_at > keep_last_attempt_at):
                keep_last_attempt_at = last_attempt_at

        cursor.execute(
            """
            UPDATE subscriptions
            SET channel_title = ?, created_at = ?, last_scraped_at = ?, next_send_at = ?, last_attempt_at = ?
            WHERE id = ?
            """,
            (keep_title, keep_created_at, keep_last_scraped_at, keep_next_send_at, keep_last_attempt_at, keep_id),
        )

        duplicate_ids = [row[0] for row in rows[1:]]
        if duplicate_ids:
            placeholders = ",".join("?" for _ in duplicate_ids)
            cursor.execute(
                f"DELETE FROM subscriptions WHERE id IN ({placeholders})",
                duplicate_ids,
            )
            deleted_rows += len(duplicate_ids)

    return deleted_rows


def _reactivate_legacy_transient_failures(cursor: sqlite3.Cursor) -> int:
    """Undo permanent disables caused by the old five-retry network policy."""
    transient_patterns = (
        "Сетевая ошибка при чтении%",
        "Таймаут при чтении%",
        "%HTTP 429%",
        "%HTTP 500%",
        "%HTTP 502%",
        "%HTTP 503%",
        "%страницу без сообщений%",
        "%пустой ответ%",
    )
    clauses = " OR ".join("s.last_error LIKE ?" for _ in transient_patterns)
    cursor.execute(
        f"""
        UPDATE subscriptions AS s
        SET is_disabled = 0,
            digest_status = 'active',
            failure_count = 0,
            last_error = NULL
        WHERE s.is_disabled = 1
          AND s.digest_status = 'failed_permanent'
          AND ({clauses})
          AND NOT EXISTS (
              SELECT 1
              FROM subscriptions AS active
              WHERE active.user_id = s.user_id
                AND active.channel_username = s.channel_username
                AND active.period = s.period
                AND active.is_disabled = 0
          )
        """,
        transient_patterns,
    )
    return cursor.rowcount


def init_db():
    with get_connection() as conn:
        # Б1: WAL — свойство файла БД, а не отдельного соединения.
        conn.execute("PRAGMA journal_mode = WAL")
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS app_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS scheduled_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                message_id INTEGER,
                delivered_message_id INTEGER,
                send_at DATETIME,
                created_at DATETIME,
                is_sent INTEGER DEFAULT 0,
                text_preview TEXT,
                source_name TEXT,
                delivery_status TEXT DEFAULT 'pending',
                retry_count INTEGER DEFAULT 0,
                last_error TEXT,
                last_attempt_at DATETIME,
                tag TEXT,
                full_text TEXT,
                note TEXT,
                delivered_at DATETIME,
                source_url TEXT
            )
        ''')

        for statement in (
            "ALTER TABLE scheduled_messages ADD COLUMN delivered_message_id INTEGER",
            "ALTER TABLE scheduled_messages ADD COLUMN created_at DATETIME",
            "ALTER TABLE scheduled_messages ADD COLUMN text_preview TEXT",
            "ALTER TABLE scheduled_messages ADD COLUMN source_name TEXT",
            "ALTER TABLE scheduled_messages ADD COLUMN delivery_status TEXT DEFAULT 'pending'",
            "ALTER TABLE scheduled_messages ADD COLUMN retry_count INTEGER DEFAULT 0",
            "ALTER TABLE scheduled_messages ADD COLUMN last_error TEXT",
            "ALTER TABLE scheduled_messages ADD COLUMN last_attempt_at DATETIME",
            "ALTER TABLE scheduled_messages ADD COLUMN tag TEXT",
            "ALTER TABLE scheduled_messages ADD COLUMN full_text TEXT",
            "ALTER TABLE scheduled_messages ADD COLUMN note TEXT",
            "ALTER TABLE scheduled_messages ADD COLUMN delivered_at DATETIME",
            "ALTER TABLE scheduled_messages ADD COLUMN source_url TEXT",
        ):
            try:
                cursor.execute(statement)
            except sqlite3.OperationalError:
                pass

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                channel_username TEXT,
                channel_title TEXT,
                period TEXT,
                created_at DATETIME,
                last_scraped_at DATETIME,
                next_send_at DATETIME,
                digest_status TEXT DEFAULT 'active',
                failure_count INTEGER DEFAULT 0,
                last_error TEXT,
                last_attempt_at DATETIME,
                is_disabled INTEGER DEFAULT 0
            )
        ''')

        for statement in (
            "ALTER TABLE subscriptions ADD COLUMN created_at DATETIME",
            "ALTER TABLE subscriptions ADD COLUMN digest_status TEXT DEFAULT 'active'",
            "ALTER TABLE subscriptions ADD COLUMN failure_count INTEGER DEFAULT 0",
            "ALTER TABLE subscriptions ADD COLUMN last_error TEXT",
            "ALTER TABLE subscriptions ADD COLUMN last_attempt_at DATETIME",
            "ALTER TABLE subscriptions ADD COLUMN is_disabled INTEGER DEFAULT 0",
            "ALTER TABLE subscriptions ADD COLUMN is_paused INTEGER DEFAULT 0",
            "ALTER TABLE subscriptions ADD COLUMN tag TEXT",
            "ALTER TABLE subscriptions ADD COLUMN last_post_id INTEGER",
        ):
            try:
                cursor.execute(statement)
            except sqlite3.OperationalError:
                pass

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS saved_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                full_text TEXT,
                source_name TEXT,
                tag TEXT,
                saved_at DATETIME
            )
        ''')
        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS digest_posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                channel_username TEXT,
                channel_title TEXT,
                post_text TEXT,
                post_link TEXT,
                post_time DATETIME,
                created_at DATETIME
            )
            '''
        )
        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS channel_posts (
                channel_username TEXT NOT NULL,
                post_id INTEGER NOT NULL,
                post_text TEXT,
                post_link TEXT NOT NULL,
                post_time DATETIME NOT NULL,
                source TEXT NOT NULL,
                created_at DATETIME NOT NULL,
                views INTEGER,
                reactions_total INTEGER,
                reactions_json TEXT,
                forwards INTEGER,
                replies INTEGER,
                metrics_at DATETIME,
                fwd_from_channel_id INTEGER,
                fwd_from_post_id INTEGER,
                PRIMARY KEY (channel_username, post_id)
            )
            '''
        )
        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS channels (
                channel_username TEXT PRIMARY KEY,
                subscribers_count INTEGER,
                subscribers_updated_at DATETIME,
                channel_id INTEGER,
                feed_backfilled_at DATETIME
            )
            '''
        )
        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS ai_usage (
                user_id INTEGER,
                usage_day TEXT,
                request_count INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, usage_day)
            )
            '''
        )
        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS telegraph_digests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                url TEXT NOT NULL,
                title TEXT,
                channel_count INTEGER,
                post_count INTEGER,
                created_at DATETIME NOT NULL
            )
            '''
        )
        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS digest_settings (
                user_id INTEGER,
                period TEXT,
                send_hour INTEGER DEFAULT 7,
                send_minute INTEGER DEFAULT 0,
                weekday INTEGER,
                month_day INTEGER,
                monthly_mode TEXT DEFAULT 'date',
                PRIMARY KEY (user_id, period)
            )
            '''
        )
        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS user_emails (
                user_id INTEGER PRIMARY KEY,
                email TEXT,
                email_periods TEXT DEFAULT '',
                pending_email TEXT,
                verify_code TEXT,
                code_sent_at DATETIME,
                code_expires_at DATETIME,
                code_attempts INTEGER DEFAULT 0
            )
            '''
        )
        # SEC1 (п.4): журнал отправок писем для суточных лимитов на пользователя/адрес.
        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS email_send_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                address TEXT NOT NULL,
                sent_at DATETIME NOT NULL
            )
            '''
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_email_send_log_user_time ON email_send_log(user_id, sent_at)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_email_send_log_address_time ON email_send_log(address, sent_at)"
        )
        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS digest_executions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                period TEXT NOT NULL,
                scheduled_at DATETIME NOT NULL,
                started_at DATETIME,
                finished_at DATETIME,
                status TEXT NOT NULL,
                posts_count INTEGER DEFAULT 0,
                channels_count INTEGER DEFAULT 0,
                channels_failed INTEGER DEFAULT 0,
                channels_partial INTEGER DEFAULT 0,
                error_message TEXT,
                retry_count INTEGER DEFAULT 0,
                created_at DATETIME NOT NULL
            )
            '''
        )
        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS user_alert_settings (
                user_id INTEGER PRIMARY KEY,
                alerts_enabled INTEGER NOT NULL DEFAULT 1,
                alerts_paused_until DATETIME,
                last_alert_sent_at DATETIME,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
            '''
        )
        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS digest_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                period TEXT NOT NULL,
                scheduled_at DATETIME NOT NULL,
                alert_type TEXT NOT NULL,
                sent_at DATETIME NOT NULL,
                message_id INTEGER,
                error_text TEXT,
                created_at DATETIME NOT NULL,
                UNIQUE(user_id, period, scheduled_at, alert_type)
            )
            '''
        )
        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS user_prefs (
                user_id INTEGER PRIMARY KEY,
                timezone TEXT,
                morning_hour INTEGER DEFAULT 9,
                day_hour INTEGER DEFAULT 14,
                evening_hour INTEGER DEFAULT 20,
                updated_at DATETIME
            )
            '''
        )
        for statement in (
            "ALTER TABLE user_prefs ADD COLUMN timezone TEXT",
            "ALTER TABLE user_prefs ADD COLUMN morning_hour INTEGER DEFAULT 9",
            "ALTER TABLE user_prefs ADD COLUMN day_hour INTEGER DEFAULT 14",
            "ALTER TABLE user_prefs ADD COLUMN evening_hour INTEGER DEFAULT 20",
            "ALTER TABLE user_prefs ADD COLUMN updated_at DATETIME",
        ):
            try:
                cursor.execute(statement)
            except sqlite3.OperationalError:
                pass

        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS reschedule_undo_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                new_pending_id INTEGER NOT NULL UNIQUE,
                user_id INTEGER NOT NULL,
                old_reminder_id INTEGER NOT NULL,
                message_id INTEGER,
                delivered_message_id INTEGER,
                delivered_at TEXT,
                old_send_at TEXT,
                created_at TEXT,
                text_preview TEXT,
                source_name TEXT,
                tag TEXT,
                full_text TEXT,
                note TEXT,
                source_url TEXT,
                delivery_status TEXT DEFAULT 'sent',
                original_text TEXT,
                original_message_id INTEGER,
                created_timestamp REAL,
                is_undone INTEGER DEFAULT 0
            )
            '''
        )
        cursor.execute(
            '''
            CREATE INDEX IF NOT EXISTS idx_reschedule_undo_lookup
            ON reschedule_undo_history(new_pending_id, user_id)
            '''
        )
        # Early 2026-09-22 builds had a global is_enabled flag; delivery is now per period.
        try:
            cursor.execute("ALTER TABLE user_emails ADD COLUMN email_periods TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass

        for statement in (
            "ALTER TABLE digest_settings ADD COLUMN send_hour INTEGER DEFAULT 7",
            "ALTER TABLE digest_settings ADD COLUMN send_minute INTEGER DEFAULT 0",
            "ALTER TABLE digest_settings ADD COLUMN weekday INTEGER",
            "ALTER TABLE digest_settings ADD COLUMN month_day INTEGER",
            "ALTER TABLE digest_settings ADD COLUMN monthly_mode TEXT DEFAULT 'date'",
            "ALTER TABLE channel_posts ADD COLUMN views INTEGER",
            "ALTER TABLE channel_posts ADD COLUMN reactions_total INTEGER",
            "ALTER TABLE channel_posts ADD COLUMN reactions_json TEXT",
            "ALTER TABLE channel_posts ADD COLUMN forwards INTEGER",
            "ALTER TABLE channel_posts ADD COLUMN replies INTEGER",
            "ALTER TABLE channel_posts ADD COLUMN metrics_at DATETIME",
            "ALTER TABLE channels ADD COLUMN last_scanned_at DATETIME",
            "ALTER TABLE channels ADD COLUMN last_scanned_post_id INTEGER",
            "ALTER TABLE channel_posts ADD COLUMN fwd_from_channel_id INTEGER",
            "ALTER TABLE channel_posts ADD COLUMN fwd_from_post_id INTEGER",
            "ALTER TABLE channels ADD COLUMN channel_id INTEGER",
            "ALTER TABLE channels ADD COLUMN feed_backfilled_at DATETIME",
            # raw_feed для ghsub: '' — медиа нет, NULL — неизвестно (посты до этой миграции).
            "ALTER TABLE channel_posts ADD COLUMN media_kind TEXT",
            "ALTER TABLE channel_posts ADD COLUMN is_forward INTEGER",
            "ALTER TABLE channel_posts ADD COLUMN link_urls TEXT",
            "ALTER TABLE user_alert_settings ADD COLUMN last_alert_sent_at DATETIME",
            "ALTER TABLE user_alert_settings ADD COLUMN consecutive_failures INTEGER DEFAULT 0",
        ):
            try:
                cursor.execute(statement)
            except sqlite3.OperationalError:
                pass

        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_scheduled_messages_due ON scheduled_messages(send_at, is_sent, delivery_status)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_scheduled_messages_user_pending ON scheduled_messages(user_id, is_sent, send_at)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_scheduled_messages_archive_cleanup ON scheduled_messages(is_sent, delivery_status, created_at)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_subscriptions_due ON subscriptions(next_send_at, is_disabled)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_subscriptions_user_active ON subscriptions(user_id, is_disabled, next_send_at)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_subscriptions_failed_cleanup ON subscriptions(is_disabled, digest_status, last_attempt_at)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_saved_messages_user_tag_date ON saved_messages(user_id, tag, saved_at)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_digest_settings_user_period ON digest_settings(user_id, period)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_digest_posts_user_time ON digest_posts(user_id, post_time)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_digest_posts_user_channel ON digest_posts(user_id, channel_username, post_time)"
        )
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_digest_posts_user_link ON digest_posts(user_id, post_link)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_channel_posts_channel_time ON channel_posts(channel_username, post_time)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_telegraph_digests_user_created ON telegraph_digests(user_id, created_at)"
        )
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_digest_executions_user_period_sched ON digest_executions(user_id, period, scheduled_at)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_digest_executions_user_status ON digest_executions(user_id, status)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_digest_executions_sched ON digest_executions(scheduled_at, status)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_digest_alerts_user_sent ON digest_alerts(user_id, sent_at)"
        )
        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS feed_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                channel_username TEXT NOT NULL,
                channel_title TEXT,
                tag TEXT,
                is_paused INTEGER DEFAULT 0,
                created_at DATETIME NOT NULL,
                UNIQUE(user_id, channel_username)
            )
            '''
        )
        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS feed_editions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                period TEXT NOT NULL,
                window_start DATETIME NOT NULL,
                window_end DATETIME NOT NULL,
                subs_hash TEXT NOT NULL,
                telegraph_url TEXT NOT NULL,
                post_count INTEGER,
                channel_count INTEGER,
                created_at DATETIME NOT NULL
            )
            '''
        )
        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS feed_user_state (
                user_id INTEGER PRIMARY KEY,
                onboarding_shown_at DATETIME
            )
            '''
        )
        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS feed_autodelivery (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                period TEXT NOT NULL,
                last_run_at DATETIME,
                created_at DATETIME NOT NULL,
                UNIQUE(user_id, period)
            )
            '''
        )
        for statement in (
            "ALTER TABLE feed_subscriptions ADD COLUMN tag TEXT",
            "ALTER TABLE feed_subscriptions ADD COLUMN is_paused INTEGER DEFAULT 0",
            "ALTER TABLE feed_autodelivery ADD COLUMN send_hour INTEGER",
            "ALTER TABLE feed_autodelivery ADD COLUMN send_minute INTEGER",
            "ALTER TABLE feed_autodelivery ADD COLUMN weekday INTEGER",
            "ALTER TABLE feed_autodelivery ADD COLUMN month_day INTEGER",
            "ALTER TABLE feed_autodelivery ADD COLUMN delivery TEXT DEFAULT 'telegram'",
        ):
            try:
                cursor.execute(statement)
            except sqlite3.OperationalError:
                pass

        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_feed_subscriptions_user ON feed_subscriptions(user_id, is_paused)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_feed_editions_lookup ON feed_editions(user_id, period, subs_hash, created_at)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_feed_autodelivery_user ON feed_autodelivery(user_id)"
        )
        try:
            cursor.execute("ALTER TABLE feed_editions ADD COLUMN headlines TEXT")
        except sqlite3.OperationalError:
            pass  # колонка уже есть

        # Q4 T20 (§3.1): целевая схема данных для модели «папка = одна рассылка»
        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS folders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                is_default INTEGER NOT NULL DEFAULT 0,
                period TEXT NOT NULL,
                times TEXT NOT NULL DEFAULT '["07:00"]',
                send_hour INTEGER NOT NULL DEFAULT 7,
                send_minute INTEGER NOT NULL DEFAULT 0,
                weekdays TEXT,
                month_day INTEGER,
                monthly_mode TEXT DEFAULT 'date',
                delivery TEXT NOT NULL DEFAULT 'telegram',
                is_enabled INTEGER NOT NULL DEFAULT 1,
                next_send_at DATETIME,
                last_edition_id INTEGER,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                UNIQUE(user_id, name),
                CHECK (period IN ('daily', 'weekly', 'monthly')),
                CHECK (send_hour BETWEEN 0 AND 23),
                CHECK (send_minute BETWEEN 0 AND 59),
                CHECK (monthly_mode IN ('date', 'weekday', 'last_day')),
                CHECK (delivery IN ('telegram', 'email'))
            )
            '''
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_folders_due ON folders(is_enabled, next_send_at) WHERE next_send_at IS NOT NULL"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_folders_user ON folders(user_id, is_enabled)"
        )
        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS digest_editions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                folder_id INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL,
                scheduled_at DATETIME NOT NULL,
                status TEXT NOT NULL,
                delivery TEXT NOT NULL DEFAULT 'telegram',
                delivery_target TEXT,
                posts_count INTEGER DEFAULT 0,
                channels_count INTEGER DEFAULT 0,
                channels_failed INTEGER DEFAULT 0,
                channels_partial INTEGER DEFAULT 0,
                error_message TEXT,
                retry_count INTEGER DEFAULT 0,
                telegraph_url TEXT,
                created_at DATETIME NOT NULL,
                started_at DATETIME,
                delivered_at DATETIME,
                UNIQUE(folder_id, scheduled_at)
            )
            '''
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_digest_editions_user_status ON digest_editions(user_id, status)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_digest_editions_sched_status ON digest_editions(scheduled_at, status)"
        )
        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS digest_edition_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                edition_id INTEGER NOT NULL REFERENCES digest_editions(id) ON DELETE CASCADE,
                subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
                channel_username TEXT NOT NULL,
                post_id INTEGER NOT NULL,
                post_time DATETIME NOT NULL,
                post_title TEXT,
                post_text TEXT,
                post_link TEXT NOT NULL,
                score REAL DEFAULT 0.0,
                rank INTEGER,
                created_at DATETIME NOT NULL,
                UNIQUE(edition_id, channel_username, post_id)
            )
            '''
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_edition_items_edition ON digest_edition_items(edition_id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_edition_items_sub ON digest_edition_items(subscription_id)"
        )

        # Q4 T20 (§3.2): модификация существующих таблиц
        for statement in (
            "ALTER TABLE user_prefs ADD COLUMN digest_scheduler_version TEXT DEFAULT 'v1'",
            "ALTER TABLE subscriptions ADD COLUMN folder_id INTEGER REFERENCES folders(id) ON DELETE RESTRICT",
            "ALTER TABLE digest_executions ADD COLUMN folder_id INTEGER REFERENCES folders(id)",
            "ALTER TABLE digest_alerts ADD COLUMN folder_id INTEGER REFERENCES folders(id)",
        ):
            try:
                cursor.execute(statement)
            except sqlite3.OperationalError:
                pass

        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_subscriptions_folder ON subscriptions(folder_id, is_disabled)"
        )
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_digest_executions_folder_slot ON digest_executions(folder_id, scheduled_at) WHERE folder_id IS NOT NULL"
        )
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_digest_alerts_folder_slot ON digest_alerts(folder_id, scheduled_at, alert_type) WHERE folder_id IS NOT NULL"
        )

        _migrate_legacy_local_timestamps_to_utc(cursor)
        cursor.execute(
            """
            UPDATE subscriptions
            SET channel_username = lower(ltrim(channel_username, '@'))
            WHERE channel_username IS NOT NULL
            """
        )
        default_created_at = serialize_datetime(utc_now())
        cursor.execute(
            """
            UPDATE scheduled_messages
            SET created_at = COALESCE(created_at, send_at, last_attempt_at, ?)
            WHERE created_at IS NULL
            """,
            (default_created_at,),
        )
        cursor.execute(
            """
            UPDATE subscriptions
            SET created_at = COALESCE(created_at, last_scraped_at, next_send_at, last_attempt_at, ?)
            WHERE created_at IS NULL
            """,
            (default_created_at,),
        )
        reactivated_count = _reactivate_legacy_transient_failures(cursor)
        if reactivated_count:
            _set_meta(cursor, "reactivated_legacy_transient_failures", str(reactivated_count))
        _deduplicate_active_subscriptions(cursor)
        _ensure_active_subscription_unique_index(cursor)
        conn.commit()
        _secure_db_files()


def add_message(
    user_id,
    message_id,
    send_at,
    text_preview="",
    source_name="",
    tag=None,
    full_text=None,
    note=None,
    source_url=None,
):
    created_at = serialize_datetime(utc_now())
    if full_text is None and text_preview:
        full_text = text_preview
    if not text_preview and full_text:
        normalized = full_text.replace("\n", " ")
        text_preview = normalized[:37] + "..." if len(normalized) > 40 else normalized
    with get_connection() as conn:
        pending = conn.execute(
            "SELECT COUNT(*) FROM scheduled_messages WHERE user_id = ? AND is_sent = 0",
            (user_id,),
        ).fetchone()[0]
        if pending >= MAX_USER_REMINDERS:
            raise ValueError(f"Достигнут лимит напоминаний ({MAX_USER_REMINDERS})")
        cur = conn.execute(
            '''
            INSERT INTO scheduled_messages (
                user_id, message_id, send_at, created_at, text_preview, source_name, tag, full_text, note, source_url, delivery_status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
            ''',
            (user_id, message_id, send_at, created_at, text_preview, source_name, tag, full_text, note, source_url),
        )
        conn.commit()
        return cur.lastrowid


def get_pending_messages(current_time_str):
    with get_connection() as conn:
        rows = conn.execute(
            '''
            SELECT id, user_id, message_id, retry_count, text_preview, source_name, full_text, note
            FROM scheduled_messages
            WHERE send_at <= ? AND is_sent = 0 AND delivery_status != 'failed_permanent'
            ''',
            (current_time_str,),
        ).fetchall()
    return rows


def mark_as_sent(msg_id, delivered_message_id=None, delivered_at=None):
    if delivered_at is None:
        delivered_at = serialize_datetime(utc_now())
    with get_connection() as conn:
        conn.execute(
            '''
            UPDATE scheduled_messages
            SET is_sent = 1, delivered_message_id = ?, delivery_status = 'sent', last_error = NULL, delivered_at = ?
            WHERE id = ?
            ''',
            (delivered_message_id, delivered_at, msg_id),
        )
        conn.commit()


def mark_message_delivery_error(msg_id, error_text, attempted_at, retry_count, is_permanent):
    error_text = truncate_error_text(error_text)
    with get_connection() as conn:
        conn.execute(
            '''
            UPDATE scheduled_messages
            SET delivery_status = ?, retry_count = ?, last_error = ?, last_attempt_at = ?
            WHERE id = ?
            ''',
            (
                'failed_permanent' if is_permanent else 'failed_temporary',
                retry_count,
                error_text,
                attempted_at,
                msg_id,
            ),
        )
        conn.commit()


def get_user_messages(user_id, limit: int | None = None):
    """SEC1 (п.9): выборка ограничена квотой пользователя, а не вычитывается целиком."""
    limit = MAX_USER_REMINDERS if limit is None else max(1, int(limit))
    with get_connection() as conn:
        rows = conn.execute(
            '''
            SELECT id, send_at, text_preview, source_name, tag, message_id, full_text, note, source_url
            FROM scheduled_messages
            WHERE user_id = ? AND is_sent = 0
            ORDER BY send_at
            LIMIT ?
            ''',
            (user_id, limit),
        ).fetchall()
    return rows


def get_reminder_by_id(user_id, msg_id):
    with get_connection() as conn:
        row = conn.execute(
            '''
            SELECT id, send_at, text_preview, source_name, tag, message_id, full_text, note, source_url
            FROM scheduled_messages
            WHERE id = ? AND user_id = ? AND is_sent = 0
            ''',
            (msg_id, user_id),
        ).fetchone()
    return row


def update_reminder_tag(user_id, msg_id, new_tag):
    with get_connection() as conn:
        conn.execute(
            'UPDATE scheduled_messages SET tag = ? WHERE id = ? AND user_id = ? AND is_sent = 0',
            (new_tag, msg_id, user_id),
        )
        conn.commit()


def update_reminder_time(user_id, msg_id, send_at):
    """Reschedule an existing pending reminder in place."""
    with get_connection() as conn:
        cur = conn.execute(
            '''
            UPDATE scheduled_messages
            SET send_at = ?, delivery_status = 'pending', retry_count = 0, last_error = NULL
            WHERE id = ? AND user_id = ? AND is_sent = 0
            ''',
            (send_at, msg_id, user_id),
        )
        conn.commit()
    return cur.rowcount


def delete_message(user_id, msg_id):
    with get_connection() as conn:
        conn.execute(
            'DELETE FROM scheduled_messages WHERE id = ? AND user_id = ?',
            (msg_id, user_id),
        )
        conn.commit()


def replace_sent_reminder_with_pending(
    user_id: int,
    old_db_id: int,
    message_id: int | None,
    send_at: str,
    text_preview: str = "",
    source_name: str = "",
    undo: dict | None = None,
) -> int:
    """Atomically insert a new pending reminder and remove the delivered row.
    If undo is provided (dict with optional original_text, original_message_id),
    saves a snapshot of the old row to reschedule_undo_history before deleting it.
    Any failure in snapshot rolls back the transaction.
    """
    created_at = serialize_datetime(utc_now())
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            old_row_dict = None
            if undo is not None:
                row = conn.execute(
                    "SELECT * FROM scheduled_messages WHERE id = ? AND user_id = ?",
                    (old_db_id, user_id),
                ).fetchone()
                if not row:
                    raise ValueError("Scheduled row missing or already changed")
                old_row_dict = dict(row)

            # Copy tag, full_text, note, source_url from the delivered row so a reschedule keeps them.
            cur = conn.execute(
                """
                INSERT INTO scheduled_messages (
                    user_id, message_id, send_at, created_at, text_preview, source_name, tag, full_text, note, source_url, delivery_status
                )
                SELECT ?, ?, ?, ?, ?, ?, tag, COALESCE(full_text, ?), note, source_url, 'pending'
                FROM scheduled_messages WHERE id = ? AND user_id = ?
                """,
                (user_id, message_id, send_at, created_at, text_preview, source_name, text_preview, old_db_id, user_id),
            )
            new_id = cur.lastrowid
            del_cur = conn.execute(
                "DELETE FROM scheduled_messages WHERE id = ? AND user_id = ?",
                (old_db_id, user_id),
            )
            if del_cur.rowcount != 1:
                raise ValueError("Scheduled row missing or already changed")

            if undo is not None and old_row_dict is not None:
                original_text = undo.get("original_text", "")
                original_message_id = undo.get("original_message_id")
                conn.execute(
                    """
                    INSERT OR REPLACE INTO reschedule_undo_history (
                        new_pending_id, user_id, old_reminder_id, message_id, delivered_message_id,
                        delivered_at, old_send_at, created_at, text_preview, source_name,
                        tag, full_text, note, source_url, delivery_status,
                        original_text, original_message_id, created_timestamp, is_undone
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                    """,
                    (
                        new_id,
                        user_id,
                        old_row_dict["id"],
                        old_row_dict.get("message_id"),
                        old_row_dict.get("delivered_message_id"),
                        old_row_dict.get("delivered_at"),
                        old_row_dict.get("send_at"),
                        old_row_dict.get("created_at"),
                        old_row_dict.get("text_preview"),
                        old_row_dict.get("source_name"),
                        old_row_dict.get("tag"),
                        old_row_dict.get("full_text"),
                        old_row_dict.get("note"),
                        old_row_dict.get("source_url"),
                        old_row_dict.get("delivery_status") or "sent",
                        original_text,
                        original_message_id,
                        datetime.now(UTC).timestamp(),
                    ),
                )

            conn.commit()
            return new_id
        except Exception:
            conn.rollback()
            raise


def update_reminder_note(user_id: int, reminder_id: int, note: str) -> bool:
    with get_connection() as conn:
        cur = conn.execute(
            """
            UPDATE scheduled_messages
            SET note = ?
            WHERE id = ? AND user_id = ? AND is_sent = 0
            """,
            (note, reminder_id, user_id),
        )
        conn.commit()
    return cur.rowcount > 0


def mark_reminder_as_done(user_id: int, db_id: int) -> bool:
    """Marks a delivered reminder as done without deleting it (T16)."""
    with get_connection() as conn:
        cur = conn.execute(
            """
            UPDATE scheduled_messages
            SET delivery_status = 'done'
            WHERE id = ? AND user_id = ?
            """,
            (db_id, user_id),
        )
        conn.commit()
        return cur.rowcount > 0


def get_last_delivered_reminder(user_id: int) -> dict | None:
    """Returns the most recent reminder with delivered_at that is not marked done and not yet deleted."""
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM scheduled_messages
            WHERE user_id = ?
              AND is_sent = 1
              AND delivery_status != 'done'
              AND delivered_at IS NOT NULL
            ORDER BY delivered_at DESC, id DESC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()
        if not row:
            return None
        return dict(row)


def get_recent_delivered_reminder(
    user_id: int,
    window_minutes: int = 10,
    now: datetime | None = None,
) -> dict | None:
    """Returns the most recently delivered reminder if delivered within the last window_minutes."""
    candidate = get_last_delivered_reminder(user_id)
    if not candidate or not candidate.get("delivered_at"):
        return None
    delivered_at_str = candidate["delivered_at"]
    delivered_at_dt = parse_db_datetime(delivered_at_str)
    current_utc = now.astimezone(UTC) if now else utc_now()
    elapsed = (current_utc - delivered_at_dt).total_seconds()
    if 0 <= elapsed <= window_minutes * 60:
        return candidate
    return None


def save_reschedule_undo(
    new_pending_id: int,
    user_id: int,
    old_reminder: dict,
    original_text: str = "",
    original_message_id: int | None = None,
) -> None:
    """Saves a snapshot of the rescheduled reminder before replacing it with pending."""
    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO reschedule_undo_history (
                new_pending_id, user_id, old_reminder_id, message_id, delivered_message_id,
                delivered_at, old_send_at, created_at, text_preview, source_name,
                tag, full_text, note, source_url, delivery_status,
                original_text, original_message_id, created_timestamp, is_undone
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                new_pending_id,
                user_id,
                old_reminder["id"],
                old_reminder.get("message_id"),
                old_reminder.get("delivered_message_id"),
                old_reminder.get("delivered_at"),
                old_reminder.get("send_at"),
                old_reminder.get("created_at"),
                old_reminder.get("text_preview"),
                old_reminder.get("source_name"),
                old_reminder.get("tag"),
                old_reminder.get("full_text"),
                old_reminder.get("note"),
                old_reminder.get("source_url"),
                old_reminder.get("delivery_status") or "sent",
                original_text,
                original_message_id,
                datetime.now(UTC).timestamp(),
            ),
        )
        conn.commit()


def undo_followup_reschedule(user_id: int, new_pending_id: int) -> tuple[bool, str, dict | None]:
    """Atomically undoes a followup reschedule: deletes new pending reminder and restores old delivered reminder.
    Returns (success, reason, snapshot).
    """
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                """
                SELECT *
                FROM reschedule_undo_history
                WHERE new_pending_id = ? AND user_id = ?
                """,
                (new_pending_id, user_id),
            ).fetchone()
            if not row:
                conn.rollback()
                return False, "not_found", None

            undo_dict = dict(row)
            if undo_dict.get("is_undone"):
                conn.rollback()
                return False, "already_undone", None

            pending_row = conn.execute(
                """
                SELECT id, delivery_status, is_sent
                FROM scheduled_messages
                WHERE id = ? AND user_id = ?
                """,
                (new_pending_id, user_id),
            ).fetchone()
            if not pending_row:
                conn.rollback()
                return False, "already_changed", None

            if pending_row["delivery_status"] != "pending" or pending_row["is_sent"] != 0:
                conn.rollback()
                return False, "already_changed", None

            # Delete the pending reminder
            del_cur = conn.execute(
                "DELETE FROM scheduled_messages WHERE id = ? AND user_id = ?",
                (new_pending_id, user_id),
            )
            if del_cur.rowcount != 1:
                conn.rollback()
                return False, "already_changed", None

            # Restore the delivered reminder row
            conn.execute(
                """
                INSERT OR REPLACE INTO scheduled_messages (
                    id, user_id, message_id, delivered_message_id, send_at, created_at,
                    is_sent, text_preview, source_name, delivery_status, retry_count,
                    last_error, last_attempt_at, tag, full_text, note, delivered_at, source_url
                ) VALUES (
                    ?, ?, ?, ?, ?, ?,
                    1, ?, ?, ?, 0,
                    NULL, NULL, ?, ?, ?, ?, ?
                )
                """,
                (
                    undo_dict["old_reminder_id"],
                    user_id,
                    undo_dict["message_id"],
                    undo_dict["delivered_message_id"],
                    undo_dict["old_send_at"],
                    undo_dict["created_at"],
                    undo_dict["text_preview"],
                    undo_dict["source_name"],
                    undo_dict["delivery_status"] or "sent",
                    undo_dict["tag"],
                    undo_dict["full_text"],
                    undo_dict["note"],
                    undo_dict["delivered_at"],
                    undo_dict["source_url"],
                ),
            )

            conn.execute(
                "UPDATE reschedule_undo_history SET is_undone = 1 WHERE new_pending_id = ? AND user_id = ?",
                (new_pending_id, user_id),
            )
            conn.commit()

            snapshot = {
                "old_reminder_id": undo_dict["old_reminder_id"],
                "new_pending_id": new_pending_id,
                "original_text": undo_dict.get("original_text") or "",
                "original_message_id": undo_dict.get("original_message_id"),
                "text_preview": undo_dict.get("text_preview"),
                "full_text": undo_dict.get("full_text"),
            }
            return True, "ok", snapshot
        except Exception:
            conn.rollback()
            raise


def get_scheduled_message_by_delivered_message_id(user_id, delivered_message_id):
    with get_connection() as conn:
        row = conn.execute(
            '''
            SELECT id, message_id, delivered_message_id, send_at, text_preview, source_name, is_sent
            FROM scheduled_messages
            WHERE user_id = ? AND delivered_message_id = ? AND is_sent = 1 AND (delivery_status != 'done' OR delivery_status IS NULL)
            ORDER BY id DESC
            LIMIT 1
            ''',
            (user_id, delivered_message_id),
        ).fetchone()
    return row


def add_subscription(user_id, channel_username, channel_title, period, last_scraped_at, next_send_at, tag=None):
    normalized_username = normalize_channel_username(channel_username)
    created_at = serialize_datetime(utc_now())
    with get_connection() as conn:
        # SEC1 (п.13): BEGIN IMMEDIATE закрывает гонку SELECT→INSERT между процессами.
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing_row = conn.execute(
                """
                SELECT id
                FROM subscriptions
                WHERE user_id = ? AND channel_username = ? AND period = ? AND is_disabled = 0
                LIMIT 1
                """,
                (user_id, normalized_username, period),
            ).fetchone()

            if existing_row:
                sub_id = existing_row[0]
                if tag is None:
                    conn.execute(
                        """
                        UPDATE subscriptions
                        SET channel_username = ?, channel_title = ?, last_scraped_at = ?, next_send_at = ?,
                            digest_status = 'active', failure_count = 0, last_error = NULL, last_attempt_at = NULL,
                            is_paused = 0, last_post_id = NULL
                        WHERE id = ?
                        """,
                        (normalized_username, channel_title, last_scraped_at, next_send_at, sub_id),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE subscriptions
                        SET channel_username = ?, channel_title = ?, last_scraped_at = ?, next_send_at = ?,
                            digest_status = 'active', failure_count = 0, last_error = NULL, last_attempt_at = NULL,
                            is_paused = 0, tag = ?, last_post_id = NULL
                        WHERE id = ?
                        """,
                        (normalized_username, channel_title, last_scraped_at, next_send_at, tag, sub_id),
                    )
            else:
                # SEC1 (п.10): имя канала должно быть безопасным для href.
                # Подписку с таким же каналом (перенос между периодами) не блокируем.
                if not is_valid_channel_username(normalized_username):
                    legacy_row = conn.execute(
                        "SELECT 1 FROM subscriptions WHERE user_id = ? AND channel_username = ? LIMIT 1",
                        (user_id, normalized_username),
                    ).fetchone()
                    if not legacy_row:
                        raise ValueError("Некорректное имя канала")
                # SEC1 (п.2): квота активных подписок на пользователя.
                active_count = conn.execute(
                    "SELECT COUNT(*) FROM subscriptions WHERE user_id = ? AND is_disabled = 0",
                    (user_id,),
                ).fetchone()[0]
                if active_count >= MAX_DIGEST_SUBSCRIPTIONS:
                    raise ValueError(f"Достигнут лимит подписок ({MAX_DIGEST_SUBSCRIPTIONS})")
                cursor = conn.execute(
                    '''
                    INSERT INTO subscriptions (
                        user_id, channel_username, channel_title, period, created_at, last_scraped_at, next_send_at, tag
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''',
                    (user_id, normalized_username, channel_title, period, created_at, last_scraped_at, next_send_at, tag),
                )
                sub_id = cursor.lastrowid
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return sub_id


def count_due_subscriptions(current_time_str) -> int:
    with get_connection() as conn:
        row = conn.execute(
            '''
            SELECT COUNT(*)
            FROM subscriptions
            WHERE next_send_at <= ? AND is_disabled = 0 AND is_paused = 0
            ''',
            (current_time_str,),
        ).fetchone()
    return int(row[0]) if row else 0


def get_due_subscriptions(current_time_str, limit: int | None = None):
    limit = max(1, int(limit)) if limit is not None else -1
    with get_connection() as conn:
        rows = conn.execute(
            '''
            SELECT id, user_id, channel_username, channel_title, period, last_scraped_at, failure_count,
                   last_post_id, next_send_at
            FROM subscriptions
            WHERE next_send_at <= ? AND is_disabled = 0 AND is_paused = 0
            ORDER BY next_send_at, id
            LIMIT ?
            ''',
            (current_time_str, limit),
        ).fetchall()
    return rows


def get_user_subscriptions_due_until(user_id, until_str, limit: int | None = None):
    """Active subscriptions of one user scheduled up to ``until_str`` (same row shape as
    get_due_subscriptions): everything that belongs to the user's edition of the day."""
    limit = MAX_DIGEST_SUBSCRIPTIONS if limit is None else max(1, int(limit))
    with get_connection() as conn:
        rows = conn.execute(
            '''
            SELECT id, user_id, channel_username, channel_title, period, last_scraped_at, failure_count,
                   last_post_id, next_send_at
            FROM subscriptions
            WHERE user_id = ? AND next_send_at <= ? AND is_disabled = 0 AND is_paused = 0
            ORDER BY next_send_at, id
            LIMIT ?
            ''',
            (user_id, until_str, limit),
        ).fetchall()
    return rows


def defer_subscriptions(sub_ids, next_send_at) -> int:
    """Move due subscriptions to a later moment of the same day (the senior edition's time)
    without touching their status, cursor or failure counters."""
    ids = [int(sub_id) for sub_id in sub_ids]
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    with get_connection() as conn:
        cursor = conn.execute(
            f'UPDATE subscriptions SET next_send_at = ? WHERE id IN ({placeholders})',
            (next_send_at, *ids),
        )
        conn.commit()
    return cursor.rowcount


def update_subscription_time(sub_id, last_scraped_at, next_send_at, last_post_id=None):
    with get_connection() as conn:
        conn.execute(
            '''
            UPDATE subscriptions
            SET last_scraped_at = ?, next_send_at = ?, digest_status = 'active',
                failure_count = 0, last_error = NULL, last_attempt_at = ?,
                last_post_id = COALESCE(?, last_post_id)
            WHERE id = ?
            ''',
            (last_scraped_at, next_send_at, last_scraped_at, last_post_id, sub_id),
        )
        conn.commit()


def update_subscription_schedule(sub_id, next_send_at, attempted_at):
    """Schedule the next run without moving the delivered-post cursor."""
    with get_connection() as conn:
        conn.execute(
            '''
            UPDATE subscriptions
            SET next_send_at = ?, digest_status = 'active', failure_count = 0,
                last_error = NULL, last_attempt_at = ?
            WHERE id = ?
            ''',
            (next_send_at, attempted_at, sub_id),
        )
        conn.commit()


def update_channel_subscribers(channel_username: str, count: int | None) -> None:
    normalized = normalize_channel_username(channel_username)
    now_str = serialize_datetime(utc_now())
    with get_connection() as conn:
        conn.execute(
            '''
            INSERT INTO channels (channel_username, subscribers_count, subscribers_updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(channel_username) DO UPDATE SET
                subscribers_count = excluded.subscribers_count,
                subscribers_updated_at = excluded.subscribers_updated_at
            ''',
            (normalized, count, now_str),
        )
        conn.commit()


def get_channel_subscribers(channel_username: str) -> int | None:
    normalized = normalize_channel_username(channel_username)
    with get_connection() as conn:
        row = conn.execute(
            "SELECT subscribers_count FROM channels WHERE channel_username = ?",
            (normalized,),
        ).fetchone()
        return row["subscribers_count"] if row else None


def needs_channel_subscribers_update(channel_username: str, ttl_hours: int = 24) -> bool:
    normalized = normalize_channel_username(channel_username)
    with get_connection() as conn:
        row = conn.execute(
            "SELECT subscribers_updated_at FROM channels WHERE channel_username = ?",
            (normalized,),
        ).fetchone()
        if not row or not row["subscribers_updated_at"]:
            return True
        updated_at = parse_db_datetime(row["subscribers_updated_at"])
        return (utc_now() - updated_at).total_seconds() > ttl_hours * 3600


def update_channel_scan_cursor(
    channel_username: str,
    last_scanned_at: datetime | str | None,
    last_scanned_post_id: int | None = None,
) -> None:
    normalized = normalize_channel_username(channel_username)
    if last_scanned_at is not None:
        scanned_str = normalize_feed_datetime(last_scanned_at)
    else:
        scanned_str = None
    with get_connection() as conn:
        conn.execute(
            '''
            INSERT INTO channels (channel_username, last_scanned_at, last_scanned_post_id)
            VALUES (?, ?, ?)
            ON CONFLICT(channel_username) DO UPDATE SET
                last_scanned_at = CASE
                    WHEN channels.last_scanned_at IS NULL THEN excluded.last_scanned_at
                    WHEN excluded.last_scanned_at IS NULL THEN channels.last_scanned_at
                    WHEN excluded.last_scanned_at > channels.last_scanned_at THEN excluded.last_scanned_at
                    ELSE channels.last_scanned_at
                END,
                last_scanned_post_id = CASE
                    WHEN excluded.last_scanned_at IS NOT NULL AND (channels.last_scanned_at IS NULL OR excluded.last_scanned_at > channels.last_scanned_at) THEN excluded.last_scanned_post_id
                    WHEN excluded.last_scanned_at IS NOT NULL AND excluded.last_scanned_at = channels.last_scanned_at THEN MAX(COALESCE(excluded.last_scanned_post_id, 0), COALESCE(channels.last_scanned_post_id, 0))
                    ELSE channels.last_scanned_post_id
                END
            ''',
            (normalized, scanned_str, last_scanned_post_id),
        )
        conn.commit()


def get_channel_scan_cursor(channel_username: str) -> tuple[str | None, int | None]:
    normalized = normalize_channel_username(channel_username)
    with get_connection() as conn:
        row = conn.execute(
            "SELECT last_scanned_at, last_scanned_post_id FROM channels WHERE channel_username = ?",
            (normalized,),
        ).fetchone()
        if not row:
            return None, None
        return row["last_scanned_at"], row["last_scanned_post_id"]


def update_channel_id(channel_username: str, channel_id: int) -> None:
    normalized = normalize_channel_username(channel_username)
    with get_connection() as conn:
        conn.execute(
            '''
            INSERT INTO channels (channel_username, channel_id)
            VALUES (?, ?)
            ON CONFLICT(channel_username) DO UPDATE SET
                channel_id = COALESCE(excluded.channel_id, channels.channel_id)
            ''',
            (normalized, channel_id),
        )
        conn.commit()


def get_channel_id(channel_username: str) -> int | None:
    normalized = normalize_channel_username(channel_username)
    with get_connection() as conn:
        row = conn.execute(
            "SELECT channel_id FROM channels WHERE channel_username = ?",
            (normalized,),
        ).fetchone()
        return row["channel_id"] if row and row["channel_id"] is not None else None


def set_channel_feed_backfilled_at(
    channel_username: str,
    backfilled_at: datetime | str | None,
) -> None:
    normalized = normalize_channel_username(channel_username)
    if isinstance(backfilled_at, datetime):
        val = serialize_datetime(backfilled_at)
    else:
        val = backfilled_at
    with get_connection() as conn:
        conn.execute(
            '''
            INSERT INTO channels (channel_username, feed_backfilled_at)
            VALUES (?, ?)
            ON CONFLICT(channel_username) DO UPDATE SET
                feed_backfilled_at = COALESCE(excluded.feed_backfilled_at, channels.feed_backfilled_at)
            ''',
            (normalized, val),
        )
        conn.commit()


def get_channel_feed_backfilled_at(channel_username: str) -> str | None:
    normalized = normalize_channel_username(channel_username)
    with get_connection() as conn:
        row = conn.execute(
            "SELECT feed_backfilled_at FROM channels WHERE channel_username = ?",
            (normalized,),
        ).fetchone()
        return row["feed_backfilled_at"] if row else None


def update_post_metrics(channel_username: str, metrics: list[dict]) -> int:
    """Обновить views, forwards, replies, metrics_at для постов канала.

    Реакции не затрагиваются (инвариант T21).
    metrics: список словарей с post_id, views, forwards, replies, metrics_at.
    """
    normalized = normalize_channel_username(channel_username)
    if not metrics:
        return 0
    rows = []
    for m in metrics:
        pid = m.get("post_id") if m.get("post_id") is not None else m.get("id")
        if pid is None:
            continue
        m_at = m.get("metrics_at")
        if isinstance(m_at, datetime):
            m_at_str = serialize_datetime(m_at)
        elif isinstance(m_at, str):
            m_at_str = m_at
        else:
            m_at_str = serialize_datetime(utc_now())
        rows.append((
            m.get("views"),
            m.get("forwards"),
            m.get("replies"),
            m_at_str,
            normalized,
            int(pid),
        ))
    if not rows:
        return 0
    with get_connection() as conn:
        before = conn.total_changes
        conn.executemany(
            '''
            UPDATE channel_posts
            SET views = COALESCE(?, views),
                forwards = COALESCE(?, forwards),
                replies = COALESCE(?, replies),
                metrics_at = ?
            WHERE channel_username = ? AND post_id = ?
            ''',
            rows,
        )
        changed = conn.total_changes - before
        conn.commit()
    return changed


def get_stale_metric_post_ids(
    channel_username: str,
    post_ids: list[int],
    cutoff: datetime | str,
) -> list[int]:
    """Вернуть из post_ids только те id, у которых metrics_at IS NULL или <= cutoff."""
    normalized = normalize_channel_username(channel_username)
    if not post_ids:
        return []
    cutoff_str = serialize_datetime(cutoff) if isinstance(cutoff, datetime) else str(cutoff)
    chunk_size = 500
    found: dict[int, str | None] = {}
    with get_connection() as conn:
        for i in range(0, len(post_ids), chunk_size):
            chunk = post_ids[i : i + chunk_size]
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f'''
                SELECT post_id, metrics_at FROM channel_posts
                WHERE channel_username = ? AND post_id IN ({placeholders})
                ''',
                (normalized, *chunk),
            ).fetchall()
            for r in rows:
                found[r["post_id"]] = r["metrics_at"]
    stale = []
    for pid in post_ids:
        if pid not in found:
            stale.append(pid)
        else:
            m_at = found[pid]
            if not m_at or m_at <= cutoff_str:
                stale.append(pid)
    return stale


def get_channel_backlog_count(
    channel_username: str,
    last_post_id: int | None = None,
    last_scraped_at: str | None = None,
) -> int:
    normalized = normalize_channel_username(channel_username)
    with get_connection() as conn:
        if last_post_id is not None:
            row = conn.execute(
                '''
                SELECT COUNT(*) FROM channel_posts
                WHERE channel_username = ? AND post_id > ?
                ''',
                (normalized, last_post_id),
            ).fetchone()
        else:
            marker = last_scraped_at or serialize_datetime(datetime.min.replace(tzinfo=UTC))
            row = conn.execute(
                '''
                SELECT COUNT(*) FROM channel_posts
                WHERE channel_username = ? AND post_time > ?
                ''',
                (normalized, marker),
            ).fetchone()
        return int(row[0]) if row else 0


def is_channel_backlog_overloaded(channel_username: str, threshold: int = CHANNEL_BACKLOG_MAX) -> bool:
    normalized = normalize_channel_username(channel_username)
    with get_connection() as conn:
        row = conn.execute(
            '''
            SELECT MIN(COALESCE(last_post_id, 0)) as min_post_id,
                   MIN(COALESCE(last_scraped_at, '1970-01-01 00:00:00')) as min_scraped
            FROM subscriptions
            WHERE channel_username = ? AND is_disabled = 0
            ''',
            (normalized,),
        ).fetchone()
        if not row or (row["min_post_id"] is None and row["min_scraped"] is None):
            return False
        min_post_id = row["min_post_id"]
        min_scraped = row["min_scraped"]
        if min_post_id is not None and min_post_id > 0:
            cnt = conn.execute(
                "SELECT COUNT(*) FROM channel_posts WHERE channel_username = ? AND post_id > ?",
                (normalized, min_post_id),
            ).fetchone()[0]
        else:
            cnt = conn.execute(
                "SELECT COUNT(*) FROM channel_posts WHERE channel_username = ? AND post_time > ?",
                (normalized, min_scraped),
            ).fetchone()[0]
        return cnt >= threshold


def upsert_channel_posts(channel_username: str, posts: list[dict], source: str) -> int:
    normalized_username = normalize_channel_username(channel_username)
    created_at = serialize_datetime(utc_now())
    rows = [
        (
            normalized_username,
            int(post["id"]),
            post.get("text") or "",
            post.get("link") or f"https://t.me/{normalized_username}/{post['id']}",
            serialize_datetime(post["time"]),
            source,
            created_at,
            post.get("views"),
            post.get("reactions_total"),
            post.get("reactions_json"),
            post.get("forwards"),
            post.get("replies"),
            serialize_datetime(post["metrics_at"]) if post.get("metrics_at") else None,
            post.get("fwd_from_channel_id"),
            post.get("fwd_from_post_id"),
            post.get("media_kind"),
            None if post.get("is_forward") is None else int(bool(post["is_forward"])),
            json.dumps(post["link_urls"], ensure_ascii=False) if post.get("link_urls") is not None else None,
        )
        for post in posts
        if post.get("id") is not None
    ]
    if not rows:
        return 0
    with get_connection() as conn:
        before = conn.total_changes
        conn.executemany(
            '''
            INSERT INTO channel_posts (
                channel_username, post_id, post_text, post_link, post_time, source, created_at,
                views, reactions_total, reactions_json, forwards, replies, metrics_at,
                fwd_from_channel_id, fwd_from_post_id, media_kind, is_forward, link_urls
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel_username, post_id) DO UPDATE SET
                post_text = excluded.post_text,
                post_link = excluded.post_link,
                post_time = excluded.post_time,
                source = excluded.source,
                views = CASE WHEN excluded.metrics_at IS NOT NULL THEN excluded.views ELSE channel_posts.views END,
                reactions_total = CASE WHEN excluded.metrics_at IS NOT NULL THEN excluded.reactions_total ELSE channel_posts.reactions_total END,
                reactions_json = CASE WHEN excluded.metrics_at IS NOT NULL THEN excluded.reactions_json ELSE channel_posts.reactions_json END,
                forwards = CASE WHEN excluded.metrics_at IS NOT NULL THEN excluded.forwards ELSE channel_posts.forwards END,
                replies = CASE WHEN excluded.metrics_at IS NOT NULL THEN excluded.replies ELSE channel_posts.replies END,
                metrics_at = CASE WHEN excluded.metrics_at IS NOT NULL THEN excluded.metrics_at ELSE channel_posts.metrics_at END,
                fwd_from_channel_id = COALESCE(excluded.fwd_from_channel_id, channel_posts.fwd_from_channel_id),
                fwd_from_post_id = COALESCE(excluded.fwd_from_post_id, channel_posts.fwd_from_post_id),
                media_kind = COALESCE(excluded.media_kind, channel_posts.media_kind),
                is_forward = COALESCE(excluded.is_forward, channel_posts.is_forward),
                link_urls = COALESCE(excluded.link_urls, channel_posts.link_urls)
            ''',
            rows,
        )
        changed = conn.total_changes - before
        conn.commit()
    return changed


def get_channel_posts_since(
    channel_username: str,
    last_scraped_at: str | None,
    last_post_id: int | None = None,
    limit: int | None = None,
) -> list[dict]:
    normalized_username = normalize_channel_username(channel_username)
    limit = CHANNEL_DELIVERY_BATCH_SIZE if limit is None else max(1, int(limit))
    with get_connection() as conn:
        if last_post_id is not None:
            rows = conn.execute(
                '''
                SELECT post_id, post_text, post_link, post_time,
                       views, reactions_total, reactions_json, forwards, replies, metrics_at
                FROM channel_posts
                WHERE channel_username = ? AND post_id > ?
                ORDER BY post_id
                LIMIT ?
                ''',
                (normalized_username, last_post_id, limit),
            ).fetchall()
        else:
            marker = last_scraped_at or serialize_datetime(datetime.min.replace(tzinfo=UTC))
            rows = conn.execute(
                '''
                SELECT post_id, post_text, post_link, post_time,
                       views, reactions_total, reactions_json, forwards, replies, metrics_at
                FROM channel_posts
                WHERE channel_username = ? AND post_time > ?
                ORDER BY post_id
                LIMIT ?
                ''',
                (normalized_username, marker, limit),
            ).fetchall()
    return [
        {
            "id": row["post_id"],
            "text": row["post_text"],
            "link": row["post_link"],
            "time": parse_db_datetime(row["post_time"]),
            "views": row["views"],
            "reactions_total": row["reactions_total"],
            "reactions_json": row["reactions_json"],
            "forwards": row["forwards"],
            "replies": row["replies"],
            "metrics_at": parse_db_datetime(row["metrics_at"]) if row["metrics_at"] else None,
        }
        for row in rows
    ]


def mark_subscription_delivery_error(sub_id, error_text, attempted_at, failure_count, is_permanent):
    error_text = truncate_error_text(error_text)
    with get_connection() as conn:
        conn.execute(
            '''
            UPDATE subscriptions
            SET digest_status = ?, failure_count = ?, last_error = ?, last_attempt_at = ?, is_disabled = ?
            WHERE id = ?
            ''',
            (
                'failed_permanent' if is_permanent else 'failed_temporary',
                failure_count,
                error_text,
                attempted_at,
                1 if is_permanent else 0,
                sub_id,
            ),
        )
        conn.commit()


def get_user_subscriptions(user_id):
    with get_connection() as conn:
        rows = conn.execute(
            '''
            SELECT id, channel_username, channel_title, period, next_send_at,
                   is_paused, tag, digest_status, failure_count
            FROM subscriptions
            WHERE user_id = ? AND is_disabled = 0
            ORDER BY next_send_at
            ''',
            (user_id,),
        ).fetchall()
    return rows


def get_subscription_by_id(user_id, sub_id):
    with get_connection() as conn:
        row = conn.execute(
            '''
            SELECT id, user_id, channel_username, channel_title, period, last_scraped_at, next_send_at,
                   is_paused, tag, digest_status, failure_count, last_error
            FROM subscriptions
            WHERE id = ? AND user_id = ? AND is_disabled = 0
            ''',
            (sub_id, user_id),
        ).fetchone()
    return row


def update_subscriptions_next_send_at(user_id, period, next_send_at):
    with get_connection() as conn:
        conn.execute(
            '''
            UPDATE subscriptions
            SET next_send_at = ?, digest_status = 'active', failure_count = 0, last_error = NULL
            WHERE user_id = ? AND period = ? AND is_disabled = 0
            ''',
            (next_send_at, user_id, period),
        )
        conn.commit()


def get_digest_settings(user_id):
    with get_connection() as conn:
        rows = conn.execute(
            '''
            SELECT period, send_hour, send_minute, weekday, month_day, monthly_mode
            FROM digest_settings
            WHERE user_id = ?
            ''',
            (user_id,),
        ).fetchall()
    return {row["period"]: row for row in rows}


def upsert_digest_settings(
    user_id,
    period,
    *,
    send_hour=None,
    send_minute=None,
    weekday=None,
    month_day=None,
    monthly_mode=None,
):
    with get_connection() as conn:
        existing = conn.execute(
            '''
            SELECT send_hour, send_minute, weekday, month_day, monthly_mode
            FROM digest_settings
            WHERE user_id = ? AND period = ?
            ''',
            (user_id, period),
        ).fetchone()
        values = {
            "send_hour": existing["send_hour"] if existing else 7,
            "send_minute": existing["send_minute"] if existing else 0,
            "weekday": existing["weekday"] if existing else None,
            "month_day": existing["month_day"] if existing else None,
            "monthly_mode": existing["monthly_mode"] if existing else "date",
        }
        if send_hour is not None:
            values["send_hour"] = send_hour
        if send_minute is not None:
            values["send_minute"] = send_minute
        if weekday is not None:
            values["weekday"] = weekday
        if month_day is not None:
            values["month_day"] = month_day
        if monthly_mode is not None:
            values["monthly_mode"] = monthly_mode

        conn.execute(
            '''
            INSERT INTO digest_settings (user_id, period, send_hour, send_minute, weekday, month_day, monthly_mode)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, period) DO UPDATE SET
                send_hour = excluded.send_hour,
                send_minute = excluded.send_minute,
                weekday = excluded.weekday,
                month_day = excluded.month_day,
                monthly_mode = excluded.monthly_mode
            ''',
            (
                user_id,
                period,
                values["send_hour"],
                values["send_minute"],
                values["weekday"],
                values["month_day"],
                values["monthly_mode"],
            ),
        )
        conn.commit()


def delete_subscription(user_id, sub_id):
    with get_connection() as conn:
        conn.execute(
            'DELETE FROM subscriptions WHERE id = ? AND user_id = ?',
            (sub_id, user_id),
        )
        conn.commit()


def count_user_subscriptions(user_id) -> int:
    with get_connection() as conn:
        row = conn.execute(
            'SELECT COUNT(*) FROM subscriptions WHERE user_id = ? AND is_disabled = 0',
            (user_id,),
        ).fetchone()
    return row[0] if row else 0


def set_subscription_paused(user_id, sub_id, paused: bool):
    with get_connection() as conn:
        conn.execute(
            'UPDATE subscriptions SET is_paused = ? WHERE id = ? AND user_id = ? AND is_disabled = 0',
            (1 if paused else 0, sub_id, user_id),
        )
        conn.commit()


def set_all_subscriptions_paused(user_id, paused: bool) -> int:
    with get_connection() as conn:
        cursor = conn.execute(
            'UPDATE subscriptions SET is_paused = ? WHERE user_id = ? AND is_disabled = 0 AND is_paused = ?',
            (1 if paused else 0, user_id, 0 if paused else 1),
        )
        conn.commit()
    return cursor.rowcount


def unsubscribe_all(user_id) -> int:
    with get_connection() as conn:
        cursor = conn.execute(
            'DELETE FROM subscriptions WHERE user_id = ? AND is_disabled = 0',
            (user_id,),
        )
        conn.commit()
    return cursor.rowcount


def set_subscription_tag(user_id, sub_id, tag: str | None):
    normalized = tag.strip() if isinstance(tag, str) and tag.strip() else None
    with get_connection() as conn:
        conn.execute(
            'UPDATE subscriptions SET tag = ? WHERE id = ? AND user_id = ? AND is_disabled = 0',
            (normalized, sub_id, user_id),
        )
        conn.commit()


def get_subscription_tags(user_id):
    with get_connection() as conn:
        rows = conn.execute(
            '''
            SELECT DISTINCT tag FROM subscriptions
            WHERE user_id = ? AND is_disabled = 0 AND tag IS NOT NULL AND tag != ''
            ORDER BY tag COLLATE NOCASE
            ''',
            (user_id,),
        ).fetchall()
    return [row[0] for row in rows if row[0]]


def add_digest_posts(user_id, channel_username, channel_title, posts):
    """Persist scraped posts into the knowledge base (deduplicated by post_link)."""
    if not posts:
        return 0
    normalized_username = normalize_channel_username(channel_username)
    created_at = serialize_datetime(utc_now())
    rows = []
    for post in posts:
        post_time = post.get("time")
        post_time_str = serialize_datetime(post_time) if post_time is not None else created_at
        rows.append(
            (
                user_id,
                normalized_username,
                channel_title,
                post.get("text", ""),
                post.get("link", ""),
                post_time_str,
                created_at,
            )
        )
    with get_connection() as conn:
        cursor = conn.executemany(
            '''
            INSERT OR IGNORE INTO digest_posts (
                user_id, channel_username, channel_title, post_text, post_link, post_time, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ''',
            rows,
        )
        inserted = cursor.rowcount
        conn.commit()
    return inserted


def add_telegraph_digest(user_id, url, title, channel_count, post_count):
    """Record a published Telegraph digest link for later lookup/search."""
    with get_connection() as conn:
        conn.execute(
            '''
            INSERT INTO telegraph_digests (user_id, url, title, channel_count, post_count, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ''',
            (user_id, url, title, channel_count, post_count, serialize_datetime(utc_now())),
        )
        conn.commit()


def get_latest_telegraph_digest(user_id: int, since_str: str | None = None) -> dict | None:
    with get_connection() as conn:
        if since_str:
            row = conn.execute(
                '''
                SELECT id, user_id, url, title, channel_count, post_count, created_at
                FROM telegraph_digests
                WHERE user_id = ? AND datetime(created_at) >= datetime(?)
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                ''',
                (user_id, since_str),
            ).fetchone()
        else:
            row = conn.execute(
                '''
                SELECT id, user_id, url, title, channel_count, post_count, created_at
                FROM telegraph_digests
                WHERE user_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                ''',
                (user_id,),
            ).fetchone()
        return dict(row) if row else None


def get_digest_posts(user_id, since_str, channel_username=None):
    with get_connection() as conn:
        if channel_username:
            rows = conn.execute(
                '''
                SELECT channel_username, channel_title, post_text, post_link, post_time
                FROM digest_posts
                WHERE user_id = ? AND post_time >= ? AND channel_username = ?
                ORDER BY post_time DESC
                ''',
                (user_id, since_str, normalize_channel_username(channel_username)),
            ).fetchall()
        else:
            rows = conn.execute(
                '''
                SELECT channel_username, channel_title, post_text, post_link, post_time
                FROM digest_posts
                WHERE user_id = ? AND post_time >= ?
                ORDER BY post_time DESC
                ''',
                (user_id, since_str),
            ).fetchall()
    return rows


def get_digest_post_channels(user_id, since_str):
    """Return [(channel_username, channel_title, post_count)] over the window."""
    with get_connection() as conn:
        rows = conn.execute(
            '''
            SELECT channel_username, MAX(channel_title) AS channel_title, COUNT(*) AS post_count
            FROM digest_posts
            WHERE user_id = ? AND post_time >= ?
            GROUP BY channel_username
            ORDER BY post_count DESC
            ''',
            (user_id, since_str),
        ).fetchall()
    return rows


def get_ai_usage_today(user_id, day: str) -> int:
    with get_connection() as conn:
        row = conn.execute(
            'SELECT request_count FROM ai_usage WHERE user_id = ? AND usage_day = ?',
            (user_id, day),
        ).fetchone()
    return row[0] if row else 0


def increment_ai_usage(user_id, day: str) -> int:
    with get_connection() as conn:
        conn.execute(
            '''
            INSERT INTO ai_usage (user_id, usage_day, request_count)
            VALUES (?, ?, 1)
            ON CONFLICT(user_id, usage_day) DO UPDATE SET request_count = request_count + 1
            ''',
            (user_id, day),
        )
        row = conn.execute(
            'SELECT request_count FROM ai_usage WHERE user_id = ? AND usage_day = ?',
            (user_id, day),
        ).fetchone()
        conn.commit()
    return row[0] if row else 0


def export_user_data(user_id) -> dict:
    """Collect a user's reminders, bookmarks, subscriptions and digest settings for JSON export."""
    with get_connection() as conn:
        reminders = conn.execute(
            '''
            SELECT send_at, text_preview, source_name, full_text, note, source_url
            FROM scheduled_messages
            WHERE user_id = ? AND is_sent = 0 AND delivery_status != 'failed_permanent'
            ORDER BY send_at
            LIMIT ?
            ''',
            (user_id, MAX_USER_REMINDERS),
        ).fetchall()
        bookmarks = conn.execute(
            '''
            SELECT tag, full_text, source_name, saved_at
            FROM saved_messages
            WHERE user_id = ?
            ORDER BY saved_at
            LIMIT ?
            ''',
            (user_id, MAX_USER_SAVED_MESSAGES),
        ).fetchall()
        subscriptions = conn.execute(
            '''
            SELECT channel_username, channel_title, period, tag, is_paused
            FROM subscriptions
            WHERE user_id = ? AND is_disabled = 0
            ORDER BY channel_username
            LIMIT ?
            ''',
            (user_id, MAX_DIGEST_SUBSCRIPTIONS),
        ).fetchall()
        digest_settings = conn.execute(
            '''
            SELECT period, send_hour, send_minute, weekday, month_day, monthly_mode
            FROM digest_settings
            WHERE user_id = ?
            ''',
            (user_id,),
        ).fetchall()
        feed_subscriptions = conn.execute(
            '''
            SELECT channel_username, channel_title, tag, is_paused
            FROM feed_subscriptions
            WHERE user_id = ?
            ORDER BY channel_username
            ''',
            (user_id,),
        ).fetchall()
    return {
        "reminders": [dict(row) for row in reminders],
        "bookmarks": [dict(row) for row in bookmarks],
        "subscriptions": [dict(row) for row in subscriptions],
        "digest_settings": [dict(row) for row in digest_settings],
        "prefs": get_user_prefs(user_id),
        "feed": [dict(row) for row in feed_subscriptions],
    }


def clear_subscription_failure(user_id, sub_id):
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE subscriptions
            SET digest_status = 'active', failure_count = 0, last_error = NULL
            WHERE id = ? AND user_id = ? AND is_disabled = 0
            """,
            (sub_id, user_id),
        )
        conn.commit()


def get_app_meta(key: str) -> str | None:
    with get_connection() as conn:
        row = conn.execute("SELECT value FROM app_meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_app_meta(key: str, value: str) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO app_meta (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        conn.commit()


def delete_app_meta(key: str) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM app_meta WHERE key = ?", (key,))
        conn.commit()


META_LAST_CYCLE_AT = "watchdog_last_cycle_at"
META_BACKLOG_HISTORY = "watchdog_backlog_history"
META_ALERT_STALL_SENT = "watchdog_alert_stall_sent"
META_ALERT_BACKLOG_SENT = "watchdog_alert_backlog_sent"


def record_digest_cycle_heartbeat(backlog: int = 0, now: datetime | None = None) -> None:
    now_val = now or utc_now()
    now_str = serialize_datetime(now_val)
    set_app_meta(META_LAST_CYCLE_AT, now_str)

    hist_raw = get_app_meta(META_BACKLOG_HISTORY)
    history: list[int] = []
    if hist_raw:
        try:
            parsed = json.loads(hist_raw)
            if isinstance(parsed, list):
                history = [int(x) for x in parsed]
        except Exception:
            history = []
    history.append(int(backlog))
    if len(history) > 10:
        history = history[-10:]
    set_app_meta(META_BACKLOG_HISTORY, json.dumps(history))

    if len(history) >= 2 and history[-1] < history[-2]:
        delete_app_meta(META_ALERT_BACKLOG_SENT)
    elif backlog == 0:
        delete_app_meta(META_ALERT_BACKLOG_SENT)

    delete_app_meta(META_ALERT_STALL_SENT)


def get_last_digest_cycle_at() -> str | None:
    return get_app_meta(META_LAST_CYCLE_AT)


def get_digest_cycle_backlog_history() -> list[int]:
    hist_raw = get_app_meta(META_BACKLOG_HISTORY)
    if not hist_raw:
        return []
    try:
        parsed = json.loads(hist_raw)
        return [int(x) for x in parsed] if isinstance(parsed, list) else []
    except Exception:
        return []


def add_saved_message(user_id, full_text, source_name, tag, saved_at):
    with get_connection() as conn:
        saved_count = conn.execute(
            "SELECT COUNT(*) FROM saved_messages WHERE user_id = ?",
            (user_id,),
        ).fetchone()[0]
        if saved_count >= MAX_USER_SAVED_MESSAGES:
            raise ValueError(f"Достигнут лимит закладок ({MAX_USER_SAVED_MESSAGES})")
        cur = conn.execute(
            '''
            INSERT INTO saved_messages (user_id, full_text, source_name, tag, saved_at)
            VALUES (?, ?, ?, ?, ?)
            ''',
            (user_id, full_text, source_name, tag, saved_at),
        )
        conn.commit()
        return cur.lastrowid


def get_user_tags(user_id):
    """Unified tag pool shared across bookmarks and reminders."""
    with get_connection() as conn:
        rows = conn.execute(
            '''
            SELECT tag FROM saved_messages WHERE user_id = ? AND tag IS NOT NULL AND tag != ''
            UNION
            SELECT tag FROM scheduled_messages
            WHERE user_id = ? AND is_sent = 0 AND tag IS NOT NULL AND tag != ''
            ORDER BY tag COLLATE NOCASE
            ''',
            (user_id, user_id),
        ).fetchall()
    return [row[0] for row in rows if row[0]]


def get_saved_messages(user_id, limit: int | None = None):
    limit = MAX_USER_SAVED_MESSAGES if limit is None else max(1, int(limit))
    with get_connection() as conn:
        rows = conn.execute(
            '''
            SELECT id, tag, full_text, source_name, saved_at
            FROM saved_messages
            WHERE user_id = ?
            ORDER BY tag, saved_at DESC
            LIMIT ?
            ''',
            (user_id, limit),
        ).fetchall()
    return rows


def get_saved_message_by_id(user_id, msg_id):
    with get_connection() as conn:
        row = conn.execute(
            '''
            SELECT full_text, source_name, tag, saved_at
            FROM saved_messages
            WHERE id = ? AND user_id = ?
            ''',
            (msg_id, user_id),
        ).fetchone()
    return row


def delete_saved_message(user_id, msg_id):
    with get_connection() as conn:
        conn.execute(
            'DELETE FROM saved_messages WHERE id = ? AND user_id = ?',
            (msg_id, user_id),
        )
        conn.commit()


def update_saved_message_tag(user_id, msg_id, new_tag):
    with get_connection() as conn:
        conn.execute(
            'UPDATE saved_messages SET tag = ? WHERE id = ? AND user_id = ?',
            (new_tag, msg_id, user_id),
        )
        conn.commit()



def record_digest_execution_start(user_id: int, period: str, scheduled_at: str) -> int:
    created_at = serialize_datetime(utc_now())
    started_at = created_at
    with get_connection() as conn:
        cursor = conn.execute(
            '''
            INSERT INTO digest_executions (
                user_id, period, scheduled_at, started_at, status, created_at
            ) VALUES (?, ?, ?, ?, 'retrying', ?)
            ON CONFLICT(user_id, period, scheduled_at) DO UPDATE SET
                started_at = excluded.started_at,
                status = 'retrying'
            RETURNING id
            ''',
            (user_id, period, scheduled_at, started_at, created_at),
        )
        row = cursor.fetchone()
        conn.commit()
        return row[0]


def record_digest_execution_finish(
    execution_id: int,
    status: str,
    posts_count: int = 0,
    channels_count: int = 0,
    channels_failed: int = 0,
    channels_partial: int = 0,
    error_message: str | None = None,
) -> None:
    finished_at = serialize_datetime(utc_now())
    err_text = truncate_error_text(error_message)
    with get_connection() as conn:
        if status in ("retrying", "failed"):
            conn.execute(
                '''
                UPDATE digest_executions
                SET finished_at = ?,
                    status = ?,
                    posts_count = ?,
                    channels_count = ?,
                    channels_failed = ?,
                    channels_partial = ?,
                    error_message = ?,
                    retry_count = retry_count + 1
                WHERE id = ?
                ''',
                (
                    finished_at,
                    status,
                    posts_count,
                    channels_count,
                    channels_failed,
                    channels_partial,
                    err_text,
                    execution_id,
                ),
            )
        else:
            conn.execute(
                '''
                UPDATE digest_executions
                SET finished_at = ?,
                    status = ?,
                    posts_count = ?,
                    channels_count = ?,
                    channels_failed = ?,
                    channels_partial = ?,
                    error_message = ?
                WHERE id = ?
                ''',
                (
                    finished_at,
                    status,
                    posts_count,
                    channels_count,
                    channels_failed,
                    channels_partial,
                    err_text,
                    execution_id,
                ),
            )
        conn.commit()


def get_digest_execution(user_id: int, period: str, scheduled_at: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            '''
            SELECT id, user_id, period, scheduled_at, started_at, finished_at,
                   status, posts_count, channels_count, channels_failed,
                   channels_partial, error_message, retry_count, created_at
            FROM digest_executions
            WHERE user_id = ? AND period = ? AND scheduled_at = ?
            ''',
            (user_id, period, scheduled_at),
        ).fetchone()
        return dict(row) if row else None


def get_digest_execution_by_id(execution_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            '''
            SELECT id, user_id, period, scheduled_at, started_at, finished_at,
                   status, posts_count, channels_count, channels_failed,
                   channels_partial, error_message, retry_count, created_at
            FROM digest_executions
            WHERE id = ?
            ''',
            (execution_id,),
        ).fetchone()
        return dict(row) if row else None


def retry_digest_execution(execution_id: int, user_id: int) -> bool:
    """SEC1 (п.1): повтор выпуска разрешён только его владельцу.

    И владелец, и период берутся из строки, найденной по (id, user_id) — чужой
    идентификатор не даёт доступа и не меняет состояние.
    """
    now_str = serialize_datetime(utc_now())
    with get_connection() as conn:
        row = conn.execute(
            'SELECT user_id, period, scheduled_at FROM digest_executions WHERE id = ? AND user_id = ?',
            (execution_id, user_id),
        ).fetchone()
        if not row:
            return False
        owner_id = row['user_id']
        period = row['period']
        sched_at = row['scheduled_at'] if ('scheduled_at' in row.keys() and row['scheduled_at']) else now_str

        conn.execute(
            '''
            UPDATE digest_executions
            SET status = 'retrying', retry_count = 0, error_message = NULL
            WHERE id = ? AND user_id = ?
            ''',
            (execution_id, user_id),
        )
        conn.execute(
            '''
            UPDATE subscriptions
            SET next_send_at = ?, failure_count = 0, digest_status = 'pending', is_disabled = 0
            WHERE user_id = ? AND period = ?
            ''',
            (sched_at, owner_id, period),
        )
        conn.commit()
        return True


def get_last_digest_execution(user_id: int, period: str | None = None) -> dict | None:
    with get_connection() as conn:
        if period:
            row = conn.execute(
                '''
                SELECT id, user_id, period, scheduled_at, started_at, finished_at,
                       status, posts_count, channels_count, channels_failed,
                       channels_partial, error_message, retry_count, created_at
                FROM digest_executions
                WHERE user_id = ? AND period = ?
                ORDER BY scheduled_at DESC, id DESC
                LIMIT 1
                ''',
                (user_id, period),
            ).fetchone()
        else:
            row = conn.execute(
                '''
                SELECT id, user_id, period, scheduled_at, started_at, finished_at,
                       status, posts_count, channels_count, channels_failed,
                       channels_partial, error_message, retry_count, created_at
                FROM digest_executions
                WHERE user_id = ?
                ORDER BY scheduled_at DESC, id DESC
                LIMIT 1
                ''',
                (user_id,),
            ).fetchone()
        return dict(row) if row else None


def get_user_alert_settings(user_id: int) -> dict:
    with get_connection() as conn:
        row = conn.execute(
            '''
            SELECT user_id, alerts_enabled, alerts_paused_until, last_alert_sent_at, consecutive_failures
            FROM user_alert_settings
            WHERE user_id = ?
            ''',
            (user_id,),
        ).fetchone()
        if not row:
            return {
                "user_id": user_id,
                "alerts_enabled": True,
                "alerts_paused_until": None,
                "last_alert_sent_at": None,
                "consecutive_failures": 0,
            }
        return {
            "user_id": row["user_id"],
            "alerts_enabled": bool(row["alerts_enabled"]),
            "alerts_paused_until": row["alerts_paused_until"],
            "last_alert_sent_at": row["last_alert_sent_at"],
            "consecutive_failures": row["consecutive_failures"] or 0,
        }


def record_user_alert_dispatched(user_id: int, sent_at: str | None = None) -> None:
    now_str = serialize_datetime(utc_now())
    sent_at_val = sent_at or now_str
    with get_connection() as conn:
        conn.execute(
            '''
            INSERT INTO user_alert_settings (
                user_id, alerts_enabled, last_alert_sent_at, consecutive_failures, created_at, updated_at
            ) VALUES (?, 1, ?, 1, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                last_alert_sent_at = excluded.last_alert_sent_at,
                consecutive_failures = COALESCE(user_alert_settings.consecutive_failures, 0) + 1,
                updated_at = excluded.updated_at
            ''',
            (user_id, sent_at_val, now_str, now_str),
        )
        conn.commit()


def reset_user_alert_consecutive_failures(user_id: int) -> None:
    now_str = serialize_datetime(utc_now())
    with get_connection() as conn:
        conn.execute(
            '''
            UPDATE user_alert_settings
            SET consecutive_failures = 0, updated_at = ?
            WHERE user_id = ?
            ''',
            (now_str, user_id),
        )
        conn.commit()


def set_user_alerts_enabled(user_id: int, enabled: bool) -> None:
    now_str = serialize_datetime(utc_now())
    val = 1 if enabled else 0
    with get_connection() as conn:
        conn.execute(
            '''
            INSERT INTO user_alert_settings (user_id, alerts_enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                alerts_enabled = excluded.alerts_enabled,
                updated_at = excluded.updated_at
            ''',
            (user_id, val, now_str, now_str),
        )
        conn.commit()


def set_user_alerts_paused_until(user_id: int, paused_until: str | None) -> None:
    now_str = serialize_datetime(utc_now())
    with get_connection() as conn:
        conn.execute(
            '''
            INSERT INTO user_alert_settings (user_id, alerts_enabled, alerts_paused_until, created_at, updated_at)
            VALUES (?, 1, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                alerts_enabled = 1,
                alerts_paused_until = excluded.alerts_paused_until,
                updated_at = excluded.updated_at
            ''',
            (user_id, paused_until, now_str, now_str),
        )
        conn.commit()


def is_user_alerting_active(user_id: int, now: datetime | None = None) -> bool:
    if now is None:
        now = utc_now()
    settings = get_user_alert_settings(user_id)
    if not settings["alerts_enabled"]:
        return False
    paused_until_str = settings["alerts_paused_until"]
    if paused_until_str:
        paused_until = parse_db_datetime(paused_until_str)
        if paused_until > now:
            return False
    return True


def get_user_prefs(user_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            '''
            SELECT user_id, timezone, morning_hour, day_hour, evening_hour, updated_at
            FROM user_prefs
            WHERE user_id = ?
            ''',
            (user_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "user_id": row["user_id"],
            "timezone": row["timezone"],
            "morning_hour": row["morning_hour"] if row["morning_hour"] is not None else 9,
            "day_hour": row["day_hour"] if row["day_hour"] is not None else 14,
            "evening_hour": row["evening_hour"] if row["evening_hour"] is not None else 20,
            "updated_at": row["updated_at"],
        }


def upsert_user_prefs(
    user_id: int,
    timezone: str | None = None,
    morning_hour: int | None = None,
    day_hour: int | None = None,
    evening_hour: int | None = None,
) -> dict:
    now_str = serialize_datetime(utc_now())
    with get_connection() as conn:
        current = get_user_prefs(user_id)
        if current is None:
            tz_val = timezone
            m_val = morning_hour if morning_hour is not None else 9
            d_val = day_hour if day_hour is not None else 14
            e_val = evening_hour if evening_hour is not None else 20
            conn.execute(
                '''
                INSERT INTO user_prefs (user_id, timezone, morning_hour, day_hour, evening_hour, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ''',
                (user_id, tz_val, m_val, d_val, e_val, now_str),
            )
        else:
            tz_val = timezone if timezone is not None else current["timezone"]
            m_val = morning_hour if morning_hour is not None else current["morning_hour"]
            d_val = day_hour if day_hour is not None else current["day_hour"]
            e_val = evening_hour if evening_hour is not None else current["evening_hour"]
            conn.execute(
                '''
                UPDATE user_prefs
                SET timezone = ?, morning_hour = ?, day_hour = ?, evening_hour = ?, updated_at = ?
                WHERE user_id = ?
                ''',
                (tz_val, m_val, d_val, e_val, now_str, user_id),
            )
        conn.commit()
    return {
        "user_id": user_id,
        "timezone": tz_val,
        "morning_hour": m_val,
        "day_hour": d_val,
        "evening_hour": e_val,
        "updated_at": now_str,
    }


def get_digest_alert(user_id: int, period: str, scheduled_at: str, alert_type: str = "failure") -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            '''
            SELECT id, user_id, period, scheduled_at, alert_type, sent_at, message_id, error_text, created_at
            FROM digest_alerts
            WHERE user_id = ? AND period = ? AND scheduled_at = ? AND alert_type = ?
            ''',
            (user_id, period, scheduled_at, alert_type),
        ).fetchone()
        return dict(row) if row else None


def record_digest_alert_sent(
    user_id: int,
    period: str,
    scheduled_at: str,
    alert_type: str,
    sent_at: str | None = None,
    message_id: int | None = None,
    error_text: str | None = None,
) -> int | None:
    created_at = serialize_datetime(utc_now())
    sent_at_val = sent_at or created_at
    with get_connection() as conn:
        cursor = conn.execute(
            '''
            INSERT INTO digest_alerts (
                user_id, period, scheduled_at, alert_type, sent_at, message_id, error_text, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, period, scheduled_at, alert_type) DO UPDATE SET
                sent_at = excluded.sent_at,
                message_id = COALESCE(excluded.message_id, digest_alerts.message_id),
                error_text = COALESCE(excluded.error_text, digest_alerts.error_text)
            ''',
            (user_id, period, scheduled_at, alert_type, sent_at_val, message_id, truncate_error_text(error_text), created_at),
        )
        conn.commit()
        return cursor.lastrowid


def has_digest_alert_been_sent(user_id: int, period: str, scheduled_at: str, alert_type: str) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            '''
            SELECT 1 FROM digest_alerts
            WHERE user_id = ? AND period = ? AND scheduled_at = ? AND alert_type = ?
            ''',
            (user_id, period, scheduled_at, alert_type),
        ).fetchone()
        return bool(row)


def get_failed_digest_executions_pending_alert(
    older_than_seconds: int,
    max_retries: int = 3,
    now_str: str | None = None,
) -> list[dict]:
    if not now_str:
        now_str = serialize_datetime(utc_now())
    with get_connection() as conn:
        rows = conn.execute(
            '''
            SELECT de.id, de.user_id, de.period, de.scheduled_at, de.started_at, de.finished_at,
                   de.status, de.posts_count, de.channels_count, de.channels_failed,
                   de.channels_partial, de.error_message, de.retry_count, de.created_at
            FROM digest_executions de
            WHERE (
                de.status = 'failed'
                OR (de.status = 'retrying' AND de.retry_count >= ?)
            )
            AND datetime(de.scheduled_at, '+' || ? || ' seconds') <= datetime(?)
            AND datetime(de.scheduled_at) >= datetime(?, '-2 days')
            AND NOT EXISTS (
                SELECT 1 FROM digest_alerts da
                WHERE da.user_id = de.user_id
                  AND da.period = de.period
                  AND da.scheduled_at = de.scheduled_at
                  AND da.alert_type = 'failure'
            )
            ORDER BY de.scheduled_at ASC
            ''',
            (max_retries, older_than_seconds, now_str, now_str),
        ).fetchall()
        return [dict(r) for r in rows]


def get_delivered_executions_pending_recovery_alert(now_str: str | None = None) -> list[dict]:
    if not now_str:
        now_str = serialize_datetime(utc_now())
    with get_connection() as conn:
        rows = conn.execute(
            '''
            SELECT de.id, de.user_id, de.period, de.scheduled_at, de.started_at, de.finished_at,
                   de.status, de.posts_count, de.channels_count, de.channels_failed,
                   de.channels_partial, de.error_message, de.retry_count, de.created_at
            FROM digest_executions de
            WHERE de.status IN ('delivered', 'partial')
              AND de.posts_count > 0
              AND datetime(de.scheduled_at) >= datetime(?, '-7 days')
              AND EXISTS (
                  SELECT 1 FROM digest_alerts da
                  WHERE da.user_id = de.user_id
                    AND da.period = de.period
                    AND da.scheduled_at = de.scheduled_at
                    AND da.alert_type = 'failure'
              )
              AND NOT EXISTS (
                  SELECT 1 FROM digest_alerts da
                  WHERE da.user_id = de.user_id
                    AND da.period = de.period
                    AND da.scheduled_at = de.scheduled_at
                    AND da.alert_type = 'recovery'
              )
            ORDER BY de.finished_at ASC, de.id ASC
            ''',
            (now_str,),
        ).fetchall()
        return [dict(r) for r in rows]


def _delete_old_rows_in_batches(conn, table: str, where_clause: str, params: tuple) -> int:
    """Удалять строки короткими порциями, освобождая write-lock после каждой."""
    deleted = 0
    while True:
        cursor = conn.execute(
            f"""
            DELETE FROM {table}
            WHERE rowid IN (
                SELECT rowid FROM {table}
                WHERE {where_clause}
                LIMIT ?
            )
            """,
            (*params, CLEANUP_BATCH_SIZE),
        )
        deleted += cursor.rowcount
        conn.commit()
        if cursor.rowcount < CLEANUP_BATCH_SIZE:
            break
    return deleted


def cleanup_old_records(now_str: str) -> dict[str, int]:
    with get_connection() as conn:
        message_retention = f"-{MESSAGE_RETENTION_DAYS} days"
        subscription_retention = f"-{SUBSCRIPTION_FAILURE_RETENTION_DAYS} days"
        digest_retention = f"-{DIGEST_POST_RETENTION_DAYS} days"
        scheduled_sent_deleted = _delete_old_rows_in_batches(
            conn,
            "scheduled_messages",
            "is_sent = 1 AND created_at < datetime(?, ?)",
            (now_str, message_retention),
        )
        scheduled_failed_deleted = _delete_old_rows_in_batches(
            conn,
            "scheduled_messages",
            "delivery_status = 'failed_permanent' AND created_at < datetime(?, ?)",
            (now_str, message_retention),
        )
        subscriptions_failed_deleted = _delete_old_rows_in_batches(
            conn,
            "subscriptions",
            "is_disabled = 1 AND digest_status = 'failed_permanent' "
            "AND COALESCE(last_attempt_at, created_at) < datetime(?, ?)",
            (now_str, subscription_retention),
        )
        digest_posts_deleted = _delete_old_rows_in_batches(
            conn,
            "digest_posts",
            "COALESCE(post_time, created_at) < datetime(?, ?)",
            (now_str, digest_retention),
        )
        channel_posts_deleted = _delete_old_rows_in_batches(
            conn,
            "channel_posts",
            "post_time < datetime(?, ?)",
            (now_str, digest_retention),
        )
        digest_executions_deleted = _delete_old_rows_in_batches(
            conn,
            "digest_executions",
            "created_at < datetime(?, ?)",
            (now_str, digest_retention),
        )
        digest_alerts_deleted = _delete_old_rows_in_batches(
            conn,
            "digest_alerts",
            "created_at < datetime(?, ?)",
            (now_str, digest_retention),
        )
        ai_usage_deleted = _delete_old_rows_in_batches(
            conn,
            "ai_usage",
            "usage_day < date(?, '-2 days')",
            (now_str,),
        )
        feed_editions_deleted = _delete_old_rows_in_batches(
            conn,
            "feed_editions",
            "created_at < datetime(?, '-2 days')",
            (now_str,),
        )
        reschedule_undo_deleted = _delete_old_rows_in_batches(
            conn,
            "reschedule_undo_history",
            "created_timestamp IS NOT NULL "
            "AND created_timestamp < unixepoch(datetime(?, '-1 day'))",
            (now_str,),
        )

    return {
        "scheduled_sent_deleted": scheduled_sent_deleted,
        "scheduled_failed_deleted": scheduled_failed_deleted,
        "subscriptions_failed_deleted": subscriptions_failed_deleted,
        "digest_posts_deleted": digest_posts_deleted,
        "channel_posts_deleted": channel_posts_deleted,
        "digest_executions_deleted": digest_executions_deleted,
        "digest_alerts_deleted": digest_alerts_deleted,
        "ai_usage_deleted": ai_usage_deleted,
        "feed_editions_deleted": feed_editions_deleted,
        "reschedule_undo_deleted": reschedule_undo_deleted,
    }


EMAIL_CODE_MAX_ATTEMPTS = 5


def get_user_email(user_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM user_emails WHERE user_id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def get_email_delivery(user_id: int) -> tuple[str | None, set[str]]:
    """Confirmed address and the digest periods delivered by email instead of Telegram."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT email, email_periods FROM user_emails WHERE user_id = ?", (user_id,)
        ).fetchone()
    if not row or not row["email"]:
        return None, set()
    return row["email"], {p for p in (row["email_periods"] or "").split(",") if p}


FEED_EMAIL_TOKEN = "feed"


def list_feed_email_recipients() -> list[tuple[int, str]]:
    """(user_id, address) пользователей с подтверждённой почтой и включённой почтовой Лентой."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT user_id, email, email_periods FROM user_emails "
            "WHERE email IS NOT NULL AND email != '' ORDER BY user_id"
        ).fetchall()
    return [
        (row["user_id"], row["email"])
        for row in rows
        if FEED_EMAIL_TOKEN in (row["email_periods"] or "").split(",")
    ]


def set_email_period(user_id: int, period: str, enabled: bool) -> bool:
    email, periods = get_email_delivery(user_id)
    if not email:
        return False
    periods = (periods | {period}) if enabled else (periods - {period})
    with get_connection() as conn:
        conn.execute(
            "UPDATE user_emails SET email_periods = ? WHERE user_id = ?",
            (",".join(sorted(periods)), user_id),
        )
        conn.commit()
    return True


def clear_email_periods(user_id: int) -> bool:
    """Send every digest type back to Telegram. Returns True if something was switched."""
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE user_emails SET email_periods = '' WHERE user_id = ? AND COALESCE(email_periods, '') != ''",
            (user_id,),
        )
        conn.commit()
    return cur.rowcount == 1


def start_email_verification(user_id: int, email: str, code: str, sent_at: str, expires_at: str) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO user_emails (user_id, pending_email, verify_code, code_sent_at, code_expires_at, code_attempts)
            VALUES (?, ?, ?, ?, ?, 0)
            ON CONFLICT(user_id) DO UPDATE SET
                pending_email = excluded.pending_email,
                verify_code = excluded.verify_code,
                code_sent_at = excluded.code_sent_at,
                code_expires_at = excluded.code_expires_at
            """,
            (user_id, email, code, sent_at, expires_at),
        )
        conn.commit()


def confirm_email_code(user_id: int, code: str, now_str: str) -> str:
    """Return 'ok', 'invalid', 'expired', 'too_many' or 'none'. On 'ok' the address is enabled."""
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT pending_email, verify_code, code_expires_at, code_attempts FROM user_emails WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not row or not row["pending_email"] or not row["verify_code"]:
            conn.rollback()
            return "none"
        if row["code_attempts"] >= EMAIL_CODE_MAX_ATTEMPTS:
            conn.rollback()
            return "too_many"
        if row["code_expires_at"] < now_str:
            conn.rollback()
            return "expired"
        if code.strip() != row["verify_code"]:
            conn.execute("UPDATE user_emails SET code_attempts = code_attempts + 1 WHERE user_id = ?", (user_id,))
            conn.commit()
            return "invalid"
        conn.execute(
            """
            UPDATE user_emails
            SET email = pending_email, pending_email = NULL,
                verify_code = NULL, code_expires_at = NULL, code_attempts = 0
            WHERE user_id = ?
            """,
            (user_id,),
        )
        conn.commit()
        return "ok"


def count_email_sends_for_user(user_id: int, since_str: str) -> int:
    """SEC1 (п.4): сколько писем пользователь запросил с момента since_str."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM email_send_log WHERE user_id = ? AND sent_at >= ?",
            (user_id, since_str),
        ).fetchone()
    return int(row[0]) if row else 0


def count_email_sends_for_address(address: str, since_str: str) -> int:
    """SEC1 (п.4): сколько писем ушло на конкретный адрес с момента since_str."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM email_send_log WHERE address = ? AND sent_at >= ?",
            (address, since_str),
        ).fetchone()
    return int(row[0]) if row else 0


def record_email_send(user_id: int, address: str, sent_at: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO email_send_log (user_id, address, sent_at) VALUES (?, ?, ?)",
            (user_id, address, sent_at),
        )
        conn.commit()


def delete_user_email(user_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM user_emails WHERE user_id = ?", (user_id,))
        conn.commit()


# ---------------------------------------------------------------------------
# Лента (Feed) — подписки, кеш и кандидаты
# ---------------------------------------------------------------------------


def normalize_feed_tag(tag: str | None) -> str | None:
    """Нормализовать тег Ленты: обрезка пробелов, <=24 символов, без переводов строк."""
    if not isinstance(tag, str):
        return None
    cleaned = tag.replace("\r", " ").replace("\n", " ").strip()
    cleaned = cleaned.lstrip("#").strip()
    if not cleaned:
        return None
    cleaned = cleaned[:24]
    dangling_chars = {
        "\u200d",
        "\ufe0f",
        "\U0001f3fb",
        "\U0001f3fc",
        "\U0001f3fd",
        "\U0001f3fe",
        "\U0001f3ff",
    }
    while cleaned and cleaned[-1] in dangling_chars:
        cleaned = cleaned[:-1]
    cleaned = cleaned.strip()
    return cleaned or None


def count_feed_subscriptions(user_id: int, include_paused: bool = True) -> int:
    """Количество подписок Ленты пользователя."""
    with get_connection() as conn:
        if include_paused:
            row = conn.execute(
                "SELECT COUNT(*) FROM feed_subscriptions WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) FROM feed_subscriptions WHERE user_id = ? AND is_paused = 0",
                (user_id,),
            ).fetchone()
    return row[0] if row else 0


def add_feed_subscription(
    user_id: int,
    channel_username: str,
    channel_title: str | None = None,
    tag: str | None = None,
) -> int:
    """Добавить канал в Ленту (идемпотентно по UNIQUE(user_id, channel_username))."""
    normalized_username = normalize_channel_username(channel_username)
    if not normalized_username:
        raise ValueError("Invalid channel username")
    normalized_tag = normalize_feed_tag(tag)
    created_at = serialize_datetime(utc_now())

    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute(
                "SELECT id, channel_title, tag FROM feed_subscriptions WHERE user_id = ? AND channel_username = ?",
                (user_id, normalized_username),
            ).fetchone()

            if existing:
                conn.commit()
                return existing["id"]

            current_count = conn.execute(
                "SELECT COUNT(*) FROM feed_subscriptions WHERE user_id = ?",
                (user_id,),
            ).fetchone()[0]
            if current_count >= MAX_FEED_CHANNELS:
                raise ValueError(f"Превышен лимит каналов Ленты ({MAX_FEED_CHANNELS})")

            cursor = conn.execute(
                '''
                INSERT INTO feed_subscriptions (user_id, channel_username, channel_title, tag, is_paused, created_at)
                VALUES (?, ?, ?, ?, 0, ?)
                ''',
                (user_id, normalized_username, channel_title, normalized_tag, created_at),
            )
            conn.commit()
            return cursor.lastrowid
        except Exception:
            conn.rollback()
            raise


def list_feed_subscriptions(user_id: int, include_paused: bool = True) -> list[sqlite3.Row]:
    """Список подписок Ленты пользователя."""
    with get_connection() as conn:
        if include_paused:
            rows = conn.execute(
                '''
                SELECT id, user_id, channel_username, channel_title, tag, is_paused, created_at
                FROM feed_subscriptions
                WHERE user_id = ?
                ORDER BY id ASC
                ''',
                (user_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                '''
                SELECT id, user_id, channel_username, channel_title, tag, is_paused, created_at
                FROM feed_subscriptions
                WHERE user_id = ? AND is_paused = 0
                ORDER BY id ASC
                ''',
                (user_id,),
            ).fetchall()
    return rows


get_feed_subscriptions = list_feed_subscriptions


def get_feed_subscription(user_id: int, sub_id: int) -> sqlite3.Row | None:
    """Получить подписку Ленты по id."""
    with get_connection() as conn:
        row = conn.execute(
            '''
            SELECT id, user_id, channel_username, channel_title, tag, is_paused, created_at
            FROM feed_subscriptions
            WHERE user_id = ? AND id = ?
            ''',
            (user_id, sub_id),
        ).fetchone()
    return row


def get_feed_subscription_by_username(user_id: int, channel_username: str) -> sqlite3.Row | None:
    """Получить подписку Ленты по username."""
    normalized = normalize_channel_username(channel_username)
    with get_connection() as conn:
        row = conn.execute(
            '''
            SELECT id, user_id, channel_username, channel_title, tag, is_paused, created_at
            FROM feed_subscriptions
            WHERE user_id = ? AND channel_username = ?
            ''',
            (user_id, normalized),
        ).fetchone()
    return row


def set_feed_tag(user_id: int, sub_id: int, tag: str | None) -> bool:
    """Установить или сбросить тег подписки Ленты."""
    normalized = normalize_feed_tag(tag)
    with get_connection() as conn:
        cursor = conn.execute(
            "UPDATE feed_subscriptions SET tag = ? WHERE user_id = ? AND id = ?",
            (normalized, user_id, sub_id),
        )
        conn.commit()
    return cursor.rowcount > 0


def set_feed_paused(user_id: int, sub_id: int, is_paused: bool) -> bool:
    """Поставить подписку Ленты на паузу или снять с паузы."""
    val = 1 if is_paused else 0
    with get_connection() as conn:
        cursor = conn.execute(
            "UPDATE feed_subscriptions SET is_paused = ? WHERE user_id = ? AND id = ?",
            (val, user_id, sub_id),
        )
        conn.commit()
    return cursor.rowcount > 0


set_feed_subscription_paused = set_feed_paused


def delete_feed_subscription(user_id: int, sub_id: int) -> bool:
    """Удалить подписку из Ленты."""
    with get_connection() as conn:
        cursor = conn.execute(
            "DELETE FROM feed_subscriptions WHERE user_id = ? AND id = ?",
            (user_id, sub_id),
        )
        conn.commit()
    return cursor.rowcount > 0


def list_feed_tags(user_id: int) -> list[str]:
    """Список уникальных тегов пользователя в алфавитном порядке."""
    with get_connection() as conn:
        rows = conn.execute(
            '''
            SELECT DISTINCT tag
            FROM feed_subscriptions
            WHERE user_id = ? AND tag IS NOT NULL AND tag != ''
            ORDER BY tag COLLATE NOCASE
            ''',
            (user_id,),
        ).fetchall()
    return [row[0] for row in rows if row[0]]


get_feed_tags = list_feed_tags


def get_feed_candidate_posts(
    user_id: int,
    window_start: datetime | str,
    window_end: datetime | str,
) -> list[dict]:
    """Выбрать посты каналов активных подписок Ленты за окно [window_start, window_end] с текстом."""
    w_start = normalize_feed_datetime(window_start)
    w_end = normalize_feed_datetime(window_end)

    with get_connection() as conn:
        rows = conn.execute(
            '''
            SELECT
                cp.channel_username,
                cp.post_id,
                cp.post_text,
                cp.post_link,
                cp.post_time,
                cp.source,
                cp.created_at,
                cp.views,
                cp.reactions_total,
                cp.reactions_json,
                cp.forwards,
                cp.replies,
                cp.metrics_at,
                cp.fwd_from_channel_id,
                cp.fwd_from_post_id,
                cp.media_kind,
                cp.is_forward,
                cp.link_urls,
                c.channel_id,
                fs.channel_title,
                fs.tag
            FROM channel_posts cp
            JOIN feed_subscriptions fs ON fs.channel_username = cp.channel_username
            LEFT JOIN channels c ON c.channel_username = cp.channel_username
            WHERE fs.user_id = ?
              AND fs.is_paused = 0
              AND cp.post_time > ?
              AND cp.post_time <= ?
              AND cp.post_text IS NOT NULL
              AND TRIM(cp.post_text) != ''
            ORDER BY cp.post_time DESC, cp.channel_username, cp.post_id
            ''',
            (user_id, w_start, w_end),
        ).fetchall()

    result = []
    for row in rows:
        post_time_raw = row["post_time"]
        pt = parse_db_datetime(post_time_raw) if isinstance(post_time_raw, str) else post_time_raw
        result.append({
            "id": row["post_id"],
            "post_id": row["post_id"],
            "channel_username": row["channel_username"],
            "channel_title": row["channel_title"],
            "tag": row["tag"],
            "text": row["post_text"],
            "post_text": row["post_text"],
            "link": row["post_link"],
            "post_link": row["post_link"],
            "time": pt,
            "post_time": pt,
            "views": row["views"],
            "reactions_total": row["reactions_total"],
            "reactions_json": row["reactions_json"],
            "forwards": row["forwards"],
            "replies": row["replies"],
            "metrics_at": row["metrics_at"],
            "source": row["source"],
            "channel_id": row["channel_id"],
            "fwd_from_channel_id": row["fwd_from_channel_id"],
            "fwd_from_post_id": row["fwd_from_post_id"],
            "media_kind": row["media_kind"],
            "is_forward": row["is_forward"],
            "link_urls": row["link_urls"],
        })
    return result


def save_feed_edition(
    user_id: int,
    period: str,
    window_start: datetime | str,
    window_end: datetime | str,
    subs_hash: str,
    telegraph_url: str,
    post_count: int,
    channel_count: int,
    headlines: list[str] | None = None,
) -> int:
    """Сохранить запись о собранном выпуске Ленты в кеш (заголовки «Главного» — JSON, чтобы кеш-попадание не пересчитывало ранжирование)."""
    w_start = normalize_feed_datetime(window_start)
    w_end = normalize_feed_datetime(window_end)
    created_at = serialize_datetime(utc_now())
    with get_connection() as conn:
        cursor = conn.execute(
            '''
            INSERT INTO feed_editions (
                user_id, period, window_start, window_end, subs_hash, telegraph_url,
                post_count, channel_count, created_at, headlines
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                user_id,
                period,
                w_start,
                w_end,
                subs_hash,
                telegraph_url,
                post_count,
                channel_count,
                created_at,
                json.dumps(headlines, ensure_ascii=False) if headlines else None,
            ),
        )
        conn.commit()
        return cursor.lastrowid


def get_feed_edition(
    user_id: int,
    period: str,
    subs_hash: str,
    max_age_seconds: int | None = 900,
) -> dict | None:
    """Получить свежий выпуск Ленты из кеша (по умолчанию до 15 минут)."""
    with get_connection() as conn:
        row = conn.execute(
            '''
            SELECT id, user_id, period, window_start, window_end, subs_hash, telegraph_url,
                   post_count, channel_count, created_at, headlines
            FROM feed_editions
            WHERE user_id = ? AND period = ? AND subs_hash = ?
            ORDER BY id DESC
            LIMIT 1
            ''',
            (user_id, period, subs_hash),
        ).fetchone()

    if not row:
        return None

    if max_age_seconds is not None:
        created_dt = parse_db_datetime(row["created_at"])
        if (utc_now() - created_dt).total_seconds() > max_age_seconds:
            return None

    return dict(row)


get_feed_edition_cache = get_feed_edition


def get_feed_user_onboarding_shown(user_id: int) -> bool:
    """Проверить, был ли показан онбординг личных каналов пользователю."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT onboarding_shown_at FROM feed_user_state WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return bool(row and row["onboarding_shown_at"] is not None)


def set_feed_user_onboarding_shown(user_id: int, shown_at: datetime | str | None = None) -> None:
    """Установить отметку показа онбординга личных каналов."""
    if shown_at is None:
        val = serialize_datetime(utc_now())
    elif isinstance(shown_at, datetime):
        val = serialize_datetime(shown_at)
    else:
        val = str(shown_at)
    with get_connection() as conn:
        conn.execute(
            '''
            INSERT INTO feed_user_state (user_id, onboarding_shown_at)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                onboarding_shown_at = excluded.onboarding_shown_at
            ''',
            (user_id, val),
        )
        conn.commit()


def get_latest_feed_edition(user_id: int, period: str) -> dict | None:
    """Вернуть последний сохранённый выпуск Ленты для пользователя и периода."""
    with get_connection() as conn:
        row = conn.execute(
            '''
            SELECT id, user_id, period, window_start, window_end, subs_hash, telegraph_url,
                   post_count, channel_count, created_at, headlines
            FROM feed_editions
            WHERE user_id = ? AND period = ?
            ORDER BY created_at DESC
            LIMIT 1
            ''',
            (user_id, period),
        ).fetchone()
        return dict(row) if row else None


FEED_DELIVERY_CHANNELS = ("telegram", "email")


def enable_feed_autodelivery(
    user_id: int,
    period: str,
    now: datetime | None = None,
    *,
    send_hour: int | None = None,
    send_minute: int | None = None,
    weekday: int | None = None,
    month_day: int | None = None,
    delivery: str = "telegram",
) -> None:
    """Включить регулярную отправку Ленты для периода.

    Без своего времени (send_hour is None) расписание берётся из настроек дайджеста — так работали записи до появления
    собственного расписания Ленты. Повторный вызов с временем обновляет расписание и канал доставки (отсчёт срока идёт заново), без времени — ничего не меняет.
    """
    if period not in {"day", "week", "month"}:
        raise ValueError(f"Unsupported feed period: {period}")
    if delivery not in FEED_DELIVERY_CHANNELS:
        raise ValueError(f"Unsupported feed delivery: {delivery}")
    now_dt = now or utc_now()
    now_str = serialize_datetime(now_dt)
    conflict = (
        "DO UPDATE SET send_hour = excluded.send_hour, send_minute = excluded.send_minute, weekday = excluded.weekday, "
        "month_day = excluded.month_day, delivery = excluded.delivery, last_run_at = excluded.created_at"
        if send_hour is not None
        else "DO NOTHING"  # повторное «включить» без своего времени ничего не сбрасывает
    )
    with get_connection() as conn:
        conn.execute(
            f'''
            INSERT INTO feed_autodelivery (user_id, period, created_at, send_hour, send_minute, weekday, month_day, delivery)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, period) {conflict}
            ''',
            (user_id, period, now_str, send_hour, send_minute, weekday, month_day, delivery),
        )
        conn.commit()


def disable_feed_autodelivery(user_id: int, period: str) -> bool:
    """Отключить автодоставку Ленты для периода."""
    with get_connection() as conn:
        res = conn.execute(
            '''
            DELETE FROM feed_autodelivery
            WHERE user_id = ? AND period = ?
            ''',
            (user_id, period),
        )
        conn.commit()
        return res.rowcount > 0


def get_feed_autodelivery(user_id: int, period: str) -> dict | None:
    """Получить запись автодоставки Ленты."""
    with get_connection() as conn:
        row = conn.execute(
            '''
            SELECT id, user_id, period, last_run_at, created_at, send_hour, send_minute, weekday, month_day, COALESCE(delivery, 'telegram') AS delivery
            FROM feed_autodelivery
            WHERE user_id = ? AND period = ?
            ''',
            (user_id, period),
        ).fetchone()
        return dict(row) if row else None


def list_user_feed_autodeliveries(user_id: int) -> list[dict]:
    """Список всех включённых автодоставок пользователя (до 3 записей)."""
    with get_connection() as conn:
        rows = conn.execute(
            '''
            SELECT id, user_id, period, last_run_at, created_at, send_hour, send_minute, weekday, month_day, COALESCE(delivery, 'telegram') AS delivery
            FROM feed_autodelivery
            WHERE user_id = ?
            ORDER BY CASE period WHEN 'day' THEN 1 WHEN 'week' THEN 2 ELSE 3 END
            ''',
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def list_all_feed_autodeliveries() -> list[dict]:
    """Список всех включённых автодоставок всех пользователей для фонового цикла."""
    with get_connection() as conn:
        rows = conn.execute(
            '''
            SELECT id, user_id, period, last_run_at, created_at, send_hour, send_minute, weekday, month_day, COALESCE(delivery, 'telegram') AS delivery
            FROM feed_autodelivery
            ORDER BY id
            '''
        ).fetchall()
        return [dict(r) for r in rows]


def update_feed_autodelivery_last_run(user_id: int, period: str, last_run_at: datetime | str) -> None:
    """Обновить отметку последнего выполнения автодоставки."""
    dt_str = serialize_datetime(last_run_at) if isinstance(last_run_at, datetime) else str(last_run_at)
    with get_connection() as conn:
        conn.execute(
            '''
            UPDATE feed_autodelivery
            SET last_run_at = ?
            WHERE user_id = ? AND period = ?
            ''',
            (dt_str, user_id, period),
        )
        conn.commit()


def checkpoint_db() -> tuple[int, int, int]:
    """Сбрасывает WAL-лог в основную БД через PRAGMA wal_checkpoint(TRUNCATE).

    Возвращает (busy, log, checkpointed).
    """
    with get_connection() as conn:
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        return tuple(row) if row else (0, 0, 0)

