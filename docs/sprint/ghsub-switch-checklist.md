# Чек-лист переключения почтовых выпусков Unagi на ghsub (GH2)

**Дата составления:** 18.10.2026  
**Цель:** Перенос почтовой доставки дайджестов из монолитного `email_digest.py` (SMTP) в надёжный внешний сервис `ghsub` через локальный файловый спул JSON v1.  
**Связанные документы:**
- Контракт JSON v1: [docs/sprint/ghsub-contract-json-v1.md](ghsub-contract-json-v1.md)
- Отчёт о реализации GH2: [docs/sprint/progress/GH2.md](progress/GH2.md)

---

## Шаг 1. Настройка `/etc/tldw/ghsub.yml` на сервере aiqu

1. **Получение списка активных адресатов из БД Unagi (безопасно, без вывода адресов в общий лог):**
   Выполнить команду в режиме `read-only` для получения только идентификаторов пользователей (`user_id`), у которых подтверждена почта и включена доставка хотя бы одного периода:
   ```bash
   sqlite3 -readonly /root/projects/unagi-reminder-bot/data/unagi.db \
     "SELECT user_id FROM user_emails WHERE email IS NOT NULL AND email != '' AND email_periods != '';"
   ```
   *Примечание:* Для сопоставления конкретного `user_id` с email-адресом выполнить точечный запрос локально оператором (не перенаправляя вывод в общие CI/CD логи):
   ```bash
   sqlite3 -readonly /root/projects/unagi-reminder-bot/data/unagi.db \
     "SELECT user_id, email FROM user_emails WHERE user_id = <USER_ID>;"
   ```

2. **Конфигурация адресатов и входящей директории в `/etc/tldw/ghsub.yml`:**
   В секцию `recipients` добавить маппинг символических ключей `unagi-<user_id>` на реальные email-адреса:
   ```yaml
   recipients:
     unagi-42: "user1@example.com"
     unagi-108: "user2@example.com"

   unagi_intake:
     inbox_dir: /root/projects/unagi-reminder-bot/storage/ghsub-outbox
     done_dir: /root/projects/unagi-reminder-bot/storage/ghsub-outbox/done
     rejected_dir: /root/projects/unagi-reminder-bot/storage/ghsub-outbox/rejected
   ```

3. **Проверка прав доступа к спул-директории:**
   Убедиться, что каталог существует и доступен на чтение/запись для пользователя процесса `ghsub` и контейнера Unagi:
   ```bash
   mkdir -p /root/projects/unagi-reminder-bot/storage/ghsub-outbox
   chmod 770 /root/projects/unagi-reminder-bot/storage/ghsub-outbox
   ```

---

## Шаг 2. Настройка cron приёмника ghsub

Приёмник `ghsub` запускается каждые 5 минут для опроса спула, валидации выпусков и отправки писем через локальный почтовый транспорт:

1. Добавить запись в crontab (`crontab -e` под системным пользователем ghsub или root):
   ```cron
   */5 * * * * /usr/local/bin/ghsub-intake /etc/tldw/ghsub.yml >> /var/log/tldw/ghsub-intake.log 2>&1
   ```

2. Проверить ротацию лога `/var/log/tldw/ghsub-intake.log` в `/etc/logrotate.d/ghsub`.

---

## Шаг 3. Включение флага в Unagi и пересоздание контейнера

1. **Обновление `.env` Unagi:**
   В файле `/root/projects/unagi-reminder-bot/.env` установить:
   ```ini
   EMAIL_DELIVERY=ghsub
   EMAIL_OUTBOX_DIR=storage/ghsub-outbox
   ```

2. **Фиксация GIT_SHA и пересоздание контейнера:**
   ```bash
   cd /root/projects/unagi-reminder-bot
   export GIT_SHA=$(git rev-parse --short HEAD)
   docker compose up -d --build --force-recreate
   ```

3. **Проверка готовности бота:**
   ```bash
   docker compose logs --tail=50 unagi-bot
   ```
   Убедиться в отсутствии ошибок запуска и инициализации.

---

## Шаг 4. Проверка первого выпуска

1. **Контроль генерации файла в outbox:**
   После наступления времени регулярного дайджеста (или ручной проверки через тестовую рассылку) проверить появление файла выпуска:
   ```bash
   ls -la /root/projects/unagi-reminder-bot/storage/ghsub-outbox/
   ```
   Имя файла должно иметь вид:
   `unagi:digest:unagi-<user_id>:<period>:<scheduled_at_iso>.json`
   Временных файлов `.tmp*` оставаться не должно.

2. **Контроль обработки в ghsub:**
   ```bash
   tail -n 50 /var/log/tldw/ghsub-intake.log
   ```
   Убедиться, что:
   - Файл валидирован по контракту `1.0`.
   - Адресат `unagi-<id>` найден в конфиге.
   - Письмо отправлено через Brevo/msmtp.
   - Файл перенесён в `done/`.

3. **Контроль идемпотентности:**
   При повторной попытке отправки того же выпуска `ghsub` должен распознать дубль по `edition_id` и перенести файл в `done/` без повторной отправки письма.

---

## Шаг 5. План быстрого отката (Rollback)

В случае любых сбоев доставки через `ghsub` откат на прямую SMTP-доставку Unagi выполняется мгновенно без потери данных:

1. **Возврат флага доставки:**
   В `/root/projects/unagi-reminder-bot/.env` установить:
   ```ini
   EMAIL_DELIVERY=unagi
   ```

2. **Пересоздание контейнера Unagi:**
   ```bash
   cd /root/projects/unagi-reminder-bot
   docker compose up -d --force-recreate
   ```

3. **Верификация отката:**
   - Unagi возвращается к отправке через встроенный `email_digest.py` (SMTP/msmtp).
   - Курсоры продолжают двигаться в обычном режиме.
   - Логи Unagi: `docker compose logs -f unagi-bot`.
