# Сервер aiqu (новый): конфигурация, харденинг, миграция

Сервер, где работают unagi и ghsub (и telemt). Здесь хранится всё, что нужно, чтобы
воспроизвести настройки на другой машине. **Секретов здесь нет и быть не должно**: `.env` бота,
`/root/.msmtprc`, `/etc/tldw/health.env`, `/etc/telemt/*` переносятся отдельно, вне git.

## Инвентарь (2026-10-11)

| Что | Значение |
|---|---|
| Хост | `89.124.104.144`, vdsina (`v1068027.hosted-by-vdsina.com`), вне России |
| ОС | Ubuntu 24.04 LTS, 1 vCPU-класс, **961 МБ RAM**, диск 9,8 ГБ (занято ~69%) |
| Часовой пояс | `Europe/Moscow` (выставлен 2026-10-11; был `Europe/Amsterdam`) |
| SSH | только порт **2258**, только ключи; доступ из России — **только через VPN** |
| unagi | `/root/projects/unagi-reminder-bot`, docker compose, контейнер `unagi-bot`, `network_mode: host`, `mem_limit: 512m` |
| ghsub | `/root/ghsub`, крон из `install.sh` (автодеплой :17, дайджест вс 07:00), состояние `/var/lib/tldw/ghsub` |
| telemt | systemd `telemt`, `telemt-upstream-probe`, `telemt-deploy.timer`; nginx `cdn.orangerd.ru` :80/:443, прокси :8443 |
| Telegram для бота | напрямую (сервер за рубежом); OVPN-туннеля `tun11` здесь **нет** |

## Применённый харденинг (2026-10-11)

| Мера | Файл / команда |
|---|---|
| Только ключи, root только по ключу, без X11/agent forwarding, `MaxAuthTries 3`, `LoginGraceTime 30`, keepalive | [`sshd/00-hardening.conf`](sshd/00-hardening.conf) → `/etc/ssh/sshd_config.d/` |
| Порт 22 закрыт (sshd и ufw), остаётся 2258 | `/etc/ssh/sshd_config`: `# Port 22`; `ufw delete allow 22/tcp` |
| Ограничение частоты подключений к SSH | `ufw limit 2258/tcp` |
| fail2ban: 3 попытки, нарастающий бан до недели, баны на все порты | [`fail2ban/10-sshd-hardening.local`](fail2ban/10-sshd-hardening.local) → `/etc/fail2ban/jail.d/` |
| Security-обновления автоматически (без авто-ребута) | `unattended-upgrades` включён; 96 пакетов поставлены 2026-10-11 |
| Резервные копии до изменений | `/root/sshd_config.bak-20261011`, `/root/authorized_keys.bak-20261011` |

Почему `00-`: провайдерский cloud-init кладёт `50-cloud-init.conf` с `PasswordAuthentication yes`,
а sshd берёт **первое** встреченное значение. Наш файл читается раньше и побеждает, даже если
cloud-init перепишет свой.

Ключи в `/root/.ssh/authorized_keys`: два ключа владельца, `claude-code aiqu` (рабочая машина,
`ssh aiqu`), `github-actions unagi-reminder-bot deploy` (CI-деплой unagi, секреты репозитория
`HOST`/`SSH_PORT=2258`/`USERNAME`/`SSH_KEY` обновлены 2026-10-11). Отзыв — удалить строку.

### Контроль прода (после каждого деплоя и по требованию)

```bash
ops/aiqu/prod_check.sh                  # версия = origin/main, контейнер, ошибки в логах, цикл дайджеста, БД, диск, бэкап
ops/aiqu/prod_check.sh --since 10m      # после деплоя — только свежие логи
```

Только чтение, один ssh-сеанс (`PROD_SSH`, по умолчанию `aiqu`). Строки `OK` / `WARN` / `ALERT`, код выхода 1 при `ALERT`. Правка `docker-compose.yml` на сервере (лимит CPU) известна и не считается «грязным» деревом.

### Лимит CPU контейнера

`docker-compose.yml` берёт лимит из `BOT_CPUS` (по умолчанию 1.0 (хост 1 vCPU: Docker отвергает больше)). На сервере нужно добавить в `.env` рядом с compose строку `BOT_CPUS=1.0` — после этого ручная правка `docker-compose.yml` не нужна, а деплой сам сбрасывает ручные правки compose (`git checkout HEAD -- docker-compose.yml`, затем `git pull --ff-only`; `--autostash` убран 17.10: при конфликте возврата stash он возвращал код 0, в compose оставались маркеры, и `docker compose up` падал на YAML). `ops/aiqu/prod_check.sh` даёт ALERT, если в рабочем дереве сервера есть конфликт слияния.

### Контейнер unagi-bot: права процесса

Ревью безопасности п.7. Образ запускается через `ops/docker/entrypoint.sh`: от root он на доли секунды передаёт `storage/` пользователю bot (uid 10001) и кладёт копию `/root/.msmtprc` в tmpfs `/run/unagi` (на хосте файл закрыт 0600 для root), затем навсегда сбрасывает права (`setpriv`). Процесс бота: не root, без capabilities, `no-new-privileges`, корневая ФС read-only, записываемы только `storage/`, `/tmp` и `/run/unagi` (tmpfs). Привилегии `cap_add` в `docker-compose.yml` нужны лишь точке входа и исчезают вместе с root.

- `network_mode: host` остаётся: бот наследует маршрутизацию хоста (OVPN-туннель до Telegram DC после переезда). Снимать только с проверкой маршрутов.
- Файлы в `storage/` теперь принадлежат uid 10001 (при каждом старте контейнер возвращает им этого владельца). Скрипты на хосте (`backup_db.py`, `prod_check.sh`) работают от root и читают их как раньше.
- Проверка: `ops/aiqu/prod_check.sh` — строки «права процесса» и «email-дайджесты»; в CI тот же набор флагов проверяет шаг «Smoke test hardened container».
- Откат: `git revert <коммит>` и push в main — деплой вернёт прежний образ (владелец файлов в `storage/` root не мешает: root читает всё).

### Контроль версии работающего контейнера и быстрый откат (DEP2)

17.10 дважды возникала ситуация: деплой падал при `up`, контейнер оставался на старом образе, а `prod_check.sh` рапортовал «версия OK», так как сверял только `git rev-parse HEAD` в репозитории на сервере, а не версию кода внутри запущенного контейнера.

**Метка версии контейнера:**
- В `docker-compose.yml` контейнер помечен меткой `labels: {org.unagi.git_sha: "${GIT_SHA:-unknown}"}`.
- В `.github/workflows/deploy.yml` перед `docker compose up` экспортируется `GIT_SHA="$(git rev-parse --short HEAD)"`.
- `ops/aiqu/prod_check.sh` читает метку через `docker inspect -f '{{ index .Config.Labels "org.unagi.git_sha" }}'` и выдаёт:
  * `ALERT "контейнер на старом коде: метка X, HEAD Y"` — если метка не совпадает с HEAD в git на сервере;
  * `WARN "контейнер: метка версии unknown/пустая (первый деплой после внедрения)"` — при значении `unknown`, пустой строке или отсутствии метки;
  * `OK "контейнер: код X (HEAD X)"` — при совпадении.

**Проверка живости при деплое:**
- В `deploy.yml` сразу после `docker compose up -d` выполняется проверка: пауза до 20 с (`sleep "${DEPLOY_WAIT_SECS:-20}"`), затем проверка через `docker inspect`, что `State.Status == running` и `RestartCount == 0`.
- Если контейнер не запустился или упал в цикличный перезапуск, деплой немедленно выводит последние 30 строк логов (`docker logs --tail 30 unagi-bot`) и завершается с ошибкой (`exit 1`).

**Откатный тег образа и как откатиться:**
- Перед сборкой нового образа (`up --build`) CI помечает текущий рабочий образ:
  ```bash
  docker tag unagi-reminder-bot-bot:latest unagi-reminder-bot-bot:rollback 2>/dev/null || true
  ```
- Очистка образов `docker image prune -f` удаляет **только dangling (безымянные)** слои без тегов (`<none>:<none>`). Образ с тегом `:rollback` защищён от удаления.
- **Быстрый откат на сервере (без пересборки):**
  1. Переключить тег `latest` на сохранённый образ `rollback` и поднять контейнер:
     ```bash
     cd ~/projects/unagi-reminder-bot
     docker tag unagi-reminder-bot-bot:rollback unagi-reminder-bot-bot:latest
     docker compose up -d
     ```
  2. Вернуть репозиторий на сервере к откатному коммиту (чтобы `prod_check` видел согласованный HEAD):
     ```bash
     git checkout <sha>
     ```
- **Откат с пересборкой из git (если тега нет или нужен другой коммит):**
  ```bash
  git checkout <sha>
  export GIT_SHA="$(git rev-parse --short HEAD)"
  docker compose up -d --build
  ```

### Резервные копии БД и восстановление

Перед каждым деплоем шаг CI выполняет:
```bash
python3 ops/aiqu/backup_db.py --db storage/bot_data.db
```
Правило «нет снимка — нет деплоя»: если снимок не создан или не прошёл `PRAGMA quick_check`, скрипт возвращает код 1 и деплой останавливается.

**Хранение и экономия места (BK1):**
- Ротация `--keep 14` (по умолчанию) считает снимки тега по обоим суффиксам (`.db` и `.db.gz`) вместе и удаляет самые старые; ручные файлы без шаблона имени не трогает.
- Сжатие `--keep-raw 2` (по умолчанию) оставляет несжатыми только 2 новейших снимка для мгновенного отката. Все более старые снимки атомарно сжимаются в gzip (`bot_data-<время>-<tag>.db.gz`). Сжатие сопровождается проверкой целостности распаковки и `PRAGMA quick_check`.
- Сбой сжатия не прерывает деплой (код выхода 0 и предупреждение в stderr), если сам новый снимок создан и проверен.
- *Новая арифметика места:* ранее 14 несжатых снимков (при весе базы ~100 МБ) занимали ~1,4 ГБ (~14% диска). Теперь хранятся 2 несжатых и 12 сжатых копий. При коэффициенте сжатия SQLite ~4x суммарный объём:
  `2 × 100 МБ + 12 × 25 МБ = 500 МБ` вместо 1400 МБ (экономия диска почти в 3 раза).
- Свежий снимок всегда остаётся несжатым (`.db`), поэтому `prod_check.sh` находит актуальный файл через `storage/backups/*.db` без изменений.

**Восстановление из снимка:**
1. **Из быстрого несжатого снимка (`.db`):**
   ```bash
   docker compose stop unagi-bot
   cp storage/backups/bot_data-<время>-predeploy.db storage/bot_data.db
   docker compose up -d
   ```
2. **Из сжатого архива (`.db.gz`):**
   ```bash
   # Распаковать с сохранением исходного gz-файла (-k):
   gunzip -k storage/backups/bot_data-<время>-predeploy.db.gz
   # Заменить файл БД при остановленном контейнере:
   docker compose stop unagi-bot
   cp storage/backups/bot_data-<время>-predeploy.db storage/bot_data.db
   docker compose up -d
   ```

### Проверка после изменений / ребута

```bash
ssh aiqu 'sshd -T | grep -E "^(port|passwordauthentication|permitrootlogin) "; ufw status | grep 2258; fail2ban-client status sshd | grep banned; systemctl is-active telemt nginx fail2ban docker unattended-upgrades; docker ps'
```

Откат SSH: `cp /root/sshd_config.bak-20261011 /etc/ssh/sshd_config && rm /etc/ssh/sshd_config.d/00-hardening.conf && sshd -t && systemctl reload ssh`.

## Доступ из России

Без VPN сервер с российских сетей не отвечает. Вероятная причина — на IP работает MTProto-прокси
(telemt), такие IP ТСПУ блокирует целиком; второй вариант — режется SSH на зарубежные IP.
Диагностика с российской сети без VPN:

```bash
for p in 443 8443 2258; do nc -vz -G 5 89.124.104.144 $p; done; ping -c 3 89.124.104.144
```

- всё по таймауту → блокирован IP: отдельный IPv4 под управление или jump-узел в России;
- 443/8443 открыты, 2258 нет → режут SSH: обернуть SSH в TLS (stunnel с сертификатом на
  отдельном порту) и `ProxyCommand openssl s_client …` на клиенте.

## Запланировано: переезд на российский сервер

Решение владельца (2026-10-11): telemt на зарубежном IP из России не работает, поэтому
следующая миграция — на сервер в России, со всеми текущими настройками и OVPN-туннелем до
Telegram (как было на старом aiqu: `tun11`, маршруты до DC Telegram через туннель,
контейнер в `network_mode: host` наследует маршрутизацию).

Чек-лист миграции:

1. **Подготовка нового сервера:** Ubuntu 24.04, `timedatectl set-timezone Europe/Moscow`,
   харденинг из этого каталога (sshd, ufw только нужные порты, fail2ban, unattended-upgrades),
   ключи владельца, `claude-code`, новый deploy-ключ CI.
2. **OVPN-туннель до Telegram:** клиент OpenVPN как systemd-сервис, интерфейс `tun11`,
   policy routing только для подсетей Telegram DC (149.154.160.0/20, 91.108.4.0/22,
   91.108.8.0/22, 91.108.12.0/22, 91.108.16.0/22, 91.108.56.0/22, 95.161.64.0/20 и IPv6-префиксы),
   остальной трафик — напрямую. Проверка: `ip route get 149.154.167.51` → `dev tun11`;
   Bot API (`api.telegram.org`) и MTProto-сессия бота отвечают. Watchdog туннеля с алертом.
3. **Секреты (вне git):** `.env` unagi, `TELEGRAM_USER_SESSION`, `/root/.msmtprc`,
   `/etc/tldw/health.env`, `/etc/tldw/ghsub.yml`, конфиг telemt — переносить по защищённому
   каналу, права 600/640.
4. **Данные:** консистентный снимок SQLite unagi через backup API (не `cp` живого файла в WAL),
   `/var/lib/tldw/ghsub/*` (state, forks, collected) — после последнего прогона ghsub.
5. **Сервисы:** `git clone` unagi и ghsub, `docker compose up -d --build`, `ghsub/install.sh`,
   telemt по его репозиторию.
6. **Переключение:** остановить бота на старом сервере → финальный снимок БД → запуск на новом
   (один бот-токен не должен поллиться с двух машин одновременно); обновить секреты CI
   (`HOST`, `SSH_PORT`, `SSH_KEY`); DNS `cdn.orangerd.ru`.
7. **Проверка:** CI-деплой, доставка напоминания, дайджест, письмо ghsub, логи без ошибок
   сутки; затем вывод старого сервера.
8. Обновить этот файл, `docs/architecture.md` (транспорт) и `~/.ssh/config` (`Host aiqu`).
