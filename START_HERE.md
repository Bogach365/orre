# START HERE — розгортання з телефону

Усі файли проєкту в цій теці. setup.sh уже виправлений під ваш сервер
(x86/amd64) і **сам копіює ваш SSH-ключ** — редагувати нічого не треба.

## Кроки на сервері (по черзі в ConnectBot)

```bash
# 1. Завантажити всі файли (git clone — підставте свій URL репозиторію)
cd ~
git clone https://github.com/ВАШ_ЛОГІН/oree.git
cd oree

# 2. Захист сервера (~5 хв). Створить користувача oree, firewall, Docker, Python.
bash setup.sh
```

Після `DONE` — вийдіть і зайдіть заново як **oree@204.168.182.27**
(новий хост у ConnectBot), далі:

```bash
cd ~/oree   # файли треба скопіювати сюди під oree, див. нижче
```

> Файли лишаються в /root/oree після clone під root. Щоб вони були в oree:
> після setup.sh виконайте (ще під root):
> `cp -r /root/oree /home/oree/ && chown -R oree:oree /home/oree/oree`

Далі — за RUNBOOK.md, Частина 5 (паролі + docker compose up).
