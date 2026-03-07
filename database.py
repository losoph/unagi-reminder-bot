import sqlite3
import os
from datetime import datetime

# НОВАЯ ЛОГИКА ПУТИ:
# Берем путь из настроек сервера. Если его нет (мы тестируем на Маке) — используем 'bot_data.db'
DB_PATH = os.getenv("DB_PATH", "bot_data.db")

def init_db():
    # Теперь везде используем DB_PATH вместо жестко заданного имени файла
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

def get_pending_messages():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    cursor.execute('''
        SELECT id, user_id, message_id FROM scheduled_messages 
        WHERE send_at <= ? AND is_sent = 0
    ''', (now,))
    
    rows = cursor.fetchall()
    conn.close()
    return rows

def mark_as_sent(msg_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('UPDATE scheduled_messages SET is_sent = 1 WHERE id = ?', (msg_id,))
    conn.commit()
    conn.close()

if __name__ == '__main__':
    init_db()