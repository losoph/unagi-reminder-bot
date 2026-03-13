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
            source_name TEXT
        )
    ''')
    
    # Умное обновление старой базы данных
    try:
        cursor.execute("ALTER TABLE scheduled_messages ADD COLUMN text_preview TEXT")
        cursor.execute("ALTER TABLE scheduled_messages ADD COLUMN source_name TEXT")
    except sqlite3.OperationalError:
        pass # Если колонки уже есть, Питон просто проигнорирует ошибку
        
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            channel_username TEXT,
            channel_title TEXT,
            period TEXT,
            last_scraped_at DATETIME,
            next_send_at DATETIME
        )
    ''')
    
    conn.commit()
    conn.close()
    print(f"База данных успешно инициализирована по пути: {DB_PATH}")

# ИЗМЕНЕНИЕ: добавили текст и источник
def add_message(user_id, message_id, send_at, text_preview="", source_name=""):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO scheduled_messages (user_id, message_id, send_at, text_preview, source_name) 
        VALUES (?, ?, ?, ?, ?)
    ''', (user_id, message_id, send_at, text_preview, source_name))
    conn.commit()
    conn.close()

def get_pending_messages(current_time_str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT id, user_id, message_id FROM scheduled_messages WHERE send_at <= ? AND is_sent = 0', 
                   (current_time_str,))
    rows = cursor.fetchall()
    conn.close()
    return rows

def mark_as_sent(msg_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('UPDATE scheduled_messages SET is_sent = 1 WHERE id = ?', (msg_id,))
    conn.commit()
    conn.close()

# ИЗМЕНЕНИЕ: достаем текст и источник
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
        SELECT id, user_id, channel_username, channel_title, period, last_scraped_at 
        FROM subscriptions 
        WHERE next_send_at <= ?
    ''', (current_time_str,))
    rows = cursor.fetchall()
    conn.close()
    return rows

def update_subscription_time(sub_id, last_scraped_at, next_send_at):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE subscriptions 
        SET last_scraped_at = ?, next_send_at = ? 
        WHERE id = ?
    ''', (last_scraped_at, next_send_at, sub_id))
    conn.commit()
    conn.close()

def get_user_subscriptions(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT id, channel_username, channel_title, period, next_send_at FROM subscriptions WHERE user_id = ? ORDER BY next_send_at', 
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