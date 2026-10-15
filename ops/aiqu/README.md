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
