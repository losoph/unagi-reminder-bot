import os
import sqlite3

from config import DEFAULT_USER_TIMEZONE_NAME

DB_PATH = os.getenv("DB_PATH", "bot_data.db")


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        f'''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            timezone TEXT NOT NULL DEFAULT '{DEFAULT_USER_TIMEZONE_NAME}'
        )
    '''
    )

    cursor.execute(
        '''
        CREATE TABLE IF NOT EXISTS scheduled_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            message_id INTEGER,
            send_at TEXT,
            is_sent INTEGER DEFAULT 0,
            text_preview TEXT,
            source_name TEXT,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
    '''
    )

    cursor.execute(
        '''
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            channel_username TEXT,
            channel_title TEXT,
            period TEXT,
            last_scraped_at TEXT,
            next_send_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
    '''
    )

    cursor.execute(
        '''
        CREATE TABLE IF NOT EXISTS saved_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            full_text TEXT,
            source_name TEXT,
            tag TEXT,
            saved_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
    '''
    )

    try:
        cursor.execute("ALTER TABLE scheduled_messages ADD COLUMN text_preview TEXT")
        cursor.execute("ALTER TABLE scheduled_messages ADD COLUMN source_name TEXT")
    except sqlite3.OperationalError:
        pass

    try:
        cursor.execute(
            f"ALTER TABLE users ADD COLUMN timezone TEXT NOT NULL DEFAULT '{DEFAULT_USER_TIMEZONE_NAME}'"
        )
    except sqlite3.OperationalError:
        pass

    conn.commit()
    conn.close()
    print(f"База данных успешно инициализирована по пути: {DB_PATH}")


def ensure_user(user_id, timezone_name=DEFAULT_USER_TIMEZONE_NAME):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        '''
        INSERT INTO users (user_id, timezone)
        VALUES (?, ?)
        ON CONFLICT(user_id) DO NOTHING
    ''',
        (user_id, timezone_name),
    )
    conn.commit()
    conn.close()



def get_user_timezone_name(user_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT timezone FROM users WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row and row[0] else DEFAULT_USER_TIMEZONE_NAME



def set_user_timezone(user_id, timezone_name):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        '''
        INSERT INTO users (user_id, timezone)
        VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET timezone = excluded.timezone
    ''',
        (user_id, timezone_name),
    )
    conn.commit()
    conn.close()



def add_message(user_id, message_id, send_at, text_preview="", source_name=""):
    ensure_user(user_id)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        '''
        INSERT INTO scheduled_messages (user_id, message_id, send_at, text_preview, source_name)
        VALUES (?, ?, ?, ?, ?)
    ''',
        (user_id, message_id, send_at, text_preview, source_name),
    )
    conn.commit()
    conn.close()



def get_pending_messages(current_time_str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        'SELECT id, user_id, message_id FROM scheduled_messages WHERE send_at <= ? AND is_sent = 0',
        (current_time_str,),
    )
    rows = cursor.fetchall()
    conn.close()
    return rows



def mark_as_sent(msg_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE scheduled_messages SET is_sent = 1 WHERE id = ?', (msg_id,))
    conn.commit()
    conn.close()



def get_user_messages(user_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        '''
        SELECT id, send_at, text_preview, source_name
        FROM scheduled_messages
        WHERE user_id = ? AND is_sent = 0
        ORDER BY send_at
    ''',
        (user_id,),
    )
    rows = cursor.fetchall()
    conn.close()
    return rows



def delete_message(msg_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM scheduled_messages WHERE id = ?', (msg_id,))
    conn.commit()
    conn.close()



def add_subscription(user_id, channel_username, channel_title, period, last_scraped_at, next_send_at):
    ensure_user(user_id)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        '''
        INSERT INTO subscriptions (user_id, channel_username, channel_title, period, last_scraped_at, next_send_at)
        VALUES (?, ?, ?, ?, ?, ?)
    ''',
        (user_id, channel_username, channel_title, period, last_scraped_at, next_send_at),
    )
    conn.commit()
    conn.close()



def get_due_subscriptions(current_time_str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        '''
        SELECT id, user_id, channel_username, channel_title, period, last_scraped_at
        FROM subscriptions
        WHERE next_send_at <= ?
    ''',
        (current_time_str,),
    )
    rows = cursor.fetchall()
    conn.close()
    return rows



def update_subscription_time(sub_id, last_scraped_at, next_send_at):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        '''
        UPDATE subscriptions
        SET last_scraped_at = ?, next_send_at = ?
        WHERE id = ?
    ''',
        (last_scraped_at, next_send_at, sub_id),
    )
    conn.commit()
    conn.close()



def get_user_subscriptions(user_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        'SELECT id, channel_username, channel_title, period, next_send_at FROM subscriptions WHERE user_id = ? ORDER BY next_send_at',
        (user_id,),
    )
    rows = cursor.fetchall()
    conn.close()
    return rows



def delete_subscription(sub_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM subscriptions WHERE id = ?', (sub_id,))
    conn.commit()
    conn.close()



def add_saved_message(user_id, full_text, source_name, tag, saved_at):
    ensure_user(user_id)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        '''
        INSERT INTO saved_messages (user_id, full_text, source_name, tag, saved_at)
        VALUES (?, ?, ?, ?, ?)
    ''',
        (user_id, full_text, source_name, tag, saved_at),
    )
    conn.commit()
    conn.close()



def get_user_tags(user_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT DISTINCT tag FROM saved_messages WHERE user_id = ?', (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return [r[0] for r in rows if r[0]]



def get_saved_messages(user_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        'SELECT id, tag, full_text, source_name, saved_at FROM saved_messages WHERE user_id = ? ORDER BY tag, saved_at DESC',
        (user_id,),
    )
    rows = cursor.fetchall()
    conn.close()
    return rows



def get_saved_message_by_id(msg_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT full_text, source_name, tag, saved_at FROM saved_messages WHERE id = ?', (msg_id,))
    row = cursor.fetchone()
    conn.close()
    return row



def delete_saved_message(msg_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM saved_messages WHERE id = ?', (msg_id,))
    conn.commit()
    conn.close()
