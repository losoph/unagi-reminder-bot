# Email-дайджест для unagi-reminder-bot: наработки из telegram-daily-digest и аналогов

Дата исследования: 2026-09-28.

Исследование проводилось в `losoph/telegram-daily-digest` (форк `vetalin/telegram-daily-digest`).
Эта копия удаляется, поэтому код и промпты, которые стоит перенести, сохранены в **Приложении A** ниже.
Далее «TDD» означает этот исходный проект.
Цель: собрать готовые наработки для email-дайджеста Telegram-каналов и фидов в проекте
[`losoph/unagi-reminder-bot`](https://github.com/losoph/unagi-reminder-bot) (Python 3.12, aiogram 3, SQLite, Telethon опционально).

---

## 0. Коротко

1. **Ранжирование как в ленте можно сделать без userbot и без LLM.** Публичная страница канала
   `t.me/s/<channel>` уже содержит просмотры каждого поста (`.tgme_widget_message_views`),
   реакции (`.tgme_reaction`) и число подписчиков в шапке канала. Unagi уже загружает эту страницу,
   но берёт из неё только дату, текст и ссылку. Остальные данные достаточно распарсить из того же
   HTML, дополнительных запросов не нужно. Формула — в §2.1.
2. **Серьёзный пробел в unagi: у веб-источника нет пагинации.** `t.me/s` отдаёт около 20 последних
   постов. У активного канала weekly- и monthly-выпуски теряют всё, что старше этих ~20 постов.
   Нужна догрузка через `?before=<post_id>`, пока не дойдём до курсора (§2.4).
3. **Команды из письма делаются без веб-сервера.** В unagi уже есть deep-link `t.me/<bot>?start=<payload>`
   (так сделана отписка). На том же механизме можно собрать «Сохранить», «Напомнить завтра» и «Ещё из
   этой папки» (§2.6). Обычный HTTPS-эндпоинт для one-click опасен: почтовые сканеры сами переходят по
   GET-ссылкам.
4. **Периодичность на уровне папки (категории).** В TDD уже есть модель `ChannelGroup` (A.3), её
   нужно перенести в unagi и дополнить расписанием (§2.3).
5. **Telegraph подходит для публичного рендера, но не для поиска.** В Telegraph API поиска нет.
   Архив ищется локально через SQLite FTS5, а Telegraph хранит страницы выпусков и индекс по месяцам (§2.5).

---

## 1. Что изучено

### 1.1 telegram-daily-digest (TDD) и его форки

| Репо | Роль | Что ценно |
|---|---|---|
| `vetalin/telegram-daily-digest` | upstream, 3★, 1 форк (этот) | исходный PRD/Taskmaster; архитектура userbot → фильтр → AI → бот |
| `losoph/telegram-daily-digest` | форк, переписан на Next.js + worker (удаляется) | см. ниже и Приложение A |

Других форков нет. В поиске по `fork:true` нашлись только одноимённые, но не связанные проекты.

**Что из TDD стоит перенести в unagi** (исходники — в Приложении A):

| Наработка | Где сохранено | Комментарий |
|---|---|---|
| LLM-скоринг поста: `importance 1–10`, `category`, `isAd`, `summary`. Возвращает JSON, при ошибке — fallback | A.1 (`GeminiScorer.ts`) | промпт переносится как есть. Вызывать **только для кандидатов на выпуск**, а не для каждого сообщения, как сейчас (§2.4) |
| Эвристический антиспам: ≥2 рекламных слова, <30 символов, пост только из URL, >50% эмодзи | A.2 (`ContentFilter.ts`, перенесён на Python) | 50 строк без зависимостей. Хорошая база для keyword-фильтров из TODO unagi |
| Группы каналов со своими `aiPrompt`, `maxMessages`, `minImportanceScore`, `analyticsOnly` | A.3 (модель `ChannelGroup`) | готовая модель «категории». В unagi к ней нужно добавить расписание |
| Режим «только аналитика»: один связный обзор со ссылками на источники внутри текста | A.1 (`ANALYTICS_ONLY_PROMPT`) | удобен для weekly/monthly выпусков в письме |
| Catch-up пропущенных выпусков после рестарта (окно 2 ч, учитывает TZ) | A.4 (`DigestCron.ts`) | в TODO unagi есть задачи №1–5 про модель выполнения, эта логика под них подходит |
| Ссылки на посты приватных каналов `t.me/c/<id>/<msg>` | A.5 (`buildMessageLink`) | нужно для MTProto-режима |
| Аудио-версия выпуска (TTS → WAV) | A.6 (`AudioService.ts`, описание) | опционально, «Could» |
| Настройки в Telegram Mini App | TDD `src/app/mini-app/*` (не сохранён) | в unagi настройки на inline-кнопках, Mini App — на будущее |

**Чего из TDD брать не стоит:** постоянно подключённый GramJS-userbot с обработчиком `NewMessage`
на все сообщения и LLM-вызов на каждый пост. Это тот самый «постоянно работающий парсер», который
тратит CPU и токены. Cron в TDD тоже тикает раз в минуту по всем пользователям.

### 1.2 Похожие проекты (по функциям, тегам, ключевым словам)

| Проект | Язык | Что заимствовать |
|---|---|---|
| [hu553in/telekilogram](https://github.com/hu553in/telekilogram) | Go | **RSS/Atom/JSON Feed и публичные TG-каналы в одном боте**. Источник добавляется ссылкой, `@username` или пересылкой. Кэш саммари на 24 ч, сбрасывается при редактировании поста. Без ключа LLM — усечение текста |
| [BOSSincrypto/tg-channel-summary-by-ai](https://github.com/BOSSincrypto/tg-channel-summary-by-ai) | Go | парсинг `t.me/s` без userbot, **дедупликация между каналами внутри группы**, время и TZ на уровне группы, retention постов 90 дней, reconciliation планировщика после рестарта |
| Telebrief ([форк devkade](https://github.com/devkade/telebrief); оригинал belaytzev, ≥7 форков) | Python | **два режима выпуска: по каналам или по AI-темам** (`digest_groups` с описаниями), `dedup_topics` (одна история → один пункт с несколькими источниками), `lookback_hours` и `prompt_extra` на канал, разбиение на сообщения по 4096 символов, MCP-сервер |
| [kitsenior/ai-news-aggregator-template](https://github.com/kitsenior/ai-news-aggregator-template) | Python | **👍/👎 обучают ранжирование** (affinity раздельно по «корзинам»). Слоты «свежее × релевантное» с автобалансом. `/focus` с квотой («не больше 6 новостей про VC»). Семантический дедуп против уже доставленного. Вставленная ссылка сохраняется в архив сразу как 👍 |
| [FedeMotzo/meridian](https://github.com/FedeMotzo/meridian) | Python | **прозрачная формула ранжирования**: 60% семантика + 20% категория + 10% свежесть + 10% доверие к источнику, лимит 30 на выпуск. Дедуп по косинусу ≥ 0.85. Email через SMTP, подкаст |
| [vfichtner/content-digest](https://github.com/vfichtner/content-digest) | Python | «engagement-weighted» саммари TG и YouTube (views/reactions как вес) |
| [drozdorus/asreported-kit](https://github.com/drozdorus/asreported-kit) | Python | **категории = папки Telegram** на аккаунте-читателе. Выпуск 5×/день по cron. Две ссылки: на TG-пост и на внешний первоисточник. Агрессивная агрегация «несколько постов об одном — один пункт». Нейтрализация кликбейта |
| [BetaLayer/tg-digest](https://github.com/BetaLayer/tg-digest) | Python | каналы из папок Telegram, выходные адаптеры Markdown (Obsidian, Notion, Hugo). Запуск раз в день в GitHub Actions, без постоянного процесса |
| [JSkutor/telegram-summary-bot](https://github.com/JSkutor/telegram-summary-bot) | Python | запуск через launchd, состояние «последний успешный прогон», так что пропуски и сбои API догоняются |
| [slurdge/goeland](https://github.com/slurdge/goeland) (207★) | Go | **лучший эталон RSS → email**: адаптивный HTML-шаблон, извлечение полного текста, 20+ фильтров (`unseen`, `today`, `digest`, `language`, `reskip`), **cron на каждый pipe** |
| [ouuan/yaf2m](https://github.com/ouuan/yaf2m) | Rust | группы фидов с дедупом, «письмо на каждый пункт или дайджест» (автодайджест при большом числе обновлений), фильтры and/or/not/regex, **уведомление, если фид сломался** |
| [codemicro/walrss](https://github.com/codemicro/walrss) | Go | email-RSS: daily или weekly в заданное время, **категории фидов**, импорт и экспорт OPML |
| [larspohlmann/simple-feed-reader](https://github.com/larspohlmann/simple-feed-reader) | PHP | теги с цветом и иконкой, **здоровье фида** (active, erroring, gone), предпросмотр фида перед подпиской, автообнаружение фида по адресу сайта, FTS-поиск, email-дайджесты, OPML |
| [SeanLF/news-digest](https://github.com/SeanLF/news-digest) | TS | **LLM не видит URL** (opaque ID `A1…`, защита от prompt-injection через фид). Кластеризация дешёвым extract+join. **Сюжеты, которые тянутся через дни** («что изменилось»). Preheader письма. Проверки перед отправкой |
| [Rongronggg9/RSS-to-Telegram-Bot](https://github.com/Rongronggg9/RSS-to-Telegram-Bot) | Python | самый зрелый RSS→TG. **Длинные посты уходят в Telegraph**, OPML, HTTP-кэширование (ETag/Last-Modified), прокси на каждый фид |
| [DIYgod/RSSHub](https://github.com/DIYgod/RSSHub) `routes/telegram/channel.ts` | TS | эталонный парсер `t.me/s` (типы медиа, форварды, ответы, сервисные сообщения) и универсальный источник фидов: YouTube, Reddit, X, VK и сотни других |
| [sajjadgazergar-work/channel-os](https://github.com/sajjadgazergar-work/channel-os) | Python | Telegraph-лонгриды по расписанию, рейтинг постов по реальным просмотрам |
| [sartoopjj/thefeed](https://github.com/sartoopjj/thefeed), [ircfspace/teleMirror](https://github.com/ircfspace/teleMirror) | Go / JS | **готовый парсинг `tgme_widget_message_views` и `tgme_reaction`** из `t.me/s` |

---

## 2. Требования: что уже есть в unagi, чего нет, как сделать

Базовая линия unagi (по коду на 2026-09-22): подписки на каналы через `t.me/s` (MTProto опционален),
периоды daily/weekly/monthly со временем отправки, папки-теги у подписок, Telegraph от 4 постов,
email с подтверждением кода и выбором «почта или Telegram» на период, закладки, напоминания,
DeepSeek-анализ канала, JSON-экспорт. Планировщик — цикл `check_digests` раз в 30 минут.

| Требование | Статус в unagi | Что добавить |
|---|---|---|
| Ранжирование как в ленте | ❌ посты идут хронологически по каналам | §2.1 |
| RSS и другие фиды | ❌ только TG, OPML в TODO | §2.2 |
| Частота и категории | ⚠️ период на подписку, папки без расписания | §2.3 |
| Лёгкий сервер, работа по расписанию | ✅ в целом (без userbot), ⚠️ нет пагинации, polling раз в 30 мин | §2.4 |
| Архив в Telegraph и поиск | ⚠️ страница только при ≥4 постах, поиска нет | §2.5 |
| Команды из письма и автонапоминание | ⚠️ в письме только отписка через deep-link | §2.6 |

### 2.1 Ранжирование как в ленте (охват, подписчики, реакции)

**Какие данные доступны без MTProto** (тот же HTML `t.me/s/<ch>`, который unagi уже загружает):

| Метрика | Селектор | Пример |
|---|---|---|
| просмотры | `.tgme_widget_message_views` | `795`, `12.3K`, `1.2M` → нужен парсер суффиксов |
| реакции | `.tgme_widget_message_reactions .tgme_reaction` | эмодзи + число, у платных ⭐ свой класс |
| подписчики | `.tgme_channel_info_counter` → `.counter_value` + `.counter_type == "subscribers"` | `48.1K` |
| форвард из | `.tgme_widget_message_forwarded_from` | признак репоста |
| медиа и превью | `.tgme_widget_message_photo_wrap` (`background-image`), `.link_preview_*` | миниатюра для карточки в письме |

Селекторы просмотров и реакций подтверждены живыми HTML-фикстурами от августа 2026
(`AmintaCCCP/GithubStarsManager/src/services/__fixtures__/telegram-channel-*.html`).
Из песочницы t.me заблокирован, поэтому селектор счётчика подписчиков стоит проверить на живой странице.
Через MTProto (Telethon) дополнительно доступны `message.forwards`, `message.replies.replies`,
`message.reactions.results[*].count`, а через `GetFullChannelRequest` — `participants_count`.

**Формула.** Идея из meridian/kitsenior: взвешенная сумма нормированных сигналов. Главное —
**сравнивать пост со средним уровнем его же канала**, иначе крупные каналы забьют выдачу.

```python
def post_score(p, ch, prefs):
    age_h = hours_since(p.time)
    # просмотры ещё растут: делим на ожидаемую долю «зрелых» просмотров
    maturity = max(0.25, 1 - math.exp(-age_h / 8))        # ~70% к 10 ч, ~95% к 24 ч
    views = p.views / maturity

    rel_views = views / max(ch.median_views_30d, 1)       # «выстрелил» относительно своей нормы
    er = (p.reactions_total + 3 * p.forwards + 2 * p.replies) / max(p.views, 1)
    rel_er = er / max(ch.median_er_30d, 1e-4)
    reach = math.log10(ch.subscribers + 10) / 7           # слабый приор «большой канал», 0..1
    fresh = math.exp(-age_h / prefs.half_life_h)          # half_life: daily≈12ч, weekly≈72ч

    s = (0.35 * math.log2(1 + rel_views)
         + 0.25 * math.log2(1 + rel_er)
         + 0.10 * reach
         + 0.15 * prefs.affinity(ch)                     # из 👍/👎 и кликов «сохранить», −1..1
         + 0.15 * fresh)
    if p.llm_importance is not None:                      # опционально, только для топ-N кандидатов
        s = 0.7 * s + 0.3 * (p.llm_importance / 10)
    return s
```

**Сборка выпуска в виде ленты** (чтобы в выдаче не было подряд пяти постов одного канала):

1. Отбросить отфильтрованное: keyword include/exclude и эвристики из `ContentFilter.ts`.
2. Кластеризовать дубли: один внешний URL, либо Jaccard по 3-граммам ≥ 0.5 (дёшево, без эмбеддингов).
   В кластере остаётся пост с максимальным score, остальные идут строкой «также: @ch1, @ch2»
   (как `dedup_topics` в Telebrief).
3. Жадный отбор с понижением за повтор канала: `score × 0.6^(n-й пост этого канала)`, жёсткий
   потолок `max_per_channel` (по умолчанию 3), общий лимит `max_items` папки.
4. Хвост «Остальное из каналов: N постов» даётся ссылкой на полный Telegraph-выпуск. В письме ничего
   не теряется, но показан только топ.

**Как выглядит карточка в письме:** имя канала и число подписчиков · бейдж «🔥 ×3 к обычному» при
`rel_views ≥ 2` · 👁 12K · ❤ 340 · первое предложение жирным, 2–3 строки текста · миниатюра, если есть ·
кнопки «Оригинал | ⭐ Сохранить | ⏰ Напомнить». Функции `_split_first_sentence` и `_build_summary`
в `telegraph_publisher.py` unagi уже умеют готовить такой текст.

**Для RSS-источников** данных о вовлечённости нет. Используется вес источника (задаёт пользователь,
по умолчанию 0.5), свежесть и, опционально, LLM-importance. Чтобы TG- и RSS-посты можно было
смешивать в одной ленте, score переводится в **перцентиль внутри своего источника** за последние
30 дней. У Reddit и HN через RSSHub или hnrss в фиде есть points и comments, их можно подставить
как `views` и `reactions`.

### 2.2 Ручная подписка на RSS и другие популярные фиды

Лучшие решения — у telekilogram, simple-feed-reader, RSStT и goeland.

- **Вход в одну строку**: пользователь присылает ссылку, бот определяет тип:
  - `t.me/…` или `@…` → Telegram (уже есть);
  - URL фида → RSS 2.0, Atom или JSON Feed (`feedparser` понимает все три);
  - URL сайта → автообнаружение `<link rel="alternate" type="application/rss+xml|atom+xml|feed+json">`;
  - `youtube.com/@x` или `/channel/…` → `https://www.youtube.com/feeds/videos.xml?channel_id=…`;
  - `reddit.com/r/x` → `…/r/x/.rss`; Habr, VC, Medium и Substack отдают RSS нативно;
  - остальное (X, VK, Instagram, Bluesky) → опциональный `RSSHUB_BASE_URL` (свой инстанс, в документации
    нельзя опираться на публичный `rsshub.app`);
  - Mastodon: `https://<host>/@user.rss`;
  - email-рассылки → через kill-the-newsletter или свой адрес. Это «Could».
- **Предпросмотр перед подпиской**: последние 5 записей, средняя частота публикаций, есть ли полный
  текст (simple-feed-reader; в TODO unagi это «Preview перед подпиской»).
- **OPML импорт и экспорт** (walrss, RSStT; в TODO unagi уже есть). Туда же можно выгружать TG-каналы
  как `https://t.me/s/<ch>` или ссылку на RSSHub.
- **Экономная загрузка**: условный GET (`ETag`, `If-Modified-Since`, ответ 304 почти ничего не стоит),
  уважать `Cache-Control` и `<ttl>`, не больше 2 параллельных запросов на хост, джиттер.
- **Здоровье фида**: статусы `active`, `erroring` (N ошибок подряд), `gone` (410/404 дольше 7 дней),
  одно агрегированное уведомление (yaf2m, simple-feed-reader). Хорошо ложится на пункты 1–5 TODO unagi.
- **Полный текст** (goeland): `trafilatura` по запросу, только для постов, которые попали в выпуск.

Модель данных: вместо отдельной ветки для RSS — обобщить `subscriptions.channel_username` до
`source_id → sources(kind, url, title, etag, last_modified, health, …)`. Тогда ранжирование, папки,
Telegraph и email работают одинаково для любого источника.

### 2.3 Частота выпусков и категории со своей периодичностью

Сейчас в unagi период хранится у каждой подписки, а папка (`tag`) — только группировка для UI.
Предложение: сделать **папку полноценной сущностью с расписанием**, по образцу `ChannelGroup` из TDD
(см. A.3), walrss и goeland (cron на pipe):

```
folders(id, user_id, name, emoji,
        schedule_kind  -- daily | weekly | monthly | cron | off
        send_hour, send_minute, weekday, month_day,  -- как в digest_settings
        cron_expr NULL,                               -- напр. "0 8 * * 1-5" (будни)
        delivery       -- email | telegram | both
        max_items, max_per_channel, min_score,
        mode           -- feed (лента) | by_channel | by_topic | analytics_only
        ai_prompt NULL, half_life_h, next_send_at, last_sent_at)
subscriptions.folder_id  -- NULL = «Без папки» с пользовательскими дефолтами
subscriptions.period_override NULL -- редкое исключение для конкретного канала
```

- Приоритет правил: переопределение у подписки → настройки папки → глобальные по умолчанию
  (в TODO unagi есть «глобальная периодичность по умолчанию»).
- **Одна папка — одно письмо.** В один момент может прийти несколько писем, например «Новости»
  ежедневно в 8:00 и «Длинное чтение» по субботам. Опционально их можно склеить в одно письмо
  с разделами (goeland: несколько pipes в один email).
- Режимы из Telebrief и TDD: `feed` — ранжированная лента из §2.1; `by_channel` — текущий
  вид unagi; `by_topic` — AI-группировка по заданным темам; `analytics_only` — связный обзор, хорош
  для monthly.
- **Импорт папок Telegram** (asreported-kit, BetaLayer). Если подключён MTProto и у пользователя
  нет своей раскладки, папки аккаунта (`messages.getDialogFilters`) можно предложить как готовые
  категории. «Could»: работает только с MTProto и только для аккаунта-читателя, не для пользователя бота.
- UX на inline-кнопках, как сейчас: «папка → ⏰ Расписание → Ежедневно / Будни / Раз в неделю…».

### 2.4 Лёгкость сервера и работа строго по расписанию

Что уже хорошо в unagi: нет постоянного userbot, канал загружается один раз за цикл в общую
таблицу `channel_posts`, курсоры сдвигаются только после успешной доставки, есть batch-лимит.

Что улучшить:

1. **Засыпать до ближайшего `next_send_at`** вместо опроса раз в 30 минут. Цикл делает
   `sleep(min(next_due - now, 30 min))` и просыпается по `asyncio.Event`, когда пользователь меняет
   настройки. Нагрузка в простое — ноль, выпуск уходит точно в назначенное время, а не в пределах
   30-минутного окна.
2. **Загружать только то, что скоро будет отправлено.** За 60–90 минут до `next_send_at` папки
   загружаются её источники. Метрики (просмотры, реакции) снимаются в этот момент, свежие посты
   успевают набрать просмотры, а при сбое остаётся время на повтор. Совпадает с пунктом 7 TODO
   unagi («отделить ingestion от доставки»), но без постоянного фонового парсинга.
3. **⚠️ Пагинация `t.me/s`.** Страница отдаёт около 20 постов. Для weekly и monthly нужно догружать
   `https://t.me/s/<ch>?before=<min_post_id>`, пока `post_time > cursor`, с потолком (например,
   10 страниц) и паузой 0.5–1 с. Сейчас у каналов с >20 постами за период weekly и monthly выпуски
   молча теряют старые посты. MTProto-путь (`channel_source.py`, `iter_messages` до
   `MTPROTO_FETCH_LIMIT`) этой проблемы не имеет. Задача вынесена в `TODO.md`.
4. **Хранить полный текст** в `channel_posts`, а не обрезанный до 300 символов (в TODO это уже есть).
   Это нужно для Telegraph-архива, FTS-поиска и LLM.
5. **Метрики обновлять при тех же загрузках, отдельного прохода не нужно**: `UPDATE channel_posts SET
   views=…, reactions_total=…, metrics_at=…`. Подписчиков обновлять раз в сутки на канал.
6. **LLM — только батчем и только для кандидатов**: топ-N после метрического ранжирования, один
   запрос на выпуск со списком (как `DIGEST_SUMMARY_PROMPT` в A.1), кэш саммари по `(post, edited_at)`
   (telekilogram). Никаких вызовов на каждое входящее сообщение, как в воркере TDD.
7. Альтернативный вариант совсем без постоянного процесса (BetaLayer, JSkutor): системный cron или
   systemd-timer запускает `python -m unagi.digest_run`. Для unagi не нужен: бот всё равно держит
   long-polling, а asyncio-таймер внутри него ничего не стоит.

### 2.5 Сохранение сводок в Telegraph: индексация и поиск по архиву

Что есть: Telegraph-страница при ≥4 постах, таблица `telegraph_digests`, email-вёрстка из того же
node-дерева. В TODO уже описана хорошая модель `digest_editions` / `digest_edition_items`.

Предложение:

- **Каждый выпуск — неизменяемый snapshot** (`digest_editions`) и **всегда страница в Telegraph**,
  а не только от 4 постов. Для маленьких выпусков — лениво, при первом «Сохранить» или «Открыть архив».
- **Индексные страницы**: одна Telegraph-страница на месяц и папку («Unagi · Новости · сентябрь 2026»)
  со списком выпусков. Обновляется через `editPage` при каждом новом выпуске. Корневой индекс —
  список месяцев. Итог: «архив подписок» одной ссылкой, пролистываемый в Instant View.
- **Поиск делать локально, не в Telegraph.** В Telegraph API нет поиска (есть только `getPageList`
  своего аккаунта), индексация поисковиками не гарантирована, и выносить на это приватные интересы
  нежелательно. Нужна SQLite FTS5-таблица поверх `channel_posts(post_text)` и `digest_editions(title)`,
  с триграммным токенайзером `tokenize='trigram'` для кириллицы и морфологии. Команда `/search <запрос>`
  (и ссылка «🔎 Поиск по архиву» в письме через deep-link) возвращает посты с ссылками на оригинал и
  на Telegraph-выпуск, где они были.
- Ограничения Telegraph учесть так: ≤64 КБ на страницу (в unagi уже `_MAX_CONTENT_BYTES`, при
  превышении — серия страниц и индекс); FLOOD_WAIT при массовом создании (очередь с паузами);
  страницы публичны (не класть user_id и настройки, как отмечено в TODO); самовосстановление
  токена при `ACCESS_TOKEN_INVALID` (пункт 8 TODO).
- «Could»: семантический поиск (эмбеддинги в `sqlite-vec`) и LLM-ответ по архиву («что писали про X
  за месяц») — у kitsenior и Telebrief это сделано через MCP-сервер. Для unagi это логичное
  продолжение DeepSeek-анализа.

### 2.6 Команды из письма и автоотправка сохранённого напоминанием

Варианты, от лучшего к худшему для unagi:

| Способ | Как работает | Плюсы | Минусы |
|---|---|---|---|
| **A. Deep-link в бота** (рекомендую) | кнопка в письме → `https://t.me/<bot>?start=sv_<item>` → бот получает `/start sv_123` | сервер не нужен, уже используется для `em_off`, пользователь авторизован самим Telegram | открывается Telegram. Payload ≤64 символов `[A-Za-z0-9_-]`. В уже открытом чате некоторые клиенты просят нажать «Start» |
| B. HTTPS one-click | `https://bot.example/a/<signed-token>` → сохранить → страница «Готово ✓» | не выходим из почты | нужен веб-сервер и домен. **GET-ссылки прокликивают сканеры** (Outlook SafeLinks, корпоративные антивирусы), поэтому нужен промежуточный экран с POST-кнопкой. Токен — HMAC(user, item, action, exp) |
| C. Ответ на письмо | «ответьте `save 3`» → IMAP-поллинг по расписанию | работает в любом клиенте | нужен IMAP-ящик, парсинг цитат, задержка. Хрупко |

**Предлагаемые действия** (payload — короткий, проверяем, что item принадлежит `from_user.id`, секрет
в ссылке не нужен):

| Кнопка | payload | Что делает бот |
|---|---|---|
| ⭐ Сохранить | `sv_<edition_item_id>` | создаёт `saved_messages` (полный текст, ссылка, канал, папка = папка подписки) и отвечает карточкой с кнопками «Перенести в папку / Напомнить» |
| ⏰ Напомнить | `rm_<item>` | сохранить и сразу поставить `scheduled_messages` на «завтра утром» (время из настроек) и показать уже существующие кнопки переноса (+1 ч, вечер, дата) |
| ⏰ В выходные | `rw_<item>` | то же, на ближайшую субботу |
| 📚 Весь выпуск | `ed_<edition_id>` | закладка на Telegraph-выпуск (сценарий из TODO) |
| 🔕 Меньше такого | `dn_<sub_id>` | 👎 к affinity канала; в UI — «пауза / отписка» |
| 🔎 Архив | `sr` | открыть `/search` |

**Автоотправка сохранённого:**
- «Напомнить» из письма создаёт обычное напоминание unagi, и оно приходит в **Telegram** с полным
  текстом поста и ссылкой. Это связывает три ключевые функции бота: дайджест, закладки и отложенные.
- Дополнительно — раздел **«Вы откладывали»** в конце следующего письма той же папки (Pocket-подобный
  возврат) и еженедельный «📌 Не прочитано из сохранённого» (Could).
- Каждое сохранение — сигнал ранжирования: +affinity каналу и папке (как «вставленная ссылка = 👍»
  у kitsenior).

**Технические детали письма:** `List-Unsubscribe` и `List-Unsubscribe-Post: List-Unsubscribe=One-Click`
(требования Gmail и Yahoo с 2024 года, частично есть — `unsubscribe_url`); preheader (скрытый первый
абзац с тремя главными заголовками — SeanLF); inline-стили и таблицы, без внешнего CSS (уже так);
multipart с plain-text (уже так); `Message-ID` и `In-Reply-To` для цепочек по папке в почтовом
клиенте (Could).

---

## 3. Что ещё оказалось востребованным в чужих проектах (обсудить)

Оценка: **M** — must для email-сборки, **S** — should, **C** — could, **✗** — не рекомендую.

| # | Функция | Где встречается | Оценка | Комментарий |
|---|---|---|---|---|
| 1 | Дедуп одной истории между каналами | Telebrief, BOSSincrypto, asreported, meridian, kitsenior | **M** | без него лента из 30 каналов повторяет одно событие 5 раз. Сначала дёшево (URL + n-граммы), эмбеддинги потом |
| 2 | Keyword include/exclude на подписку и папку | yaf2m, goeland, TODO unagi | **M** | дёшево, закрывает рекламу вместе с эвристиками из `ContentFilter.ts` |
| 3 | 👍/👎 и «сохранить» как обучение ранжирования | kitsenior, meridian | **S** | через deep-link из письма. Affinity только ранжирует и ничего не скрывает |
| 4 | Здоровье источников и агрегированные алерты | yaf2m, simple-feed-reader, TODO 1–6 | **S** | уже спланировано в TODO |
| 5 | OPML импорт и экспорт | walrss, RSStT, simple-feed-reader | **S** | в TODO |
| 6 | Режим выпуска по AI-темам | Telebrief | **S** | как `mode` папки |
| 7 | Ссылка на первоисточник рядом с TG-постом | asreported-kit | **S** | извлекать первый внешний URL поста |
| 8 | Обзор «главное за период» (analytics-only) для weekly/monthly | TDD, Telebrief Overview | **S** | промпт — A.1. В TODO unagi: «Топ-5 главных за неделю» |
| 9 | LLM не видит URL (opaque ID) | SeanLF | **S** | обязательно, если посты идут в LLM: защита от инъекций из чужих каналов |
| 10 | Сюжеты через дни («что изменилось») | SeanLF | C | дорого. После дедупа и эмбеддингов |
| 11 | `/focus` с квотой («не больше N про X») | kitsenior | C | можно как правило папки |
| 12 | Баланс «свежее ↔ релевантное» | kitsenior | C | в формуле это `half_life_h` и вес `fresh` |
| 13 | Аудио-версия выпуска | TDD, meridian | C | TTS платный, по кнопке |
| 14 | MCP или API к архиву | Telebrief, kitsenior | C | после FTS |
| 15 | Экспорт выпусков в Obsidian или Notion (Markdown) | BetaLayer, JSkutor | C | почти бесплатно при наличии `digest_editions` |
| 16 | Кэш саммари с инвалидацией по редактированию | telekilogram | S (если LLM) | экономия токенов |
| 17 | Нейтрализация кликбейта в саммари | asreported-kit | C | одна строка промпта |
| 18 | Постоянный userbot с real-time обработкой | TDD, upstream | ✗ | противоречит требованию «без постоянных парсеров» |
| 19 | Mini App для настроек | TDD, BOSSincrypto | C | inline-кнопки unagi пока справляются |

---

## 4. Дельта схемы SQLite для unagi (эскиз)

```sql
-- источники: TG и фиды одной сущностью
CREATE TABLE sources (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL,            -- tg | rss | atom | jsonfeed | rsshub
  key TEXT NOT NULL UNIQUE,                              -- @username или нормализованный URL
  title TEXT, site_url TEXT, weight REAL DEFAULT 0.5,
  subscribers INTEGER, subscribers_at DATETIME,
  median_views_30d REAL, median_er_30d REAL,
  etag TEXT, last_modified TEXT,
  health TEXT DEFAULT 'active', fail_count INTEGER DEFAULT 0, last_error TEXT
);
-- channel_posts → source_posts: полный текст и метрики
ALTER TABLE channel_posts ADD COLUMN views INTEGER;
ALTER TABLE channel_posts ADD COLUMN reactions_total INTEGER;
ALTER TABLE channel_posts ADD COLUMN reactions_json TEXT;
ALTER TABLE channel_posts ADD COLUMN forwards INTEGER;
ALTER TABLE channel_posts ADD COLUMN replies INTEGER;
ALTER TABLE channel_posts ADD COLUMN metrics_at DATETIME;
ALTER TABLE channel_posts ADD COLUMN external_url TEXT;
ALTER TABLE channel_posts ADD COLUMN media_thumb TEXT;
CREATE VIRTUAL TABLE posts_fts USING fts5(post_text, content='channel_posts', tokenize='trigram');

CREATE TABLE folders (...);                              -- см. §2.3
CREATE TABLE digest_editions (id, user_id, folder_id, period_start, period_end,
  scheduled_at, status, title, telegraph_url, delivered_via, created_at);
CREATE TABLE digest_edition_items (edition_id, post_key, rank, score, cluster_id, shown INTEGER);
CREATE TABLE feedback (user_id, source_id, post_key, kind, created_at);  -- up/down/save/click
```

---

## 5. Порядок внедрения

| Этап | Содержание | Оценка |
|---|---|---|
| 1 | Пагинация `t.me/s?before=`, полный текст, парсинг просмотров, реакций и подписчиков, миграция колонок | 1–2 дня |
| 2 | Ранжирование «лента» (§2.1): медианы канала, score, дедуп по URL и n-граммам, лимит на канал. Карточки в письме с метриками | 2–3 дня |
| 3 | `folders` с расписанием и режимами, «засыпать до next_due», префетч за 60–90 минут | 2–3 дня |
| 4 | `digest_editions` + Telegraph для каждого выпуска + индексы по месяцам + FTS5 `/search` | 2–3 дня |
| 5 | Deep-link-действия в письме (sv, rm, rw, ed, dn), раздел «Вы откладывали», affinity из фидбэка | 1–2 дня |
| 6 | RSS, Atom и JSON Feed через `feedparser`, автообнаружение, YouTube и Reddit, OPML, health, условный GET | 2–3 дня |
| 7 | Опционально: LLM-importance для топ-N, analytics-only для weekly и monthly, by_topic | 1–2 дня |

---

## 6. Риски и подводные камни

- **`t.me/s` — не API.** Разметка может поменяться, у части каналов веб-превью закрыто. У unagi уже
  есть `ChannelFetchError` и fallback на MTProto. Нужен тест на HTML-фикстуре и алерт, если метрики
  у всех каналов разом стали `NULL`.
- **Просмотры растут со временем.** Без поправки на «зрелость» (§2.1) утренний выпуск всегда
  недооценивает вечерние посты.
- **Числа вида `12.3K` и `1,2 млн`.** Нужен парсер суффиксов (K, M, тыс, млн) и разделителей.
- **Миниатюры `cdn*.telesco.pe`** могут протухать, а почтовые клиенты режут внешние картинки. Нужен
  alt-текст, вёрстка не должна зависеть от картинок.
- **Сканеры ссылок в почте.** Действия только через deep-link или POST (§2.6).
- **Telegraph публичен.** В страницах никакой персональной информации. Индекс архива не должен
  раскрывать список подписок, если это нежелательно: можно не давать индексу заголовок с именем.
- **Deliverability.** SPF, DKIM и DMARC на домене `SMTP_FROM`, One-Click unsubscribe, ровное время
  отправки. Для больших объёмов — провайдер вроде Resend, Postmark или SES вместо личного SMTP.

---

---

## Приложение A. Наработки из telegram-daily-digest (сохранены перед удалением репо)

Исходный стек TDD: Next.js 14, воркер на GramJS, Prisma/PostgreSQL, OpenRouter (`google/gemini-3-flash-preview`).
Ниже только то, что имеет смысл переносить в unagi. Промпты даны дословно.

### A.1 LLM-промпты (`src/services/GeminiScorer.ts`)

**Скоринг одного поста** (`response_format: json_object`, текст обрезается до 2000 символов;
при ошибке парсинга fallback `importance=5, category=other, isAd=false, summary=text[:200]`;
`importance` зажимается в 1..10):

```text
You are a news importance evaluator. Analyze the given Telegram message and respond with JSON only.

Rate the importance from 1 to 10:
- 10: Breaking news, major world events, crises
- 7-9: Important political, economic, or social news
- 4-6: Noteworthy developments, analysis
- 1-3: Minor updates, routine information

Categories: politics, economy, technology, science, society, sports, culture, other

Respond with valid JSON matching this schema:
{
  "importance": number (1-10),
  "category": string,
  "isAd": boolean,
  "summary": string (1-2 sentences in Russian)
}
```

**Краткое аналитическое резюме к выпуску** (на вход — нумерованный список
`N. [category] Канал: summary (важность: X.X)`; кастомный промпт папки дописывается как
`\n\nДополнительные инструкции: <aiPrompt>`):

```text
Ты — аналитик новостей. Тебе дан список самых важных новостей за день из Telegram-каналов пользователя.
Напиши краткое аналитическое резюме на русском языке:
- Выдели 3-5 главных тем/событий дня
- Проанализируй связи между событиями, если они есть
- Сделай краткие выводы о том, что происходит
- Укажи, на что стоит обратить особое внимание

Формат ответа: HTML для Telegram (не JSON, не Markdown). Используй только теги: <b>заголовок</b>, <i>курсив</i>. Для разделов используй <b>Заголовок</b> на отдельной строке. Для пунктов используй символ • в начале строки. Не используй # ## ### ** __ и другие Markdown-символы.
```

**Режим «только аналитика»** (вместо списка постов — один обзор со ссылками в тексте; в список
добавляется `[ссылка: <url>]`). Если использовать с unagi, стоит заменить URL на opaque ID и
подставлять ссылки после ответа модели (приём SeanLF, §3 п.9):

```text
Ты — аналитик новостей. Тебе дан список самых важных новостей за день из Telegram-каналов пользователя. Каждая новость сопровождается ссылкой на оригинал.
Напиши развёрнутый аналитический обзор на русском языке:
- Выдели и подробно разбери 3-7 главных тем/событий дня
- Для каждой темы: объясни суть, контекст, возможные последствия
- Проанализируй взаимосвязи между событиями
- Дай оценку значимости происходящего
- Укажи, на что стоит обратить особое внимание в ближайшее время
- Для каждой упомянутой новости вставь HTML-ссылку на оригинал в формате: <a href="ССЫЛКА">краткий текст</a>

ВАЖНО: Это единственное сообщение, которое получит пользователь — без отдельного списка новостей. Поэтому аналитика должна быть подробной и самодостаточной, со ссылками на источники прямо в тексте.

Формат ответа: HTML для Telegram (не JSON, не Markdown). Используй только теги: <b>заголовок</b>, <i>курсив</i>, <a href="...">текст</a>. Для разделов используй <b>Заголовок</b> на отдельной строке. Для пунктов используй символ • в начале строки. Не используй # ## ### ** __ и другие Markdown-символы.
```

### A.2 Эвристический фильтр рекламы и мусора (`src/services/ContentFilter.ts`, перенесён на Python)

```python
import re

AD_WORDS = [
    "реклама", "промокод", "скидка", "купить", "партнёр", "партнер", "спонсор",
    "акция", "распродажа", "promo", "discount", "sponsor", "ad ", "#ad",
]
URL_ONLY_RE = re.compile(r"^(https?://\S+\s*)+$")
EMOJI_RE = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u27BF]")


def filter_message(text: str) -> tuple[bool, str | None]:
    """(filtered, reason). Пороги как в TDD."""
    trimmed = text.strip()
    if len(trimmed) < 30:
        return True, "too_short"
    if URL_ONLY_RE.match(trimmed):
        return True, "url_only"
    lower = trimmed.lower()
    if sum(word in lower for word in AD_WORDS) >= 2:
        return True, "ad_keywords"
    words = trimmed.split()
    if len(words) < 20 and len(EMOJI_RE.findall(trimmed)) / len(words) > 0.5:
        return True, "emoji_spam"
    return False, None
```

В TDD отфильтрованные посты всё равно сохранялись (`isFiltered=true`), чтобы не проверять их повторно.

### A.3 Модель группы каналов (`prisma/schema.prisma`)

```prisma
model ChannelGroup {
  id                 Int     @id @default(autoincrement())
  userId             Int
  name               String
  aiPrompt           String?          // дописывается к промпту резюме
  maxMessages        Int     @default(30)
  minImportanceScore Float   @default(1)
  analyticsOnly      Boolean @default(false)
}
// UserChannel.groupId -> ChannelGroup (onDelete: SetNull); каналы без группы
// собираются в отдельный выпуск с настройками пользователя по умолчанию.
```

Выпуск в TDD: на каждую группу отдельный дайджест (`WHERE importanceScore >= min
ORDER BY importanceScore DESC LIMIT maxMessages`, без рекламы и отфильтрованного), затем
«аналитика» отдельным сообщением с кнопкой «🔊 Получить аудио вывод».
В unagi это ложится на `folders` из §2.3 (плюс расписание, которого в TDD не было: там было одно
время `digestTime` на пользователя).

### A.4 Догон пропущенных выпусков (`worker/DigestCron.ts`)

- При старте: всем активным пользователям, у кого назначенное время сегодня (в их TZ) было
  **не более 2 ч назад** и сегодня ещё нет выпуска со статусом `SENT`, выпуск отправляется сразу.
- Каждую минуту: отправка тем, у кого `HH:MM` в их TZ совпало с `digestTime`, если сегодня ещё
  не было `SENT`.
- Начало «сегодня» в TZ пользователя: дата через `toLocaleDateString('en-CA', {timeZone})` и
  смещение `toLocaleString(... timeZone) - now`.

Для unagi полезна сама идея окна догона и проверки «уже отправлено сегодня». Поминутный опрос
не нужен: в unagi есть `next_send_at`, а в §2.4 предложено засыпать до ближайшего срока.

### A.5 Ссылка на пост приватного канала (`DigestService.buildMessageLink`)

```python
def build_message_link(username: str | None, telegram_channel_id: int, msg_id: int) -> str:
    if username:
        return f"https://t.me/{username}/{msg_id}"
    numeric = str(telegram_channel_id).removeprefix("-100")
    return f"https://t.me/c/{numeric}/{msg_id}"   # откроется только у участников канала
```

### A.6 Аудио-версия выпуска (`src/services/AudioService.ts`)

Текст аналитики очищается от HTML (`<br>`, `</p>`, `</li>` → перевод строки, остальные теги
удаляются, сущности декодируются) и уходит в OpenRouter: `chat.completions` с моделью
`openai/gpt-4o-audio-preview`, `modalities: ['text','audio']`, `audio: {voice: 'alloy', format: 'pcm16'}`,
`stream: true`, промпт «Прочитай вслух следующий текст аналитики: …». Base64-чанки `delta.audio.data`
склеиваются в PCM16, 24 кГц, моно; к нему дописывается 44-байтный WAV-заголовок (`RIFF`/`WAVE`/`fmt `/`data`),
затем файл уходит в Telegram как аудио. Запускается по inline-кнопке `audio:<digestId>`.

