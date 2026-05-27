# OREE Market Research — повний setup з нуля

Покрокова інструкція для людини, яка вперше налаштовує Linux-сервер.
Кожен крок пояснює **що** ви робите і **навіщо**. Тримайте цей файл відкритим
поруч і виконуйте по порядку.

**Що в результаті:** сервер, що сам щодня збирає дані ринку ОРЕ, зберігає їх
у базу, показує графіки в Grafana, і пише вам у Telegram про статус.

**Скільки часу:** ~1 година на все (без урахування 40-хв backfill, який іде сам).

---

## Що вам знадобиться перед стартом

- Акаунт на [console.hetzner.cloud](https://console.hetzner.cloud) (з платіжкою)
- ConnectBot на телефоні (вже є)
- SSH-ключ (інструкція нижче, якщо ще немає)
- Telegram (для бота)
- Файли проєкту: `oree_collector.py`, `schema.sql`, `docker-compose.yml`,
  `run_daily.sh`, `telegram_bot.py`, `oree-collector.service`,
  `oree-collector.timer`

---

## Частина 0. SSH-ключ (якщо ще немає)

SSH-ключ — це пара файлів: приватний (секретний, лишається у вас) і публічний
(його кладуть на сервер). Замість пароля сервер впізнає вас за ключем.

У ConnectBot: головне меню → **Pubkeys** → кнопка `+` → Name: `oree`,
Type: `ED25519` → **Generate**. Потім тапніть на ключ → **Copy public key**.
Цей текст (починається з `ssh-ed25519 ...`) знадобиться у Частині 1.

---

## Частина 1. Замовлення сервера (5 хв)

1. Зайдіть на console.hetzner.cloud → **+ New Server**.
2. **Location**: Helsinki (HEL1) — найближче до України.
3. **Image**: Ubuntu 24.04.
4. **Type**: вкладка **Arm64** → **CAX21** (4 vCPU, 8 GB RAM, 80 GB). ~€7-9/міс.
5. **SSH keys**: натисніть **Add SSH key**, вставте свій публічний ключ
   (скопійований у Частині 0), назвіть `my-phone`.
6. **Name**: `oree-research`.
7. **Create & Buy now**.

Через ~30 секунд сервер готовий. Запишіть його **IP-адресу** (напр. `95.x.x.x`).

> **Що сталося:** ви орендували віртуальний комп'ютер у дата-центрі Гельсінкі,
> який працює 24/7. Тепер до нього треба підключитись.

---

## Частина 2. Перший вхід (5 хв)

У ConnectBot: новий хост → `root@<IP>` (напр. `root@95.x.x.x`) → виберіть
ваш ключ `oree` → Connect. При першому вході підтвердіть fingerprint (yes).

Ви побачите запрошення типу `root@oree-research:~#`. Ви всередині сервера.

> **Що таке `root`:** це суперкористувач, може все. Працювати під ним постійно
> небезпечно — наступний крок створить звичайного користувача.

---

## Частина 3. Захист і підготовка сервера (10 хв)

Тут ми завантажимо скрипт `setup.sh`, який автоматично: оновить систему,
створить безпечного користувача, закриє діри в безпеці, поставить Docker і Python.

**3.1.** Спочатку завантажте файл setup.sh на сервер. Найпростіше — створити
його прямо там через текстовий редактор `nano`:

```bash
nano setup.sh
```

Відкриється редактор. Вставте весь вміст файлу `setup.sh` (у ConnectBot:
довгий тап → Paste). Потім **обов'язково** знайдіть рядок `SSH_PUBKEY=` і
замініть `ssh-ed25519 AAAA...REPLACE` на ваш справжній публічний ключ.

Збереження в nano: `Ctrl+O`, Enter, потім `Ctrl+X` для виходу.

**3.2.** Запустіть:

```bash
bash setup.sh
```

Скрипт ~3-5 хвилин щось встановлюватиме. Наприкінці напише `DONE`.

**3.3.** Тепер вийдіть і зайдіть під новим користувачем:

```bash
exit
```

У ConnectBot створіть НОВИЙ хост: `oree@<IP>` (не root!), той самий ключ.
Зайдіть. Запрошення стане `oree@oree-research:~$`.

**3.4.** Перевірте, що Docker працює без sudo:

```bash
docker ps
```

Має показати порожню таблицю (не помилку). Якщо помилка "permission denied" —
вийдіть і зайдіть ще раз (членство в групі docker застосовується при вході).

> **Що сталося:** сервер тепер безпечний (тільки вхід по ключу, firewall
> увімкнено, автооновлення безпеки), і готовий запускати контейнери.

---

## Частина 4. Завантаження файлів проєкту (10 хв)

Створіть робочу теку і покладіть туди всі файли проєкту:

```bash
mkdir -p ~/oree && cd ~/oree
```

Для кожного файлу (`oree_collector.py`, `schema.sql`, `docker-compose.yml`,
`run_daily.sh`, `telegram_bot.py`, `oree-collector.service`,
`oree-collector.timer`) зробіть те саме, що в кроці 3.1: `nano <ім'я файлу>`,
вставте вміст, збережіть.

> **Порада:** якщо файлів багато і вставляти незручно з телефону — залийте їх
> у приватний git-репозиторій (GitHub/GitLab) з комп'ютера, а на сервері зробіть
> `git clone <url> .`. Це набагато швидше. Але для першого разу nano теж ок.

Перевірте, що всі файли на місці:

```bash
ls -la
```

---

## Частина 5. Паролі і запуск бази даних + Grafana (5 хв)

**5.1.** Згенеруйте випадкові паролі у файл `.env`:

```bash
cd ~/oree
echo "PG_PASSWORD=$(openssl rand -hex 16)" > .env
echo "GF_PASSWORD=$(openssl rand -hex 12)" >> .env
cat .env
```

**ЗАПИШІТЬ ці два паролі** (наприклад у нотатки телефону) — вони знадобляться.

**5.2.** Запустіть базу даних і Grafana:

```bash
docker compose up -d
```

Перший раз завантажаться образи (~1-2 хв). Перевірте:

```bash
docker compose ps
```

Обидва сервіси (`oree_pg`, `oree_grafana`) мають бути `running`/`healthy`.

> **Що сталося:** PostgreSQL (база даних) і Grafana (графіки) тепер працюють
> у контейнерах. База автоматично створила всі таблиці зі `schema.sql`.

---

## Частина 6. Python-середовище collector'а (3 хв)

```bash
cd ~/oree
python3 -m venv .venv
source .venv/bin/activate
pip install httpx asyncpg 'python-telegram-bot>=21'
```

> **Що таке venv:** ізольоване середовище для Python-бібліотек, щоб вони не
> конфліктували з системними. `source .venv/bin/activate` "входить" у нього.

Тепер пропишіть пароль БД у двох файлах. Відкрийте `run_daily.sh`:

```bash
nano run_daily.sh
```

Знайдіть `CHANGE_ME` і замініть на `PG_PASSWORD` з вашого `.env`. Збережіть.

---

## Частина 7. Тестовий збір однієї доби (2 хв)

```bash
cd ~/oree
source .venv/bin/activate
export OREE_DSN="postgresql://oree:<ВАШ_PG_PASSWORD>@localhost:5432/oree"
python oree_collector.py --start 2026-05-02 -v
```

Успіх виглядає як рядок `[2026-05-02] persisted clearing=24 points=XXXX`.

Перевірте дані в базі:

```bash
docker exec -it oree_pg psql -U oree -d oree -c "SELECT COUNT(*) FROM dam_curves;"
```

Має показати число > 0.

> **Якщо помилка підключення:** перевірте, що пароль у `OREE_DSN` збігається
> з `.env`, і що `docker compose ps` показує postgres як healthy.

---

## Частина 8. Backfill 3 років (~40 хв, іде сам)

Щоб збір не обірвався при втраті зв'язку з телефоном, запускаємо в `tmux` —
це "відчеплювана" сесія, що живе на сервері навіть коли ви вийшли.

```bash
tmux new -s backfill
```

Усередині tmux:

```bash
cd ~/oree && source .venv/bin/activate
export OREE_DSN="postgresql://oree:<ВАШ_PG_PASSWORD>@localhost:5432/oree"
python oree_collector.py --start 2023-05-26 --end 2026-05-25
```

Тепер можете від'єднатись: натисніть `Ctrl+B`, відпустіть, потім `D`.
Збір продовжиться сам. Закривайте ConnectBot спокійно.

Щоб повернутись і перевірити прогрес:

```bash
tmux attach -t backfill
```

Коли побачите `done. ok=... fail=...` — backfill завершено.

---

## Частина 9. Щоденний автозбір (3 хв)

Тепер налаштуємо, щоб сервер сам збирав дані щодня о 14:30.

```bash
cd ~/oree
chmod +x run_daily.sh
sudo cp oree-collector.service /etc/systemd/system/
sudo cp oree-collector.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now oree-collector.timer
```

Перевірте, що таймер активний:

```bash
systemctl list-timers 'oree-collector*'
```

Побачите наступний час запуску. Протестувати негайно (не чекаючи 14:30):

```bash
sudo systemctl start oree-collector.service
journalctl -u oree-collector.service -n 30 --no-pager
```

> **Що таке systemd timer:** це як будильник для сервера. Щодня о 14:30 він
> запускає `run_daily.sh`, який збирає вчорашню добу. `Persistent=true` означає:
> якщо сервер був вимкнений о 14:30, збір відбудеться при наступному увімкненні.

---

## Частина 10. Telegram-бот моніторингу (10 хв)

**10.1. Створіть бота.** У Telegram знайдіть `@BotFather` → напишіть `/newbot`
→ дайте ім'я (напр. "OREE Monitor") і username (напр. `oree_monitor_bot`).
BotFather дасть **токен** виду `1234567890:AAxx...`. Запишіть його.

**10.2. Дізнайтесь свій chat_id.** Напишіть боту `@userinfobot` → команду
`/start`. Він покаже ваш `Id` (число). Це ваш chat_id.

**10.3. Налаштуйте сервіс бота.** Створіть systemd-юніт:

```bash
sudo nano /etc/systemd/system/oree-bot.service
```

Вставте (замініть `ТОКЕН`, `CHATID`, `PG_PASSWORD`):

```ini
[Unit]
Description=OREE Telegram monitor bot
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=oree
WorkingDirectory=/home/oree/oree
Environment=TG_TOKEN=ТОКЕН
Environment=TG_CHAT=CHATID
Environment=OREE_DSN=postgresql://oree:PG_PASSWORD@localhost:5432/oree
ExecStart=/home/oree/oree/.venv/bin/python /home/oree/oree/telegram_bot.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Збережіть. Запустіть:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now oree-bot.service
sudo systemctl status oree-bot.service --no-pager
```

**10.4. Перевірте.** У Telegram напишіть вашому боту `/status`. Має відповісти
станом збору. Команди: `/status`, `/last`, `/today`, `/gaps`, `/help`.

**10.5. (Опційно) Push-сповіщення після збору.** Відкрийте `run_daily.sh`,
розкоментуйте блок Telegram внизу, впишіть TG_TOKEN і TG_CHAT. Тоді після
кожного збору бот сам напише, успішно чи ні.

---

## Частина 11. Доступ до Grafana з телефону (5 хв)

Grafana навмисне закрита від інтернету (слухає лише localhost) — це безпечно.
Доступ через SSH-тунель.

У ConnectBot: налаштування хоста `oree@<IP>` → **Port Forwards** → `+` →
Type: `Local`, Source port: `3000`, Destination: `localhost:3000` → Save.

Перепідключіться. Тепер у браузері телефону відкрийте `http://localhost:3000`.
Логін `admin`, пароль — `GF_PASSWORD` з `.env`.

**Підключення бази в Grafana:** Connections → Data sources → Add → PostgreSQL:
- Host: `oree_pg:5432` (або `localhost:5432` якщо не спрацює — `host.docker.internal:5432`)
- Database: `oree`, User: `oree`, Password: ваш `PG_PASSWORD`
- TLS/SSL Mode: `disable`
- Save & test.

Далі створюйте панелі через Explore + SQL (приклади запитів — у README.md).

---

## Частина 12. Шпаргалка щоденних операцій

Що робити з телефону для перевірки (через ConnectBot або Telegram-бот):

| Хочу | Команда |
|------|---------|
| Стан збору | Telegram: `/status` |
| Чи зібрано вчора | Telegram: `/today` |
| Пропуски за 90 днів | Telegram: `/gaps` |
| Останній лог збору | `journalctl -u oree-collector.service -n 30 --no-pager` |
| Скільки вільно RAM/диску | `htop` (вихід `q`) і `df -h` |
| Чи працюють контейнери | `docker compose -f ~/oree/docker-compose.yml ps` |
| Перезібрати конкретний день | `cd ~/oree && source .venv/bin/activate && python oree_collector.py --start 2026-05-20 -v` |
| Перезапустити бота | `sudo systemctl restart oree-bot.service` |
| Бекап бази | `docker exec oree_pg pg_dump -U oree oree \| gzip > ~/backup_$(date +%F).sql.gz` |

---

## Частина 13. Якщо щось пішло не так

- **Не заходить по SSH після setup.sh** — переконайтесь, що вставили правильний
  публічний ключ у `SSH_PUBKEY`. Якщо заблокувались — у Hetzner Console є
  кнопка "Console" (веб-термінал) для входу як root і виправлення.
- **docker: permission denied** — вийдіть і зайдіть знову (група docker).
- **collector: connection refused** — postgres ще піднімається, зачекайте 30с;
  перевірте `docker compose ps`.
- **Бот не відповідає** — `sudo systemctl status oree-bot.service`, дивіться логи
  `journalctl -u oree-bot.service -n 50 --no-pager`. Часто — неправильний токен.
- **Диск заповнюється** — `df -h`; старі raw JSON можна архівувати або видаляти
  (дані вже в БД). Або збільшити сервер (Hetzner resize, але диск — в один бік).

---

## Наступні кроки після запуску

1. Знайти в DevTools endpoints для **indices** та **HHI** (та сама метода, що
   й `lines_data/`) — додати в collector.
2. Зареєструвати **ENTSO-E API-ключ** і додати модуль збору load + generation.
3. Додати **TTF/EUA/FX** для аналізу ціноутворення.
4. Будувати дашборди дослідження ринку в Grafana.
