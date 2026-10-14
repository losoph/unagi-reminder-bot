T19b — правки по ревью T19/T20 (agy, первым коммитом второго этапа: «fix: модель данных и UX Ленты по ревью (T19, T20)»)

Подтверждено наблюдателем:
1. Импорт канала, который уже есть в Ленте, молча меняет тег и не снимает паузу, но считается
   «добавленным». Решение: существующую подписку импорт не трогает и не считает; в отчёте —
   «уже были в Ленте: N». (data/database.py add_feed_subscription / main.py _import_user_payload)
2. feed_editions не чистится. Добавить в cleanup_old_records удаление выпусков старше 2 суток.
3. get_feed_candidate_posts / save_feed_edition / get_feed_edition принимают строки окна без
   нормализации (дробные секунды, смещение пояса). Принимать только aware datetime (строку — через
   parse с ValueError на неверный формат).
4. Проверка MAX_FEED_CHANNELS и вставка не атомарны: делать в одной транзакции BEGIN IMMEDIATE.
5. Обрезка тега до 24 символов может оставить хвост составного эмодзи (ZWJ, VS16, модификатор кожи):
   после обрезки срезать висячие U+200D, U+FE0F, U+1F3FB–U+1F3FF.
6. data/database.py:632 — дублирует CREATE INDEX idx_channel_posts_channel_time (строка 569); убрать.
7. feed.py:468 — set_feed_tag(message.chat.id, …) → message.from_user.id (единообразие с колбэками).
8. После удаления канала список открывается с первой страницы: возвращать на ту же страницу
   (или предыдущую, если она опустела).
Каждый пункт — тест.

По T21 (подтверждено наблюдателем):
9.  feed.py ingest_window: feed_backfilled_at ставится и при source == "web" (веб отдаёт ~20 последних
    постов; MTProto при > MTPROTO_FETCH_LIMIT бросает ChannelFetchError → фолбэк на web) → месячная
    Лента канала навсегда неполная. Ставить отметку только при source == "mtproto".
10. Семафор Ленты (feed.get_fetch_semaphore) не общий с дайджестом: main.py создаёт свои
    asyncio.Semaphore(DIGEST_FETCH_CONCURRENCY) в трёх местах → до 10 параллельных запросов к MTProto
    (риск FloodWait на аккаунте). Один общий семафор (например, в channel_source), main и feed берут его.
11. update_channel_scan_cursor не монотонен: при загрузке от window_start < курсора и без постов новее
    курсора курсор уходит назад. Двигать только вперёд (max с текущим).
12. channel_source.refresh_metrics: zip(batch, views) по позиции без проверки длины. Если
    len(views) != len(batch) — WARNING и прекратить обновление канала (не сопоставлять со сдвигом).
13. Задачи фоновой подгрузки (feed._backfill_tasks) не отменяются при остановке бота: отменить и
    дождаться в shutdown рядом с остальными фоновыми задачами main.
