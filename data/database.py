import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

UTC = timezone.utc
DB_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"
LEGACY_STORAGE_OFFSET_HOURS = 3
MESSAGE_RETENTION_DAYS = int(os.getenv("MESSAGE_RETENTION_DAYS", "30"))
SUBSCRIPTION_FAILURE_RETENTION_DAYS = int(os.getenv("SUBSCRIPTION_FAILURE_RETENTION_DAYS", "90"))
MAX_ERROR_TEXT_LENGTH = int(os.getenv("MAX_ERROR_TEXT_LENGTH", "800"))

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = os.getenv("DB_PATH", str(BASE_DIR / "bot_data.db"))


@contextmanager
def get_connection():
    if DB_PATH != ":memory:":
        Path(DB_PATH).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("PRAGMA cache_size = -64000")
        yield conn
    finally:
        conn.close()


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


def init_db():
    with get_connection() as conn:
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
                last_attempt_at DATETIME
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
            "DROP INDEX IF EXISTS uq_subscriptions_user_channel_period_active"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_saved_messages_user_tag_date ON saved_messages(user_id, tag, saved_at)"
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
        _deduplicate_active_subscriptions(cursor)
        cursor.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_subscriptions_user_channel_period_active
            ON subscriptions(user_id, channel_username, period)
            WHERE is_disabled = 0
            """
        )
        conn.commit()


def add_message(user_id, message_id, send_at, text_preview="", source_name=""):
    created_at = serialize_datetime(utc_now())
    with get_connection() as conn:
        conn.execute(
            '''
            INSERT INTO scheduled_messages (
                user_id, message_id, send_at, created_at, text_preview, source_name, delivery_status
            )
            VALUES (?, ?, ?, ?, ?, ?, 'pending')
            ''',
            (user_id, message_id, send_at, created_at, text_preview, source_name),
        )
        conn.commit()


def get_pending_messages(current_time_str):
    with get_connection() as conn:
        rows = conn.execute(
            '''
            SELECT id, user_id, message_id, retry_count
            FROM scheduled_messages
            WHERE send_at <= ? AND is_sent = 0 AND delivery_status != 'failed_permanent'
            ''',
            (current_time_str,),
        ).fetchall()
    return rows


def mark_as_sent(msg_id, delivered_message_id=None):
    with get_connection() as conn:
        conn.execute(
            '''
            UPDATE scheduled_messages
            SET is_sent = 1, delivered_message_id = ?, delivery_status = 'sent', last_error = NULL
            WHERE id = ?
            ''',
            (delivered_message_id, msg_id),
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


def get_user_messages(user_id):
    with get_connection() as conn:
        rows = conn.execute(
            '''
            SELECT id, send_at, text_preview, source_name
            FROM scheduled_messages
            WHERE user_id = ? AND is_sent = 0
            ORDER BY send_at
            ''',
            (user_id,),
        ).fetchall()
    return rows


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
    message_id: int,
    send_at: str,
    text_preview: str = "",
    source_name: str = "",
) -> None:
    """Atomically insert a new pending reminder and remove the delivered row."""
    created_at = serialize_datetime(utc_now())
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """
                INSERT INTO scheduled_messages (
                    user_id, message_id, send_at, created_at, text_preview, source_name, delivery_status
                )
                VALUES (?, ?, ?, ?, ?, ?, 'pending')
                """,
                (user_id, message_id, send_at, created_at, text_preview, source_name),
            )
            cur = conn.execute(
                "DELETE FROM scheduled_messages WHERE id = ? AND user_id = ?",
                (old_db_id, user_id),
            )
            if cur.rowcount != 1:
                raise ValueError("Scheduled row missing or already changed")
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def get_scheduled_message_by_delivered_message_id(user_id, delivered_message_id):
    with get_connection() as conn:
        row = conn.execute(
            '''
            SELECT id, message_id, delivered_message_id, send_at, text_preview, source_name, is_sent
            FROM scheduled_messages
            WHERE user_id = ? AND delivered_message_id = ? AND is_sent = 1
            ORDER BY id DESC
            LIMIT 1
            ''',
            (user_id, delivered_message_id),
        ).fetchone()
    return row


def add_subscription(user_id, channel_username, channel_title, period, last_scraped_at, next_send_at):
    normalized_username = normalize_channel_username(channel_username)
    created_at = serialize_datetime(utc_now())
    with get_connection() as conn:
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
            conn.execute(
                """
                UPDATE subscriptions
                SET channel_username = ?, channel_title = ?, last_scraped_at = ?, next_send_at = ?, digest_status = 'active',
                    failure_count = 0, last_error = NULL, last_attempt_at = NULL
                WHERE id = ?
                """,
                (normalized_username, channel_title, last_scraped_at, next_send_at, existing_row[0]),
            )
        else:
            conn.execute(
                '''
                INSERT INTO subscriptions (
                    user_id, channel_username, channel_title, period, created_at, last_scraped_at, next_send_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ''',
                (user_id, normalized_username, channel_title, period, created_at, last_scraped_at, next_send_at),
            )
        conn.commit()


def get_due_subscriptions(current_time_str):
    with get_connection() as conn:
        rows = conn.execute(
            '''
            SELECT id, user_id, channel_username, channel_title, period, last_scraped_at, failure_count
            FROM subscriptions
            WHERE next_send_at <= ? AND is_disabled = 0
            ''',
            (current_time_str,),
        ).fetchall()
    return rows


def update_subscription_time(sub_id, last_scraped_at, next_send_at):
    with get_connection() as conn:
        conn.execute(
            '''
            UPDATE subscriptions
            SET last_scraped_at = ?, next_send_at = ?, digest_status = 'active',
                failure_count = 0, last_error = NULL, last_attempt_at = ?
            WHERE id = ?
            ''',
            (last_scraped_at, next_send_at, last_scraped_at, sub_id),
        )
        conn.commit()


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
            SELECT id, channel_username, channel_title, period, next_send_at
            FROM subscriptions
            WHERE user_id = ? AND is_disabled = 0
            ORDER BY next_send_at
            ''',
            (user_id,),
        ).fetchall()
    return rows


def delete_subscription(user_id, sub_id):
    with get_connection() as conn:
        conn.execute(
            'DELETE FROM subscriptions WHERE id = ? AND user_id = ?',
            (sub_id, user_id),
        )
        conn.commit()


def add_saved_message(user_id, full_text, source_name, tag, saved_at):
    with get_connection() as conn:
        conn.execute(
            '''
            INSERT INTO saved_messages (user_id, full_text, source_name, tag, saved_at)
            VALUES (?, ?, ?, ?, ?)
            ''',
            (user_id, full_text, source_name, tag, saved_at),
        )
        conn.commit()


def get_user_tags(user_id):
    with get_connection() as conn:
        rows = conn.execute(
            'SELECT DISTINCT tag FROM saved_messages WHERE user_id = ? ORDER BY tag COLLATE NOCASE',
            (user_id,),
        ).fetchall()
    return [row[0] for row in rows if row[0]]


def get_saved_messages(user_id):
    with get_connection() as conn:
        rows = conn.execute(
            '''
            SELECT id, tag, full_text, source_name, saved_at
            FROM saved_messages
            WHERE user_id = ?
            ORDER BY tag, saved_at DESC
            ''',
            (user_id,),
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


def cleanup_old_records(now_str: str) -> dict[str, int]:
    with get_connection() as conn:
        scheduled_sent_deleted = conn.execute(
            """
            DELETE FROM scheduled_messages
            WHERE is_sent = 1
              AND created_at < datetime(?, ?)
            """,
            (now_str, f"-{MESSAGE_RETENTION_DAYS} days"),
        ).rowcount
        scheduled_failed_deleted = conn.execute(
            """
            DELETE FROM scheduled_messages
            WHERE delivery_status = 'failed_permanent'
              AND created_at < datetime(?, ?)
            """,
            (now_str, f"-{MESSAGE_RETENTION_DAYS} days"),
        ).rowcount
        subscriptions_failed_deleted = conn.execute(
            """
            DELETE FROM subscriptions
            WHERE is_disabled = 1
              AND digest_status = 'failed_permanent'
              AND COALESCE(last_attempt_at, created_at) < datetime(?, ?)
            """,
            (now_str, f"-{SUBSCRIPTION_FAILURE_RETENTION_DAYS} days"),
        ).rowcount
        conn.commit()

    return {
        "scheduled_sent_deleted": scheduled_sent_deleted,
        "scheduled_failed_deleted": scheduled_failed_deleted,
        "subscriptions_failed_deleted": subscriptions_failed_deleted,
    }
