# GH2: Почтовые выпуски unagi через ghsub (сторона unagi, за флагом)

## Статус
Завершено (18.10.2026)

## Описание
В рамках архитектурного решения 18.10 (unagi = Telegram, ghsub = почта) реализован механизм передачи сформированных email-выпусков в почтовый сервис `ghsub` в соответствии с согласованным контрактом JSON v1 (`docs/sprint/ghsub-contract-json-v1.md`).

Доставка через `ghsub` скрыта за конфигурационным флагом `EMAIL_DELIVERY` (по умолчанию `"unagi"` — сохраняется прежняя отправка через `email_digest.py` без изменений).

## Реализация

1. **Конфигурация (`config.py`):**
   - Добавлены поля `delivery: str = "unagi"` и `outbox_dir: str = "storage/ghsub-outbox"` в `EmailConfig`.
   - Добавлен разбор переменных окружения `EMAIL_DELIVERY` (по умолчанию `"unagi"`) и `EMAIL_OUTBOX_DIR` (по умолчанию `"storage/ghsub-outbox"`).
   - Поля добавлены строго в конец классов для бесконфликтной интеграции с параллельными ветками CFG2/RS1.

2. **Модуль адаптера спула (`ghsub_outbox.py`):**
   - Реализована сборка выпуска в формате JSON v1 (`build_ghsub_edition`).
   - Детерминированный `edition_id`: `unagi:digest:<recipient>:<period>:<scheduled_at_iso>` (обеспечивает идемпотентность на стороне `ghsub`).
   - Символический ключ адресата: `recipient = f"unagi-{user_id}"` (адрес почты в файл не пишется — маппинг хранится в конфигурации ghsub).
   - `manage_url`: `https://t.me/<BOT_USERNAME>?start=email` (если имя бота неизвестно, поле не включается).
   - `window`: границы выпуска (`from`, `to` в ISO 8601 UTC с суффиксом `Z`, `timezone` из `APP_TIMEZONE`).
   - `items`: формирование карточек и блока «Коротко» строго по правилам формата TG1:
     - Детерминированное разделение на уровне канала: до 3 карточек (`tier: "card"`), остальные посты — в `tier: "brief"`.
     - Очистка текстов через TX1 (`post_text.py`) с обязательным удалением всех HTML-тегов (`strip_html`).
     - Строгая валидация ссылок: все URL проверяются на протокол `http://` или `https://` с безопасным fallback-построением ссылок на пост в канале при наличии некорректных или псевдо-схем (например, `javascript:`).
   - Атомарная запись (`write_ghsub_outbox`): запись во временный файл `.tmp_edition_<edition_id>.json` в том же каталоге с последующим `os.replace` в `<edition_id>.json`. Временные файлы после успешной записи отсутствуют; при сбое временный файл удаляется.

3. **Интеграция в доставку (`main.py`, `email_digest.py`):**
   - `email_digest.is_enabled()`: возвращает `True`, если `EMAIL_DELIVERY=ghsub`, позволяя пользователям настраивать email-периоды даже при отсутствии прямых SMTP-настроек в unagi.
   - `send_digest_email`: при `settings().email.delivery == "ghsub"` асинхронно вызывает `write_ghsub_outbox` вместо `email_digest.send_email`.
   - `deliver_digest`: передаёт `user_id` и `scheduled_at` в `send_digest_email`. При успешной записи в outbox возвращает `True`, обеспечивая сдвиг курсоров и отметок отправки в `_finish_period_execution` ровно так же, как и при успешной отправке по SMTP.
   - Не затронуты: Telegram-доставка, планировщик дайджестов, Очередь 4, экраны `render_screen`.

4. **Тестирование (`tests/test_gh2_outbox.py`):**
   - Проверка поведения по умолчанию: письмо уходит через `email_digest.send_email`, outbox-файлы не создаются.
   - Проверка режима `ghsub`: письмо не отправляется, в `EMAIL_OUTBOX_DIR` создаётся валидный JSON v1.
   - Проверка обязательных полей, типов и контракта JSON v1.
   - Проверка TG1 tier partitioning (до 3 карточек, остальное brief).
   - Проверка повторной отправки (идемпотентный `edition_id`).
   - Проверка атомарности записи (.tmp файл удаляется, на диске остаётся только готовый `.json`).
   - Проверка очистки HTML и фильтрации не-http(s) ссылок.
   - Проверка отсутствия `manage_url` при неизвестном `BOT_USERNAME`.
   - Проверка интеграции с `deliver_digest` (сдвиг курсоров без fallback на Telegram).

5. **Чек-лист для владельца (`docs/sprint/ghsub-switch-checklist.md`):**
   - Шаг 1: /etc/tldw/ghsub.yml, безопасная команда sqlite3 на чтение для получения user_id, inbox_dir.
   - Шаг 2: cron приёмника ghsub (*/5).
   - Шаг 3: EMAIL_DELIVERY=ghsub в .env + пересоздание контейнера с GIT_SHA.
   - Шаг 4: проверка первого выпуска.
   - Шаг 5: план быстрого отката на EMAIL_DELIVERY=unagi.
