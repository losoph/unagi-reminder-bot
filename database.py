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
            is_sent INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()
    print(f"База данных успешно инициализирована по пути: {DB_PATH}")

def add_message(user_id, message_id, send_at):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO scheduled_messages (user_id, message_id, send_at)
        VALUES (?, ?, ?)
    ''', (user_id, message_id, send_at))
    conn.commit()
    conn.close()

# ИЗМЕНЕНИЕ: Теперь функция принимает точное время от бота
def get_pending_messages(current_time_str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, user_id, message_id FROM scheduled_messages 
        WHERE send_at <= ? AND is_sent = 0
    ''', (current_time_str,))
    rows = cursor.fetchall()
    conn.close()
    return rows

def mark_as_sent(msg_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('UPDATE scheduled_messages SET is_sent = 1 WHERE id = ?', (msg_id,))
    conn.commit()
    conn.close()

# --- НОВЫЙ ФУНКЦИОНАЛ ДЛЯ /list ---

def get_user_messages(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, send_at FROM scheduled_messages 
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