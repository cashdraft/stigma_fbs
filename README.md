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
