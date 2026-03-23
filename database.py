import sqlite3
import os
from datetime import datetime

DB_PATH = os.getenv("DB_PATH", "bot_data.db")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS scheduled_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            message_id INTEGER,
            send_at DATETIME,
            is_sent INTEGER DEFAULT 0,
            text_preview TEXT,
            source_name TEXT,
            delivery_status TEXT DEFAULT 'pending',
            retry_count INTEGER DEFAULT 0,
            last_error TEXT,
            last_attempt_at DATETIME
        )
    ''')
    
    try:
        cursor.execute("ALTER TABLE scheduled_messages ADD COLUMN text_preview TEXT")
        cursor.execute("ALTER TABLE scheduled_messages ADD COLUMN source_name TEXT")
    except sqlite3.OperationalError:
        pass 

    for statement in (
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
    
    conn.commit()
    conn.close()
    print(f"База данных успешно инициализирована по пути: {DB_PATH}")

def add_message(user_id, message_id, send_at, text_preview="", source_name=""):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO scheduled_messages (user_id, message_id, send_at, text_preview, source_name, delivery_status) 
        VALUES (?, ?, ?, ?, ?, 'pending')
    ''', (user_id, message_id, send_at, text_preview, source_name))
    conn.commit()
    conn.close()

def get_pending_messages(current_time_str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, user_id, message_id, retry_count
        FROM scheduled_messages
        WHERE send_at <= ? AND is_sent = 0 AND delivery_status != 'failed_permanent'
    ''', 
                   (current_time_str,))
    rows = cursor.fetchall()
    conn.close()
    return rows

def mark_as_sent(msg_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE scheduled_messages
        SET is_sent = 1, delivery_status = 'sent', last_error = NULL
        WHERE id = ?
    ''', (msg_id,))
    conn.commit()
    conn.close()

def mark_message_delivery_error(msg_id, error_text, attempted_at, retry_count, is_permanent):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE scheduled_messages
        SET delivery_status = ?, retry_count = ?, last_error = ?, last_attempt_at = ?
        WHERE id = ?
    ''', (
        'failed_permanent' if is_permanent else 'failed_temporary',
        retry_count,
        error_text,
        attempted_at,
        msg_id,
    ))
    conn.commit()
    conn.close()

def get_user_messages(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, send_at, text_preview, source_name 
        FROM scheduled_messages 
        WHERE user_id = ? AND is_sent = 0 
        ORDER BY send_at
    ''', (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows

def delete_message(msg_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('DELETE FROM scheduled_messages WHERE id = ?', (msg_id,))
    conn.commit()
    conn.close()

def add_subscription(user_id, channel_username, channel_title, period, last_scraped_at, next_send_at):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO subscriptions (user_id, channel_username, channel_title, period, last_scraped_at, next_send_at)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (user_id, channel_username, channel_title, period, last_scraped_at, next_send_at))
    conn.commit()
    conn.close()

def get_due_subscriptions(current_time_str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, user_id, channel_username, channel_title, period, last_scraped_at, failure_count
        FROM subscriptions 
        WHERE next_send_at <= ? AND is_disabled = 0
    ''', (current_time_str,))
    rows = cursor.fetchall()
    conn.close()
    return rows

def update_subscription_time(sub_id, last_scraped_at, next_send_at):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE subscriptions 
        SET last_scraped_at = ?, next_send_at = ?, digest_status = 'active',
            failure_count = 0, last_error = NULL, last_attempt_at = ?
        WHERE id = ?
    ''', (last_scraped_at, next_send_at, last_scraped_at, sub_id))
    conn.commit()
    conn.close()

def mark_subscription_delivery_error(sub_id, error_text, attempted_at, failure_count, is_permanent):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE subscriptions
        SET digest_status = ?, failure_count = ?, last_error = ?, last_attempt_at = ?, is_disabled = ?
        WHERE id = ?
    ''', (
        'failed_permanent' if is_permanent else 'failed_temporary',
        failure_count,
        error_text,
        attempted_at,
        1 if is_permanent else 0,
        sub_id,
    ))
    conn.commit()
    conn.close()

def get_user_subscriptions(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, channel_username, channel_title, period, next_send_at
        FROM subscriptions
        WHERE user_id = ? AND is_disabled = 0
        ORDER BY next_send_at
    ''', 
                   (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows

def delete_subscription(sub_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('DELETE FROM subscriptions WHERE id = ?', (sub_id,))
    conn.commit()
    conn.close()

def add_saved_message(user_id, full_text, source_name, tag, saved_at):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO saved_messages (user_id, full_text, source_name, tag, saved_at) 
        VALUES (?, ?, ?, ?, ?)
    ''', (user_id, full_text, source_name, tag, saved_at))
    conn.commit()
    conn.close()

def get_user_tags(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT DISTINCT tag FROM saved_messages WHERE user_id = ?', (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return [r[0] for r in rows if r[0]]

def get_saved_messages(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT id, tag, full_text, source_name, saved_at FROM saved_messages WHERE user_id = ? ORDER BY tag, saved_at DESC', (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_saved_message_by_id(msg_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT full_text, source_name, tag, saved_at FROM saved_messages WHERE id = ?', (msg_id,))
    row = cursor.fetchone()
    conn.close()
    return row

def delete_saved_message(msg_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('DELETE FROM saved_messages WHERE id = ?', (msg_id,))
    conn.commit()
    conn.close()
