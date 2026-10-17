# Проект Очереди 4: несколько расписаний дайджеста на папку

- **Статус**: Проект согласован с решениями владельца ([roadmap.md](../roadmap.md) §2, [research/digest-schedules-architecture.md](../research/digest-schedules-architecture.md)).
- **Цель**: Переход от плоской модели «подписка с периодом» к иерархической модели **«папка → 1..N расписаний → выпуск (снапшот до отправки)»**.
- **Границы этапа**: Текущая задача — исключительно проектная документация (`docs/sprint/queue4-design.md`), read-only скрипт проверки базы (`ops/migrate_schedules_dryrun.py`) и тесты к нему (`tests/test_migrate_schedules_dryrun.py`). Код бота (`main.py`, `data/database.py`, `digest_*.py`) не модифицируется. Реализация схемы и логики запланирована в задачах T20–T23; T19 (бэкап SQLite перед деплоем) уже выполнен и задачей этой нарезки не является (§7).

---

## 1. Как сейчас устроено в коде

### 1.1. Таблицы базы данных (`data/database.py:init_db`)
1. **`subscriptions`**:
   - Поля: `id`, `user_id`, `channel_username`, `channel_title`, `period` (`'daily'` | `'weekly'` | `'monthly'`), `created_at`, `last_scraped_at`, `next_send_at`, `digest_status`, `failure_count`, `last_error`, `last_attempt_at`, `is_disabled`, `is_paused`, `tag`, `last_post_id`.
   - Индексы: `idx_subscriptions_due (next_send_at, is_disabled)`, `idx_subscriptions_user_active (user_id, is_disabled, next_send_at)`.
   - Ограничения: Нет ограничения уникальности `(user_id, channel_username)`. Пользователь может быть подписан на один и тот же канал в разных периодах (например, `daily` и `weekly`) и с разными тегами (`tag`).
2. **`digest_settings`**:
   - Поля: `user_id`, `period`, `send_hour` (default 7), `send_minute` (default 0), `weekday`, `month_day`, `monthly_mode` (default `'date'`).
   - Ключ: `PRIMARY KEY (user_id, period)`. Ровно одна настройка времени на период для каждого пользователя.
3. **`user_emails`**:
   - Поля: `user_id PRIMARY KEY`, `email`, `email_periods` (строка через запятую, например `'daily,weekly'`), `pending_email`, `verify_code`, `code_sent_at`, `code_expires_at`, `code_attempts`.
   - Эксклюзивность доставки: если период указан в `email_periods` и адрес подтверждён, дайджест уходит на почту, иначе в Telegram (`main.py:get_email_delivery_periods`).
4. **`digest_executions`**:
   - Поля: `id`, `user_id`, `period`, `scheduled_at`, `started_at`, `finished_at`, `status` (`'delivered'` | `'empty'` | `'partial'` | `'retrying'` | `'failed'`), `posts_count`, `channels_count`, `channels_failed`, `channels_partial`, `error_message`, `retry_count`, `created_at`.
   - Уникальность: `uq_digest_executions_user_period_sched UNIQUE(user_id, period, scheduled_at)`.
5. **`digest_alerts`**:
   - Поля: `id`, `user_id`, `period`, `scheduled_at`, `alert_type`, `sent_at`, `message_id`, `error_text`, `created_at`.
   - Уникальность: `UNIQUE(user_id, period, scheduled_at, alert_type)`.
6. **`channel_posts`**:
   - Общее хранилище постов каналов: `(channel_username, post_id) PRIMARY KEY`, `post_text`, `post_link`, `post_time`, `views`, `reactions_total`, `forwards`, `replies`, `metrics_at`, `fwd_from_channel_id`, `fwd_from_post_id`.
7. **`channels`**:
   - Метаданные каналов и общий курсор сканирования: `channel_username PRIMARY KEY`, `subscribers_count`, `subscribers_updated_at`, `channel_id`, `feed_backfilled_at`, `last_scanned_at`, `last_scanned_post_id`.
8. **`digest_posts`**:
   - База знаний доставленных постов: `(user_id, post_link) UNIQUE`, `post_time`, `channel_username`.

### 1.2. Функции и конвейер доставки
- **Выборка due-подписок**: `get_due_subscriptions(current_time_str, limit)` выбирает строки `subscriptions` с `next_send_at <= now` и `is_disabled = 0 AND is_paused = 0`.
- **Группировка в выпуск**: `plan_due_editions(due_subs, now)` (`main.py:5467`) группирует подписки по `user_id`. Использует `digest_edition.plan_daily_edition`:
  - В один календарный день (`local_day_end`, в `APP_TIMEZONE`) формируется **один** выпуск пользователя по времени старшего периода (`monthly` > `weekly` > `daily`).
  - Младшие подписки, наступившие раньше, откладываются (`defer_subscriptions`) до времени старшего.
  - Подписки этого же дня, назначенные на более позднее время, подтягиваются в текущий выпуск (`get_user_subscriptions_due_until`).
- **Сбор постов**: `prefetch_due_channels` вычитывает посты каналов батчем.
- **Курсоры доставки**:
  - `subscriptions.last_post_id` обновляется строго после успешной отправки (`_finish_period_execution` → `update_subscription_time`).
  - Если постов нет, обновляется только расписание следующего запуска (`update_subscription_schedule`).
- **Слабые места текущей схемы**:
  1. *Неидемпотентность отправки*: состав выпуска не сохраняется до отправки. При падении бота между отправкой сообщения в Telegram и обновлением `subscriptions.last_post_id` повторный запуск пересоберёт и заново отправит те же самые посты.
  2. *Ключевание `scheduled_at`*: время execution берётся как `min(sub.next_send_at)` по подпискам периода (`main.py:5280`). При наличии каналов с рассинхронизированным `next_send_at` это порождает дробление execution-записей.
  3. *Жёсткая связь канала с периодом*: канал не может иметь несколько независимых расписаний доставки (например, утренний короткий дайджест и вечерний полный).
  4. *Тег — просто текстовое поле*: нет сущности папки, нельзя задать расписание на тег.

---

## 2. Целевая схема данных (Target Schema DDL)

Модель: **папка (`folders`) → 1..N расписаний (`digest_schedules`) → выпуск (`digest_editions` + `digest_edition_items`)**. Канал привязан к подписке, подписка строго в одной папке (`subscriptions.folder_id`).
**Курсор доставки**: перенесён на независимую пару (расписание, подписка) в новую таблицу `digest_schedule_cursors`. Поле `subscriptions.last_post_id` сохраняется исключительно для обратной совместимости legacy-планировщика на время поэтапного перехода T20→T23.

> **Развилка по слотам (Q4D-fix2, открыта — §8 п.7).** Базовая DDL ниже описывает вариант А: одно время на расписание, независимые курсоры разных расписаний. Если владелец выберет вариант В («расписание = набор слотов с одним курсором»), к `digest_schedules` добавляется `times TEXT` (JSON-массив локальных времён, например `["08:00","19:00"]`) либо дочерняя таблица `digest_schedule_slots(schedule_id, send_hour, send_minute, ...)`; `next_send_at` по-прежнему хранит ближайший будущий слот. Курсор `digest_schedule_cursors` остаётся один на расписание, `UNIQUE(schedule_id, scheduled_at)` и правила §2.3/§5 не меняются, а разные расписания папки (daily и weekly, Telegram и Email) сохраняют раздельные курсоры.

```mermaid
erDiagram
    users ||--o{ folders : owns
    folders ||--o{ subscriptions : contains
    folders ||--o{ digest_schedules : configures
    digest_schedules ||--o{ digest_schedule_cursors : tracks
    subscriptions ||--o{ digest_schedule_cursors : references
    digest_schedules ||--o{ digest_editions : generates
    digest_editions ||--o{ digest_edition_items : snapshots
    subscriptions ||--o{ digest_edition_items : feeds
    digest_schedules ||--o{ digest_executions : tracks
    digest_schedules ||--o{ digest_alerts : alerts
```

### 2.1. DDL новых таблиц

```sql
-- 1. Папки подписок (бывшие теги)
CREATE TABLE IF NOT EXISTS folders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    is_default INTEGER NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL,
    UNIQUE(user_id, name)
);
CREATE INDEX IF NOT EXISTS idx_folders_user ON folders(user_id, is_default);

-- 2. Расписания дайджестов на папку (1..N расписаний на папку)
CREATE TABLE IF NOT EXISTS digest_schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    folder_id INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    name TEXT,                                  -- Необязательное понятное имя: "Утренний", "Итоги недели"
    period TEXT NOT NULL,                       -- 'daily' | 'weekly' | 'monthly'
    send_hour INTEGER NOT NULL DEFAULT 9,       -- 0..23
    send_minute INTEGER NOT NULL DEFAULT 0,     -- 0..59
    weekdays TEXT,                              -- Для weekly: '0'..'6' (0=Пн, 6=Вс) или список '0,1,2,3,4'
    month_day INTEGER,                          -- Для monthly: 1..31
    monthly_mode TEXT DEFAULT 'date',           -- 'date' | 'last_day'
    delivery TEXT NOT NULL DEFAULT 'telegram',  -- 'telegram' | 'email' (эксклюзивно)
    email_address TEXT,                         -- Зафиксированный подтверждённый адрес для email-доставки
    is_enabled INTEGER NOT NULL DEFAULT 1,
    next_send_at DATETIME NOT NULL,             -- Время следующего планового срабатывания в UTC
    last_edition_id INTEGER,                    -- Ссылка на последний созданный выпуск
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_digest_schedules_due ON digest_schedules(is_enabled, next_send_at);
CREATE INDEX IF NOT EXISTS idx_digest_schedules_folder ON digest_schedules(folder_id, is_enabled);
CREATE INDEX IF NOT EXISTS idx_digest_schedules_user ON digest_schedules(user_id, is_enabled);

-- 3. Выпуски дайджеста (Снапшот до отправки, гарантия идемпотентности)
CREATE TABLE IF NOT EXISTS digest_editions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id INTEGER NOT NULL REFERENCES digest_schedules(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    folder_id INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
    scheduled_at DATETIME NOT NULL,             -- Плановый таймслот выпуска
    status TEXT NOT NULL,                       -- 'pending' | 'building' | 'ready' | 'delivering' | 'delivered' | 'partial' | 'empty' | 'failed' | 'retrying'
    delivery TEXT NOT NULL,                     -- 'telegram' | 'email'
    delivery_target TEXT,                       -- chat_id или email-адрес
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
    UNIQUE(schedule_id, scheduled_at)
);
CREATE INDEX IF NOT EXISTS idx_digest_editions_user_status ON digest_editions(user_id, status);
CREATE INDEX IF NOT EXISTS idx_digest_editions_sched_status ON digest_editions(scheduled_at, status);

-- 4. Состав выпуска (Зафиксированные посты снапшота)
CREATE TABLE IF NOT EXISTS digest_edition_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    edition_id INTEGER NOT NULL REFERENCES digest_editions(id) ON DELETE CASCADE,
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    channel_username TEXT NOT NULL,
    post_id INTEGER NOT NULL,
    post_time DATETIME NOT NULL,
    post_title TEXT,
    post_link TEXT NOT NULL,
    score REAL DEFAULT 0.0,
    rank INTEGER,
    created_at DATETIME NOT NULL,
    UNIQUE(edition_id, channel_username, post_id)
);
CREATE INDEX IF NOT EXISTS idx_edition_items_edition ON digest_edition_items(edition_id);
CREATE INDEX IF NOT EXISTS idx_edition_items_sub ON digest_edition_items(subscription_id);

-- 5. Курсоры доставки на пару (расписание, подписка)
CREATE TABLE IF NOT EXISTS digest_schedule_cursors (
    schedule_id INTEGER NOT NULL REFERENCES digest_schedules(id) ON DELETE CASCADE,
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    last_post_id INTEGER,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY(schedule_id, subscription_id)
);
CREATE INDEX IF NOT EXISTS idx_schedule_cursors_sub ON digest_schedule_cursors(subscription_id);
```

### 2.2. Модификация существующих таблиц и правила курсоров
1. **`subscriptions`**:
   - Добавляется поле `folder_id INTEGER REFERENCES folders(id) ON DELETE RESTRICT`.
   - Инвариант: канал строго в одной папке на пользователя:
     ```sql
     CREATE UNIQUE INDEX IF NOT EXISTS uq_subscriptions_user_channel ON subscriptions(user_id, channel_username);
     ```
   - Колонка `tag` остаётся для обратной совместимости на время переходного периода (read-only fallback).
   - Колонка `period` объявляется устаревшей (deprecated legacy). Расписание теперь привязано к папке через `digest_schedules`.
   - Поле `subscriptions.last_post_id` **остаётся только для legacy-планировщика** на время перехода. Для нового планировщика источником правды является `digest_schedule_cursors.last_post_id`.
   - **Backfill курсоров**: при миграции T20 и при добавлении нового расписания/подписки в папку в `digest_schedule_cursors` создаётся запись со значением `last_post_id = subscriptions.last_post_id` (или `NULL`, если подписка новая).
2. **`digest_executions` / `digest_alerts`**:
   - В T22 добавляется `schedule_id INTEGER REFERENCES digest_schedules(id)`.
   - Ключевание переводится на `(schedule_id, scheduled_at)`.
   - Существующие записи остаются как legacy (с `schedule_id IS NULL`, старые `user_id, period, scheduled_at` не трогаются).

### 2.3. Правило движения курсора доставки
Курсор пары `(schedule_id, subscription_id)` двигается **ТОЛЬКО** для каналов, чьи посты реально вошли в доставленный снапшот, и **строго в той же SQL-транзакции**, что и фиксация статуса `delivered` (или `partial`) с выставлением `delivered_at`.

| Статус выпуска (`digest_editions.status`) | Действие с курсорами `digest_schedule_cursors` | Транзакционность и детали |
|---|---|---|
| `delivered` | **Двигаются** до `MAX(post_id)` из снапшота для каждой подписки, чьи посты вошли в выпуск. | Выполняется атомарно в одной транзакции: `UPDATE digest_editions SET status='delivered', delivered_at=...` + `UPDATE digest_schedule_cursors SET last_post_id=... WHERE schedule_id=? AND subscription_id=?`. Если у подписки в выпуске не было постов, её курсор не меняется. |
| `partial` | **Двигаются ЧАСТИЧНО** — ТОЛЬКО для подписок/каналов, чьи посты успешно включены в доставленную часть выпуска. Для упавших каналов (`channels_failed`) курсор пары **НЕ сдвигается**. | Атомарная транзакция с фиксацией `partial`. Посты упавшего канала остаются в БД и гарантированно войдут в следующий выпуск или ретрай. Потери контента нет. |
| `empty` | **НЕ двигаются**. Новых постов не обнаружено ни в одном канале папки. | Курсоры остаются прежними. Обновляется только `digest_schedules.next_send_at` на следующий будущий слот. |
| `failed` | **НЕ двигаются** ни для одного канала/подписки папки. | Курсоры остаются без изменений. Запись выпуска остаётся со статусом `failed` для ретрая или алерта. |
| `retrying` | **НЕ двигаются**. Идёт повторная попытка отправки. | Состав постов для отправки берётся СТРОГО из `digest_edition_items` (существующий снапшот), а НЕ пересобирается по курсорам заново. |
| `pending` / `building` / `ready` / `delivering` | **НЕ двигаются**. Промежуточные шаги конвейера. | Курсоры остаются в исходном состоянии до окончательного подтверждения доставки транспортом. |

---

## 3. Таблица соответствия «было → стало»

| Существующая сущность / колонка | Целевая сущность / колонка | Правило трансформации и назначение |
|---|---|---|
| `subscriptions.tag` (строка или `NULL`) | `folders.name` + `subscriptions.folder_id` | Если `tag` не пустой → создаётся `folders(user_id, name=tag)`. Если `tag IS NULL` или пуст → `folders(user_id, name='Основное', is_default=1)`. Подписка связывается через `folder_id`. |
| `subscriptions.period` (`'daily'`/`'weekly'`/`'monthly'`) | `digest_schedules.period` | Переносится в расписание папки. В `subscriptions` колонка остаётся как legacy; частота получения постов определяется расписаниями папки, в которой состоит канал. |
| `subscriptions.next_send_at` | `digest_schedules.next_send_at` | Планировщик опрашивает `digest_schedules`, а не отдельные подписки. В `subscriptions` поле сохраняется до полного отказа от старого планировщика. |
| `subscriptions.last_post_id` | `digest_schedule_cursors(schedule_id, subscription_id, last_post_id)` | **Курсор на пару (расписание, подписка)**. У каждого расписания независимый курсор. `subscriptions.last_post_id` сохраняется только для legacy-планировщика на период миграции. Исключены гонки и обнуления: daily 08:00 не опустошает weekly 09:00 той же папки, а одновременные расписания (Telegram и Email) не затирают посты друг друга. |
| `digest_settings.send_hour`, `send_minute`, `weekday`, `month_day`, `monthly_mode` | `digest_schedules.send_hour`, `send_minute`, `weekdays`, `month_day`, `monthly_mode` | Пользовательские настройки времени переносятся в расписания папки по умолчанию (и папок, каналы которых имели соответствующий период). |
| `user_emails.email_periods` (`'daily,weekly'`) | `digest_schedules.delivery = 'email'`, `digest_schedules.email_address` | **Условно, только при варианте А §8 п.8.** Если период был отмечен для отправки на почту И адрес `user_emails.email` подтверждён → соответствующее расписание получает `delivery = 'email'`, `email_address = user_emails.email`. При варианте Б email-периоды остаются на legacy-пути `subscriptions.period`/`user_emails.email_periods`, а в `digest_schedules` попадают только Telegram-расписания. |
| `user_emails.email` | `digest_schedules.email_address` | Денормализуется в расписание для изоляции от последующих изменений настроек почты во время доставки — только в варианте А §8 п.8. |
| `digest_executions` `(user_id, period, scheduled_at)` | `digest_editions` + `digest_executions(schedule_id, scheduled_at)` | Вся новая история выполнения и повторов привязана к `schedule_id`. Старые строки остаются legacy со старыми ключами. |
| `digest_alerts` `(user_id, period, scheduled_at, alert_type)` | `digest_alerts(schedule_id, scheduled_at, alert_type)` | Дедупликация технических алертов и кнопки повтора привязываются к конкретному расписанию. Старые алерты остаются legacy. |

---

## 4. Миграция данных

### 4.1. Порядок выполнения миграции
1. **Резервная копия (T19 — уже выполнено, механизм готов)**: Вызов штатного проверенного скрипта `python3 ops/aiqu/backup_db.py --tag pre-q4d` (создаёт снимок `bot_data-<stamp>-pre-q4d.db` с проверкой `PRAGMA quick_check`).
2. **Создание DDL**: Создание таблиц `folders`, `digest_schedules`, `digest_editions`, `digest_edition_items`, `digest_schedule_cursors`, индекса `idx_digest_schedules_due`. Добавление колонки `subscriptions.folder_id` (через безопасный `ALTER TABLE ADD COLUMN`).
3. **Формирование папок по умолчанию**:
   - Для каждого пользователя с активными подписками создаётся запись `folders(user_id, name='Основное', is_default=1)`.
   - Для каждого уникального непустого `tag` активных подписок создаётся `folders(user_id, name=tag, is_default=0)`.
4. **Разрешение дубликатов каналов и привязка к папкам**:
   - Если пользователь подписан на канал в нескольких периодах с одинаковым тегом → объединение в одну подписку с `last_post_id = MAX(last_post_id)`.
   - Если канал в нескольких разных тегах → применение детерминированного правила разрешения конфликтов (§6.1) и привязка к выбранной папке.
   - Проставление `subscriptions.folder_id = folders.id`.
   - Создание уникального индекса `uq_subscriptions_user_channel`.
5. **Генерация расписаний `digest_schedules`**:
   - Для каждой папки создаются расписания на основе существовавших периодов каналов, попавших в эту папку. Базовый план — только Telegram; email-расписания создаются лишь при выборе варианта А §8 п.8.
   - Параметры времени (час, минута, дни недели/месяца) берутся из `digest_settings` пользователя для этого периода; если настройки в базе отсутствовали — из системных дефолтов (`07:00` daily, `09:00` weekly Вс, `10:00` monthly 1-е число).
   - Если период входил в `user_emails.email_periods` и `user_emails.email` подтверждён → `delivery = 'email'`, иначе `'telegram'` — **только в варианте А §8 п.8**. В варианте Б email-периоды не мигрируют и продолжают обслуживаться legacy-планировщиком; в варианте В email-доставка уходит в ghsub по JSON-контракту.
   - Вычисление первого значения `next_send_at` в UTC.
6. **Backfill курсоров `digest_schedule_cursors`**:
   - Для каждого созданного расписания и каждой активной подписки его папки создаётся запись:
     ```sql
     INSERT INTO digest_schedule_cursors (schedule_id, subscription_id, last_post_id, updated_at)
     SELECT s.id, sub.id, sub.last_post_id, CURRENT_TIMESTAMP
     FROM digest_schedules s
     JOIN subscriptions sub ON sub.folder_id = s.folder_id
     WHERE sub.is_disabled = 0
     ON CONFLICT(schedule_id, subscription_id) DO NOTHING;
     ```
   - Если `sub.last_post_id` равен `NULL`, курсор пары инициализируется как `NULL` (первый запуск вычитает посты с момента подписки).
7. **Фиксация в метаданных**:
   - Запись в `app_meta` ключа `schema_queue4 = 'migrated_v1'`.

### 4.2. Идемпотентность и повторный запуск
- Миграционный скрипт оборачивается в транзакцию `BEGIN IMMEDIATE`.
- Перед запуском проверяется `_get_meta(cursor, 'schema_queue4')`. Если миграция уже выполнена, скрипт завершается без изменений.
- DDL-команды используют `CREATE TABLE IF NOT EXISTS`, `ALTER TABLE` защищён перехватом `sqlite3.OperationalError` (колонка уже существует).
- Вставки используют `INSERT INTO ... ON CONFLICT DO NOTHING`.
- Повторный запуск скрипта на уже мигрированной или частично мигрированной базе детерминирован, не создаёт дубликатов папок и расписаний.

### 4.3. Откат миграции (Rollback)
- В случае сбоя миграции или критических дефектов при выкате откат выполняется восстановлением из проверенного снимка T19:
  ```bash
  sqlite3 storage/bot_data.db < /dev/null # проверка блокировок
  cp storage/backups/bot_data-<stamp>-pre-q4d.db storage/bot_data.db
  ```
- Поскольку механизм T19 (уже выполнен) создаёт валидный SQLite-файл с правами 0600 и проверкой целостности, откат гарантирует мгновенный возврат к стабильному состоянию.

### 4.4. Совместимость старого и нового кода при поэтапном выкате (T20 → T23)
Выкат спроектирован так, чтобы происходить **без остановки сервиса, без big-bang релиза и строго без двойной отправки**:
- **Механизм изоляции планировщиков (Feature Flag переключения)**:
  - Вводится флаг версии планировщика на уровне пользователя: `user_prefs.digest_scheduler_version TEXT DEFAULT 'v1'` (или `user_features.digest_v2 = 0|1`).
  - Пока флаг `'v1'` (дефолт): старый планировщик (`main.py:check_digests`) обслуживает подписки пользователя по `subscriptions.next_send_at`. Новый планировщик игнорирует расписания таких пользователей.
  - При переключении пользователя на `'v2'`:
    - Старый планировщик атомарно исключает пользователя из выборки (`WHERE user_id NOT IN (SELECT user_id FROM user_prefs WHERE digest_scheduler_version = 'v2')` либо установкой `subscriptions.next_send_at = NULL`).
    - Новый планировщик начинает опрашивать `digest_schedules` для данного пользователя.
    - Окно параллельной работы двух планировщиков для одного пользователя отсутствует — двойная отправка исключена физически.
- **Порядок шагов выката**:
  - **Шаг T20 (Схема и миграция данных)**:
    - **Сначала ворота G1:** dry-run на копии прод-БД, решения владельца по §8 п.1, п.7 и п.8.
    - Создаются новые таблицы (`folders`, `digest_schedules`, `digest_editions`, `digest_edition_items`, `digest_schedule_cursors`).
    - Добавляется колонка `subscriptions.folder_id`.
    - Выполняется миграция данных: наполняются папки, расписания, выполняется backfill курсоров `digest_schedule_cursors`.
    - Все пользователи остаются на флаге `v1`. Работает только старый планировщик. Новые таблицы не опрашиваются в горячем цикле.
    - Доступен strictly read-only инструмент `ops/migrate_schedules_dryrun.py` для верификации консистентности.
  - **Шаг T21 (Новый планировщик, снапшоты, независимые курсоры)**:
    - Реализуется новый модуль `digest_scheduler.py` с выборкой due-расписаний по `digest_schedules.next_send_at <= now` для пользователей с флагом `v2`.
    - Сборка выпуска фиксирует снапшот в `digest_editions` и `digest_edition_items` до отправки.
    - Движение курсора: только после подтверждения доставки и только для вошедших каналов в `digest_schedule_cursors`.
    - Ретраи читают посты строго из снапшота `digest_edition_items`.
    - Канареечное переключение: флаг переключается сначала для тестовых пользователей/админов, затем для всей базы.
  - **Шаг T22 (Алерты и executions на schedule_id)**:
    - `digest_executions` и `digest_alerts` получают колонку `schedule_id REFERENCES digest_schedules(id)`.
    - Ключевание новых записей переводится на `(schedule_id, scheduled_at)`.
    - Исторические записи остаются как legacy (`schedule_id IS NULL`), не мешая дедупликации новых алертов и повторов.
    - Кнопка «Повторить» в алерте перезапускает конкретное расписание через его `schedule_id`.
  - **Шаг T23 (UI папок и расписаний)**:
    - **Перед UI — ворота G2:** обработчики настроек дайджеста выносятся из `main.py` в отдельный роутер `handlers/digest_settings.py` отдельным коммитом (проверка MS1: 150 хендлеров до/после, точные литералы `callback_data`).
    - Включается интерфейс управления папками и расписаниями (несколько расписаний на папку, выбор времени и транспорта; при варианте В §8 п.7 — список слотов одного расписания).
    - Полный вывод из эксплуатации старого планировщика `v1` и удаление legacy-колонок в плановой оптимизации — только после решения по email §8 п.8.

---

## 5. Планировщик T21 (Архитектура и граничные случаи)

### 5.1. Запросы и индексы
Вместо опроса сотен строк `subscriptions` каждые 30 минут, планировщик выбирает due-расписания:
```sql
SELECT s.id, s.folder_id, s.user_id, s.period, s.delivery, s.email_address, s.next_send_at
FROM digest_schedules s
JOIN user_prefs p ON p.user_id = s.user_id
WHERE s.is_enabled = 1 AND s.next_send_at <= ? AND p.digest_scheduler_version = 'v2'
ORDER BY s.next_send_at ASC
LIMIT ?;
```
- Запрос покрывается составным индексом: `idx_digest_schedules_due ON digest_schedules(is_enabled, next_send_at)`.
- Для каждого due-расписания подписки и их индивидуальные курсоры выбираются одним запросом по папке:
  ```sql
  SELECT sub.id AS subscription_id, sub.channel_username, sub.channel_title,
         c.last_post_id
  FROM subscriptions sub
  JOIN digest_schedule_cursors c
    ON c.subscription_id = sub.id AND c.schedule_id = ?
  WHERE sub.folder_id = ? AND sub.is_disabled = 0 AND sub.is_paused = 0;
  ```
  *(Если для новой подписки запись в `digest_schedule_cursors` ещё не создана, используется `LEFT JOIN` с `COALESCE(c.last_post_id, sub.last_post_id, 0)` и автоматическим backfill).*
- Выборка постов канала в выпуск выполняется строго от курсора пары `(schedule_id, subscription_id)`:
  ```sql
  SELECT p.post_id, p.post_text, p.post_link, p.post_time,
         p.views, p.reactions_total, p.forwards, p.replies
  FROM channel_posts p
  WHERE p.channel_username = ?
    AND p.post_id > COALESCE(?, 0)  -- подставляется c.last_post_id пары (schedule_id, subscription_id)
  ORDER BY p.post_id ASC
  LIMIT ?;
  ```
- После успешной доставки и фиксации снапшота планировщик вычисляет следующий `next_send_at` расписания и засыпает до ближайшего `MIN(next_send_at)` среди активных расписаний (с ограничением сверху, например 15–30 минут для подхвата ручных изменений).

**Вариант В (§8 п.7): набор слотов с одним курсором.** Расписание остаётся одной строкой `digest_schedules`; его слоты хранятся в `times`/`digest_schedule_slots`. Due-условие прежнее (`next_send_at <= now`), но `next_send_at` — это минимум по будущим слотам расписания. После доставки выпуска на текущем слоте `next_send_at` пересчитывается на ближайший следующий слот (а не на «следующий день»), поэтому 08:00 и 19:00 обслуживаются одним расписанием и одним курсором пары `(schedule_id, subscription_id)`. Выборка постов и транзакционное движение курсора не меняются: утренний выпуск забирает ночные посты, вечерний — дневные от сдвинутого курсора. Отдельные расписания папки (daily и weekly, Telegram и Email) при этом остаются независимыми.

### 5.2. Catch-up одним объединённым выпуском на расписание при простое
Если бот был остановлен (деплой, сбой сети, простой сервера) на длительное время (например, 36 часов):
- По расписанию наступило несколько плановых сроков (например, вчера в 09:00 и сегодня в 09:00).
- **Правило**: Формируется строго **ОДИН объединённый выпуск (catch-up) НА РАСПИСАНИЕ** (а не на папку).
- В выпуск входят все посты канала, вышедшие с момента `digest_schedule_cursors.last_post_id` для данного расписания.
- **Изоляция расписаний**: Благодаря раздельным курсорам в `digest_schedule_cursors` разные расписания одной папки не влияют друг на друга:
  - Утреннее расписание (daily 08:00) при catch-up собирает посты от своего курсора и сдвигает только свой курсор.
  - Еженедельное расписание (weekly Вс 09:00) или отдельное вечернее расписание (daily 20:00) той же папки при наступлении своего слота собирает посты от СВОЕГО курсора. Утренний выпуск не опустошает еженедельный!
  - Если 08:00 и 19:00 — это **слоты одного расписания** (вариант В §8 п.7), у них ОДИН курсор: утренний выпуск забирает ночные посты, вечерний — дневные от сдвинутого курсора. Это деление потока внутри расписания, а не независимость двух расписаний.
- Для варианта В catch-up остаётся один на расписание: он покрывает все пропущенные слоты и фиксируется на последнем наступившем слоте; отдельные выпуски на каждый пропущенный слот не создаются.
- Срок `scheduled_at` этого выпуска фиксируется как время последнего наступившего слота расписания.
- Поле `next_send_at` расписания не шагает по 1 дню, а сразу переносится на **следующий будущий слот** от текущего момента:
  $$\text{next\_send\_at} = \text{calculate\_next\_slot}(\text{now})$$
- Пользователь не получает лавину из пропущенных дайджестов — приходит одна актуальная сводка на каждое активное расписание за всё время простоя.

### 5.3. Граничные случаи
1. **Часовой пояс пользователя (`user_prefs.timezone`, T15)**:
   - Пользователь настраивает время расписания в своём локальном представлении (например, «08:30 утра»).
   - `digest_schedules.send_hour` и `send_minute` хранятся как локальное настенное время пользователя.
   - `digest_schedules.next_send_at` всегда хранится и рассчитывается строго в **UTC** с учётом `user_tz(user_id)`.
   - При смене часового пояса в `/settings` все `next_send_at` активных расписаний пользователя атомарно пересчитываются.
2. **Переход на летнее/зимнее время (DST)**:
   - Расчёт локального времени через `zoneinfo` корректно обрабатывает сдвиги на 1 час весной и осенью: локальный слот 08:30 остаётся 08:30 утра для пользователя, меняется только его UTC-проекция в `next_send_at`.
3. **Длительный простой (> суток / недели)**:
   - Чтобы не превысить лимиты Telegraph (64 КБ) и не собрать тысячи постов из сверхактивных каналов, в конвейер ранжирования включается стандартный предохранитель скоринга и потолок постов на канал (`MAX_POSTS_PER_CHANNEL`). Сверхлимит сворачивается в строчку «ещё N постов» со ссылкой на канал.
4. **Несколько расписаний на одно время**:
   - Сценарий А: У одного пользователя два расписания для разных папок (например, папка «Новости» и папка «Работа») назначены на 09:00.
     - *Поведение*: Создаются два независимых выпуска `digest_editions` (по одному на расписание/папку). Если оба идут в Telegram — отправляются последовательно двумя сообщениями (или двумя Telegraph-ссылками) с понятными заголовками папок.
     - Оптимизация сети: каналы обеих папок предзагружаются в память общим пулом через `prefetch_due_channels`, исключая повторные запросы по каналам.
   - Сценарий Б (если email-периоды мигрированы в `digest_schedules`, вариант А §8 п.8): Одно расписание на папку в 09:00 (Telegram) и другое в 09:00 (Email).
     - Благодаря раздельным строкам `digest_schedule_cursors` для каждого `schedule_id` курсоры изолированы:
     - Успешная отправка в Telegram сдвигает ТОЛЬКО курсоры Telegram-расписания.
     - Email-расписание гарантированно получает полный набор постов, даже если Telegram отправился раньше.
     - При одновременной сборке посты не дублируются и не теряются: каждый транспорт доставляет свой зафиксированный снапшот.

### 5.4. Ретрай, идемпотентность и восстановление при сбоях (Crash Recovery)
1. **Ретрай строго по снапшоту**:
   - Повторная попытка отправки выпуска (`status = 'retrying'`) читает состав постов **СТРОГО из `digest_edition_items`**, а **НЕ** запрашивает посты по курсорам заново.
   - Это гарантирует, что пользователь получит ровно тот выпуск, который был сформирован, даже если в каналы уже поступили новые посты.
2. **Идемпотентность слота**:
   - Повторное создание выпуска на один и тот же слот невозможно благодаря ограничению целостности:
     ```sql
     UNIQUE(schedule_id, scheduled_at)
     ```
   - Любая попытка параллельного планировщика создать дублирующий выпуск отсекается на уровне СУБД (`sqlite3.IntegrityError`).
3. **Поведение при сбое посреди доставки и рестарте бота**:
   - **Сбой во время отправки (`delivering`)**:
     - Обновление статуса на `delivered` и сдвиг курсоров в `digest_schedule_cursors` происходят в единой атомарной транзакции SQLite (`BEGIN IMMEDIATE ... COMMIT`).
     - Если бот упал (SIGKILL, падение хоста, OOM) до коммита транзакции, курсоры в базе данных **НЕ изменились**.
     - При перезапуске бота фоновый процесс инициализации находит выпуски в промежуточных статусах (`building`, `delivering`).
     - Для выпусков в `delivering`: статус переводится в `retrying`, и планировщик выполняет повторную отправку зафиксированного снапшота из `digest_edition_items`. Двойного сбора постов не происходит.
   - **Сбой во время сборки снапшота (`building`)**:
     - Если бот упал до завершения фиксации снапшота (статус `building`), незавершённый выпуск удаляется или его items очищаются, после чего сборка выполняется заново от актуальных курсоров `digest_schedule_cursors`.

---

## 6. Конфликты данных и стратегия их разрешения

В ходе миграции скрипт `ops/migrate_schedules_dryrun.py` инспектирует базу на предмет следующих конфликтов:

### 6.1. Канал в нескольких тегах у одного пользователя
- **Суть проблемы**: В текущей схеме пользователь мог подписаться на `@durov` с тегом `#tech` в daily и с тегом `#news` в weekly. В целевой схеме действует ограничение: один канал — строго одна папка (`UNIQUE(user_id, channel_username)`).
- **Обнаружение**:
  ```sql
  SELECT user_id, channel_username, COUNT(DISTINCT COALESCE(tag, '')) as tag_cnt,
         GROUP_CONCAT(DISTINCT COALESCE(tag, '<без тега>')) as tags
  FROM subscriptions
  WHERE is_disabled = 0
  GROUP BY user_id, channel_username
  HAVING tag_cnt > 1;
  ```
- **Стратегия разрешения**:
  1. Выбирается папка подписки со **старшим периодом** (`monthly` > `weekly` > `daily`).
  2. При равных периодах побеждает непустой тег перед пустым.
  3. При разных непустых тегах побеждает подписка с наибольшим `last_post_id` (наиболее свежая активность).
  4. Подписки схлопываются в одну запись: `last_post_id = MAX(last_post_id)` (чтобы исключить дублирование старых постов). Вторая запись помечается `is_disabled = 1`.
  5. В сухом отчёте (dry-run) пишется явное предупреждение с указанием каналов и выбранной папки.
- **Оговорка Q4D-fix2 (до фиксации §8 п.1)**:
  - Рассматривавшийся вариант «автоматически создать на папку оба расписания (daily и weekly)» **не сохраняет прежнее поведение полностью**: обе периодичности получат все каналы папки, а не только тот канал, который действительно был в двух тегах/периодах. Поэтому он не является безопасной заменой автоматическому выбору папки по старшему периоду и не должен включаться без отдельного решения владельца.
  - До решения нужен прогон `ops/migrate_schedules_dryrun.py` на копии прод-БД; прод-цифры конфликтов даст OPS1 через `prod_check` (без записи в прод). Скрипт уже есть, запуск строго read-only.
  - **Порог**: если конфликтов «канал в нескольких тегах» 0–3, автоматическое разрешение не требуется — достаточно ручного разбора по списку dry-run. Автоматическое правило (пп. 1–4) применяется только при большем числе конфликтов и после подтверждения владельцем.

### 6.2. Пустые папки
- **Суть проблемы**: Наличие тегов, у которых нет ни одной активной подписки (например, все каналы тега были отключены, либо тег остался только в архиве `saved_messages`).
- **Обнаружение**: Поиск тегов в `subscriptions WHERE is_disabled = 1` или `saved_messages`, для которых нет активных записей в `subscriptions WHERE is_disabled = 0`.
- **Стратегия разрешения**:
  - Для неактивных тегов папки не создаются автоматически при миграции. Создаются только папки, имеющие хотя бы одну активную подписку, плюс папка «Основное» (`is_default = 1`).
  - Если пользователь в будущем создаст пустую папку в UI — она сохраняется, но расписания этой папки не генерируют пустых отправок (состояние `empty`).

### 6.3. Расписание без времени (отсутствие настроек)
- **Суть проблемы**: Пользователь подписан на каналы периода (например, `weekly`), но строки для `(user_id, 'weekly')` в таблице `digest_settings` нет, либо поля `send_hour`/`send_minute` равны `NULL`.
- **Обнаружение**:
  ```sql
  SELECT DISTINCT s.user_id, s.period
  FROM subscriptions s
  LEFT JOIN digest_settings ds ON ds.user_id = s.user_id AND ds.period = s.period
  WHERE s.is_disabled = 0 AND (ds.send_hour IS NULL OR ds.send_minute IS NULL);
  ```
- **Стратегия разрешения**:
  - Применяются детерминированные значения по умолчанию в соответствии с контрактом `get_default_digest_settings`:
    - `daily`: `07:00`
    - `weekly`: `09:00`, воскресенье (`weekday = 6`)
    - `monthly`: `10:00`, 1-е число месяца (`month_day = 1`)
  - В отчёте миграции фиксируется применение дефолтного времени.

### 6.4. Почта без подтверждения
- **Суть проблемы**: В `user_emails.email_periods` прописаны периоды (например, `'daily'`), но колонка `email` пуста или `NULL` (например, пользователь начал ввод через `/email`, заполнил `pending_email`, но не ввёл код подтверждения `verify_code`).
- **Обнаружение**:
  ```sql
  SELECT user_id, pending_email, email_periods
  FROM user_emails
  WHERE (email IS NULL OR email = '') AND (email_periods IS NOT NULL AND email_periods != '');
  ```
- **Стратегия разрешения**:
  - Расписание НЕ переводится на `delivery = 'email'`.
  - Устанавливается безопасный дефолт `delivery = 'telegram'`.
  - Запись об отклонённой неподтверждённой почте выводится в отчёт миграции со статусом предупреждения.

---

## 7. Нарезка на задачи и коммиты (T19 — выполнено; T20…T23 — план)

T19 уже выполнен и в план не входит. Каждая из задач T20–T23 представляет собой логически изолированный, независимо тестируемый шаг.

```mermaid
flowchart TD
    T19["T19: Резервное копирование SQLite (ВЫПОЛНЕНО)"] --> G1
    G1["Ворота G1: dry-run на копии прод-БД + решения §8"] --> T20
    T20["T20: Схема folders/schedules/editions + dry-run миграция"] --> T21
    T21["T21: Планировщик due-расписаний, snapshot и catch-up"] --> T22
    T22["T22: digest_executions/alerts на schedule_id"] --> G2
    G2["Ворота G2: вынос дайджест-хэндлеров в Router (MS1)"] --> T23
    T23["T23: UI папок и расписаний (после G2)"]
```

**T19 — бэкап SQLite перед деплоем: ВЫПОЛНЕНО, не задача.** `ops/aiqu/backup_db.py` делает консистентный снимок через SQLite backup API, проверяет `PRAGMA quick_check`, пишет атомарно с правами 0600 и ротирует 14 последних снимков; `.github/workflows/deploy.yml` вызывает его до `docker compose up -d --build` и останавливает деплой при коде 1. Покрытие — `tests/test_backup_db.py`. Повторно в T20–T23 не планируется.

### Ворота G1 (перед T20)
- Прогнать `ops/migrate_schedules_dryrun.py` на **копии прод-БД**, полученной из свежего снимка T19; зафиксировать число конфликтов и статистику миграции в отчёте.
- Владелец принимает решения по §8 п.1 (конфликт тегов), п.7 (слоты/деление потока) и п.8 (email-периоды). До этих решений T20 не начинается.
- При 0–3 конфликтах §6.1 автоматическое разрешение не включается — достаточно ручного разбора по dry-run.

### T20. Схема данных folders / digest_schedules / digest_editions / digest_schedule_cursors и миграция с dry-run
- **Объём**:
  - DDL в `data/database.py:init_db` (создание таблиц `folders`, `digest_schedules`, `digest_editions`, `digest_edition_items`, `digest_schedule_cursors`, внешние ключи, индексы).
  - Поле флага миграции `user_prefs.digest_scheduler_version TEXT DEFAULT 'v1'`.
  - Скрипт миграции базы данных `ops/migrate_schedules.py` с флагом транзакции, backfill курсоров `digest_schedule_cursors = subscriptions.last_post_id` и проверкой контрольных сумм.
  - Read-only диагностический скрипт `ops/migrate_schedules_dryrun.py` (подключение через `mode=ro`, расчёт числа курсоров, предупреждения о NULL-курсорах).
- **Критерий готовности**:
  - `init_db()` создаёт новые таблицы без регрессий существующих тестов.
  - Dry-run скрипт на чистой базе возвращает код 0, на конфликтной базе — код 3 с детальным списком конфликтов.
  - Повторный запуск миграции идемпотентен.
  - Старый планировщик продолжает работать штатно для всех пользователей (`v1`).
- **Тесты**: `tests/test_migrate_schedules_dryrun.py`, `tests/test_database_migration_q4.py`.

### T21. Планировщик по due-расписаниям, snapshot выпуска, повтор, catch-up и безопасное переключение
- **Объём**:
  - Модуль `digest_scheduler.py`: выборка due-расписаний по `digest_schedules.next_send_at <= now` для пользователей с флагом `v2`.
  - Изоляция планировщиков: пользователи с `v1` обслуживаются старым планировщиком, пользователи с `v2` — исключительно новым, исключая двойную отправку.
  - Сборка выпуска с фиксацией снапшота в `digest_editions` и `digest_edition_items` до вызова Telegram API (а при варианте А §8 п.8 — SMTP API).
  - Идемпотентный повтор: при ошибке отправки повторный запуск отправляет посты строго из существующего снапшота `digest_edition_items` без повторного сбора по курсору.
  - Механизм catch-up: строго один выпуск на расписание; расписания одной папки не влияют друг на друга благодаря раздельным курсорам.
  - Продвижение курсоров `digest_schedule_cursors` строго в одной транзакции с отметкой `delivered`/`partial` и только для каналов, чьи посты реально вошли в доставленный снапшот.
- **Критерий готовности**:
  - Имитация сбоя при отправке не приводит к повторному сбору постов и дублированию.
  - Отсутствие двойной отправки при переключении пользователя с `v1` на `v2`.
- **Тесты**: `tests/test_digest_schedules_scheduler.py`, `tests/test_digest_edition_snapshot.py`, `tests/test_digest_catchup.py`.

### T22. Перевод digest_executions и digest_alerts на schedule_id
- **Объём**:
  - Привязка новых записей `digest_executions` и `digest_alerts` к `schedule_id + scheduled_at`.
  - Маркировка исторической информации как legacy (`schedule_id IS NULL`).
  - Адаптация логики технических алертов (`digest_alerts.py`): кулдауны, антиспам, кнопки «Повторить сейчас» и «Пауза 24ч» работают через `schedule_id`.
- **Критерий готовности**:
  - Кнопка «Повторить» в техническом алерте перезапускает конкретное расписание через его `schedule_id`.
  - Корректная дедупликация алертов per-schedule.
- **Тесты**: `tests/test_digest_alerts_schedules.py`, `tests/test_digest_executions_schedules.py`.

### Ворота G2 (перед T23)
- Отдельным коммитом вынести дайджест-хэндлеры, FSM-состояния и inline-клавиатуры из `main.py` в Router `handlers/digest_settings.py` (или `digest_ux.py`).
- Метод проверен в MS1: до и после выноса — **150 хендлеров**; обязательна сверка точных литералов `callback_data` (без изменения существующих callback-ов), иначе старые кнопки перестанут находить обработчики.
- Только после зелёного прогона тестов и этой сверки начинается UI-часть T23.

### T23. UI «папка → расписания» (после G2)
- **Объём**:
  - Вынос дайджест-хэндлеров выполнен на воротах G2; UI-коммит T23 его не повторяет.
  - Реализация интерфейса папок: «📁 Мои папки», создание папки, перемещение канала в другую папку.
  - Реализация интерфейса расписаний: добавление расписания к папке (выбор времени, дней недели, канала доставки Telegram/Email). Если владелец выберет вариант В §8 п.7 — редактирование списка времён одного расписания (добавить/удалить слот) вместо создания отдельного расписания на каждое время.
  - Перевод всех пользователей на `v2` и отключение старого планировщика `v1` — только после решения по email §8 п.8; пользователи с непрекращёнными email-периодами не должны потерять почту.
- **Критерий готовности**:
  - `main.py` сокращён минимум на 800+ строк на воротах G2; UI-часть T23 не увеличивает монолит.
  - Пользователь может в чате добавить второе расписание на папку и получать дайджест дважды в день; при варианте В — добавить второй слот в одно расписание.
- **Тесты**: `tests/test_digest_settings_router.py`, `tests/test_folders_ux.py`.

---

## 8. Риски и открытые вопросы к владельцу

Ниже представлены ключевые развилки проекта (не более 8), сформулированные как выбор из конкретных вариантов:

1. **Конфликт папок при наличии канала в нескольких подписках с разными тегами**:
   - **Вариант А (предварительно рекомендуется)**: Автоматически привязать канал к папке подписки с более старшим периодом (`monthly` > `weekly` > `daily`), при равенстве — по максимальному `last_post_id`. Курсор установить в `MAX(last_post_id)`.
   - **Вариант Б**: Создавать специальную системную папку «Разобрать» («Конфликты») и складывать спорные каналы туда.
   - **Вариант В (не рекомендуется как замена А)**: Автоматически создавать на папку оба расписания (daily и weekly). Это **не сохраняет прежнее поведение полностью**: обе периодичности получат все каналы папки, а не только тот канал, который был в двух тегах/периодах; возможны лишние выпуски и дубли постов между daily и weekly.
   - *Статус*: Окончательный выбор — за владельцем после dry-run на копии прод-БД (§6.1). Если конфликтов 0–3, достаточно ручного разбора, автоматическое правило не включается. Прод-цифры даёт OPS1 через `prod_check`.

2. **Привязка времени расписания к часовому поясу пользователя**:
   - **Вариант А (Рекомендуется)**: Время расписания задаётся и отображается в персональном часовом поясе пользователя (`user_prefs.timezone`, T15), если он задан, либо в системном `APP_TIMEZONE`.
   - **Вариант Б**: Расписания настраиваются строго в едином `APP_TIMEZONE` (как работало до T15).
   - *Решение для проекта*: Выбран **Вариант А**, так как персональные часовые пояса уже поддержаны в T15 для напоминаний.

3. **Имя папки по умолчанию для каналов без тега**:
   - **Вариант А (Рекомендуется)**: «Основное» (`is_default = 1`).
   - **Вариант Б**: «Без папки» (`is_default = 1`).
   - *Решение для проекта*: Выбран **Вариант А** («Основное»), так как это соответствует общепринятому UX почтовых и RSS-клиентов.

4. **Действие при удалении папки, в которой остались каналы (T23 UI)**:
   - **Вариант А (Рекомендуется)**: Автоматически перемещать оставшиеся каналы в папку по умолчанию («Основное») с сохранением их курсоров доставки.
   - **Вариант Б**: Запрещать удаление папки, пока пользователь вручную не перенесёт каналы или не отпишется от них.
   - **Вариант В**: Удалять папку вместе с подписками (с обязательным подтверждением «Удалить папку и N каналов»).
   - *Решение для проекта*: Выбран **Вариант А** как наиболее безопасный против случайной потери подписок.

5. **Ограничение объёма catch-up выпуска при простое более 7 дней**:
   - **Вариант А (Рекомендуется)**: Ограничивать окно выборки постов для catch-up дайджеста разумным потолком (не более 7 дней назад от момента старта), даже если курсор отстал сильнее.
   - **Вариант Б**: Собирать все посты строго с `last_post_id`, полагаясь на усечение Telegraph (64 КБ) и скоринг постов.
   - *Решение для проекта*: Выбран **Вариант А**, чтобы исключить выедание памяти и перегрузку при многомесячных простоях.

6. **Обработка неподтверждённого email при миграции**:
   - **Вариант А (Рекомендуется)**: Если адрес не подтверждён, создавать расписание с `delivery = 'telegram'` и фиксировать предупреждение в отчёте миграции.
   - **Вариант Б**: Создавать расписание с `delivery = 'email'`, но ставить `is_enabled = 0` (отключено до подтверждения).
   - *Решение для проекта*: Выбран **Вариант А**, так как пользователь не должен терять доставку дайджеста в Telegram из-за незавершённого подтверждения почты.

7. **Распределение потока постов между расписаниями одной папки (ОТКРЫТЫЙ ВОПРОС К ВЛАДЕЛЬЦУ)**:
   - **Вопрос**: «Расписания одной папки делят поток постов (каждый пост попадает ровно в один выпуск: утренний — ночные, вечерний — дневные) или независимы (каждое расписание получает все посты с момента СВОЕЙ прошлой доставки; при утро+вечер часть постов повторится)?»
   - **Вариант А (Независимые курсоры — базовая модель/fallback)**:
     - Каждое расписание имеет собственный курсор `digest_schedule_cursors(schedule_id, subscription_id, last_post_id)`.
     - Выпуск собирает все посты с момента СВОЕЙ прошлой доставки.
     - *Что в схеме*: Таблица `digest_schedule_cursors(schedule_id, subscription_id, last_post_id, updated_at, PRIMARY KEY(schedule_id, subscription_id))`.
     - *Что в планировщике*: Выборка постов `WHERE post_id > c.last_post_id` для текущего `schedule_id`. При успешной доставке сдвигается только курсор этого расписания.
     - *Плюсы*:
       - Не ломает комбинацию `daily` + `weekly`: еженедельный воскресный выпуск папки получит ВСЕ посты за неделю, а не окажется пустым из-за утреннего ежедневного выпуска.
       - Параллельные расписания разных транспортов (Telegram в 09:00 и Email в 09:00) работают без гонок и взаимного обнуления.
       - Независимый catch-up и прозрачные ретраи per-schedule.
     - *Минусы*: При наличии утреннего (08:00) и вечернего (20:00) расписаний посты могут пересекаться, если окно выборки шире межвыпускного интервала.
   - **Вариант Б (Деление единого потока постов — режим «потокового пайплайна»)**:
     - Курсор фиксируется на уровне папки: один курсор на пару `(folder_id, subscription_id)`.
     - Каждый пост попадает ровно в один выпуск: утренний выпуск забирает ночные посты и двигает курсор папки, вечерний — дневные посты от сдвинутого курсора.
     - *Что в схеме*: Таблица курсоров `digest_folder_cursors(folder_id, subscription_id, last_post_id)` либо поле на связи папки с подпиской.
     - *Что в планировщике*: Все расписания папки читают и двигают единый курсор папки.
     - *Плюсы*: Гарантированное отсутствие дублирования постов между утренним и вечерним дайджестами одной папки.
     - *Минусы*: Несовместимо с еженедельными/ежемесячными сводками в той же папке (`weekly` соберет 0 постов). Несовместимо с одновременной доставкой в Telegram и Email (первый отправившийся опустошит второй).
   - **Вариант В (Расписание = набор слотов с ОДНИМ курсором — РЕКОМЕНДАЦИЯ ПРОЕКТА, уверенность средняя)**:
     - Расписание содержит список времён суток, например `08:00` и `19:00`, с общими для них папкой, периодом, днями недели и транспортом. У расписания **один курсор** на подписку `(schedule_id, subscription_id)`.
     - Утро забирает ночные посты и двигает курсор; вечер забирает дневные посты уже от сдвинутого курсора. Поток делится **внутри расписания**. Разные расписания папки (`daily` и `weekly`, Telegram и Email) остаются **независимыми**: у них раздельные курсоры.
     - *Что в схеме*: либо колонка `digest_schedules.times TEXT` с JSON-массивом локальных времён (`["08:00","19:00"]`), либо дочерняя таблица `digest_schedule_slots(id, schedule_id, send_hour, send_minute, ...)` с `UNIQUE(schedule_id, send_hour, send_minute)`. Базовая DDL §2.1 в этом случае дополняется, а не заменяется.
     - *Что в планировщике §5*: due-условие прежнее, но `next_send_at` — ближайший будущий слот среди всех слотов расписания; после доставки он пересчитывается на следующий слот. Catch-up — один на расписание по последнему пропущенному слоту, без лавины выпусков на каждый слот.
     - *Идентификатор выпуска*: `UNIQUE(schedule_id, scheduled_at)` остаётся уникальным; `scheduled_at` — время конкретного слота. Слоты одного расписания различаются временем, а совпадающие времена отсекаются валидацией/уникальным индексом.
     - *UI T23*: пользователь редактирует одно расписание со списком времён (добавить/удалить слот, видеть ближайший слот), а не создаёт отдельное расписание на каждое время. Для независимых daily/weekly по-прежнему создаются отдельные расписания.
     - *Плюсы*: убирает дубли постов между утренним и вечерним выпусками; сохраняет независимость daily/weekly и Telegram/Email; меньше сущностей в UI.
     - *Минусы/что подтвердить*: меняется привычная семантика «одно расписание — одно время»; нужно подтвердить, что пользователь ожидает именно общий курсор на набор слотов, а не независимые выпуски; нужен прогон на реальных сценариях (утро/вечер, простой, catch-up) и решение по миграции старых одиночных расписаний (они остаются one-time, объединение в слоты — только явное действие в UI).
   - **Рекомендация проекта**: принять **Вариант В** как целевой продуктовый сценарий с уверенностью **средней** (логика курсора ясна, но UX и миграцию надо подтвердить). **Вариант А** остаётся безопасной базовой моделью и fallback, если владелец предпочтёт независимые выпуски. **Вариант Б** не рекомендуется из-за несовместимости с weekly/monthly и раздельными транспортами.
   - *Статус решения*: **ОТКРЫТ**; выбор остаётся за владельцем продукта (вариант НЕ зафиксирован директивно). До T20 нужно подтвердить: (1) согласен ли владелец на один курсор на набор слотов; (2) допустимы ли пересечения между разными расписаниями папки; (3) как показывать слоты в UI T23.

8. **Куда деваются существующие email-периоды (`user_emails.email_periods`) (ОТКРЫТЫЙ ВОПРОС К ВЛАДЕЛЬЦУ)**:
   - **Контекст**: `docs/roadmap.md` §0 говорит, что текущая email-доставка unagi «сохраняется как есть до переезда» в ghsub, а §2 дорожной карты предполагает миграцию email-периодов в `digest_schedules.delivery='email'`. Это расхождение надо снять до T20.
   - **Вариант А (текущее решение §3/§4): мигрировать email-периоды в `digest_schedules` с `delivery='email'`.**
     - T20: создаёт email-расписания и `email_address`; T21: новый планировщик отправляет email напрямую (переиспользует существующий SMTP-путь); T22: алерты/executions получают `schedule_id` и для email.
     - Риск потери почты: низкий при корректной миграции, но появляется **двойная миграция** (сначала в новую схему, затем в ghsub); ошибка разбора `email_periods`/неподтверждённый адрес может молча перевести период на Telegram.
     - Зависимость от ghsub: не блокирует работу, но создаёт код, который ghsub затем заменит.
   - **Вариант Б (рекомендация проекта, уверенность средняя): оставить email-периоды на legacy-пути `period`; в `digest_schedules` мигрировать только Telegram.**
     - T20: создаёт папки и только Telegram-расписания, `user_emails.email_periods` не трогает; T21: новый планировщик обслуживает только Telegram, старый email-путь продолжает работать; T22: `schedule_id` нужен только новым Telegram-записям, legacy email-алерты остаются как есть.
     - Риск потери почты: минимальный, если старый email-путь не отключать. Главный риск — смешанные пользователи (есть и email-, и Telegram-периоды): при per-user флаге `v1/v2` нельзя одновременно отдать email старому планировщику, а Telegram новому. До ghsub таких пользователей нужно либо целиком держать на `v1`, либо явно выделить email в отдельный legacy-контур с исключением из `v2`; это надо подтвердить владельцу.
     - Зависимость от ghsub: только отложенная — перенос email в ghsub ждёт JSON-контракта, сейчас ничего не блокирует.
   - **Вариант В (целевой после ghsub): выдавать выпуск JSON v1 в файловый спул для ghsub, email-доставку из unagi не развивать.**
     - T20–T22: формируют и хранят выпуск/снапшот, но не создают email-расписания; дополнительно нужен экспортёр JSON v1 по контракту `ghsub-candidates.md` §2.2 и приёмник в ghsub. Текущая почта unagi до готовности контракта остаётся как есть.
     - Риск потери почты: высокий, если отключить текущую email-доставку раньше, чем ghsub начнёт принимать и отправлять выпуски; при поэтапном включении риск управляемый.
     - Зависимость от ghsub: **высокая** — без готового контракта и приёмника вариант не даёт доставки.
   - **Рекомендация проекта**: на этот спринт выбрать **Вариант Б** (уверенность средняя): он соответствует §0 дорожной карты, сокращает T20 и убирает двойную миграцию, а риск потери почты минимален при сохранении legacy-пути. **Вариант В** — следующий шаг после готовности ghsub; **Вариант А** — fallback, если владелец хочет сохранить email внутри новой схемы до переезда. Окончательное решение — за владельцем; до него T20 не начинается (ворота G1).
