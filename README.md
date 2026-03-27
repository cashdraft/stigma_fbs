# Stigma FBS

Веб-приложение на Flask для просмотра и фильтрации FBS-заказов Ozon. Данные кэшируются в SQLite, синхронизация с Ozon — вручную кнопкой «Обновить заказы».

## Требования

- Python 3.11+
- Linux (развёртывание рассчитано на `/srv/stigma_fbs`)

## Установка

```bash
cd /srv/stigma_fbs
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Заполните .env: OZON_CLIENT_ID, OZON_API_KEY, FLASK_SECRET_KEY
```

## Запуск

```bash
source .venv/bin/activate
python3 app.py
```

Откройте в браузере `http://127.0.0.1:5000`. Проверка: `GET /health` → `{"status":"ok"}`.

## Структура

- `api_clients/` — клиент Ozon API
- `services/` — синхронизация и выборка заказов
- `database/` — SQLite, модели
- `templates/`, `static/` — интерфейс

Файлы `.env`, база `instance/*.db`, логи `logs/` и кэш категорий в `instance/` в репозиторий не коммитятся (см. `.gitignore`).

## Push на GitHub (без токена в URL и в логе)

В `.env` должен быть `GITHUB_TOKEN` (classic PAT с правом `repo`). Пуш идёт через `GIT_ASKPASS`, вывод при необходимости чистится от похожих на токены строк.

```bash
cd /srv/stigma_fbs
python3 scripts/push_github.py
# другая ветка: python3 scripts/push_github.py develop
```

Не используйте `git push https://x-access-token:...` — так токен часто попадает в вывод команды.
