import os
from dotenv import load_dotenv


load_dotenv()


class Config:
    BASE_DIR = os.path.abspath(os.path.dirname(__file__))
    DB_PATH = os.path.join(BASE_DIR, "instance", "stigma_fbs.db")
    LOG_PATH = os.path.join(BASE_DIR, "logs", "app.log")
    LABELS_DIR = os.path.join(BASE_DIR, "instance", "labels")

    FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "dev-secret-key")

    OZON_CLIENT_ID = os.getenv("OZON_CLIENT_ID", "")
    OZON_API_KEY = os.getenv("OZON_API_KEY", "")
    OZON_BASE_URL = os.getenv("OZON_BASE_URL", "https://api-seller.ozon.ru")

    # Токен категории «Маркетплейс» в личном кабинете WB (см. dev.wildberries.ru — FBS orders).
    # Для названий, фото и размеров по nmId/chrtId при синхронизации нужна ещё категория «Контент»
    # (тот же JWT в WB_API_TOKEN или отдельный токен — как настроено в кабинете).
    WB_API_TOKEN = os.getenv("WB_API_TOKEN", "").strip()
    WB_MARKETPLACE_BASE_URL = os.getenv(
        "WB_MARKETPLACE_BASE_URL",
        "https://marketplace-api.wildberries.ru",
    ).rstrip("/")
    WB_CONTENT_BASE_URL = os.getenv(
        "WB_CONTENT_BASE_URL",
        "https://content-api.wildberries.ru",
    ).rstrip("/")
    # Запрос карточек к Content API при sync WB (название, фото, размер). Отключить: WB_FETCH_CONTENT_CARDS=0.
    WB_FETCH_CONTENT_CARDS = os.getenv("WB_FETCH_CONTENT_CARDS", "1").lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    # Лимит Content API ~100 запросов/мин; меньше значение — риск 429 и «подвисаний».
    WB_CONTENT_MIN_INTERVAL = float(os.getenv("WB_CONTENT_MIN_INTERVAL", "0.65"))
    # При числе уникальных nmId >= порога тянем каталог пачками по 100 (быстрее, чем textSearch на каждый nm).
    # Ниже порога — только textSearch по nmId (удобно при небольшой воронке заказов).
    WB_CONTENT_PAGINATE_MIN_NM = int(os.getenv("WB_CONTENT_PAGINATE_MIN_NM", "15"))

    # Отдельная SQLite с карточками WB (полная выгрузка ночью / вручную).
    WB_CATALOG_DB_PATH = os.getenv(
        "WB_CATALOG_DB_PATH",
        os.path.join(BASE_DIR, "instance", "wb_catalog.db"),
    )
    # При синхронизации заказов сначала читать карточки из WB_CATALOG_DB_PATH.
    WB_USE_CATALOG_FOR_CARDS = os.getenv("WB_USE_CATALOG_FOR_CARDS", "1").lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    # Для nmId, которых нет в локальном каталоге, дернуть Content API (как раньше).
    WB_CATALOG_FILL_GAPS_FROM_API = os.getenv("WB_CATALOG_FILL_GAPS_FROM_API", "1").lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    # Сохранять в wb_catalog.db карточки, полученные при доборе из API (накапливаем каталог между ночными полными прогонами).
    WB_CATALOG_SAVE_API_GAPS = os.getenv("WB_CATALOG_SAVE_API_GAPS", "1").lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    # Эксклюзивный lock на полную синхронизацию каталога (cron + ручной запуск не пересекаются).
    WB_CATALOG_SYNC_USE_LOCK = os.getenv("WB_CATALOG_SYNC_USE_LOCK", "1").lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    WB_CATALOG_SYNC_LOCK_PATH = os.getenv(
        "WB_CATALOG_SYNC_LOCK_PATH",
        os.path.join(BASE_DIR, "instance", "wb_catalog_sync.lock"),
    )
    # Если WB отвечает 401, попробуйте WB_USE_BEARER_PREFIX=1 (заголовок Authorization: Bearer <токен>).
    WB_USE_BEARER_PREFIX = os.getenv("WB_USE_BEARER_PREFIX", "").lower() in ("1", "true", "yes")
    # Генерация PDF этикеток (ШК) после сохранения заказов — не в одной длинной транзакции с INSERT (меньше database locked).
    WB_SYNC_BUILD_LABEL_PDF = os.getenv("WB_SYNC_BUILD_LABEL_PDF", "1").lower() not in (
        "0",
        "false",
        "no",
        "off",
    )

    LABEL_FONT_PATH = os.getenv(
        "LABEL_FONT_PATH",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    )
    LABEL_BRAND_LINE = os.getenv("LABEL_BRAND_LINE", "STIGMA")
    LABEL_SELLER = os.getenv("LABEL_SELLER", "ИП Главацкая С.П.")
    LABEL_COMPOSITION = os.getenv("LABEL_COMPOSITION", "100% Хлопок")
    LABEL_COUNTRY = os.getenv("LABEL_COUNTRY", "Россия")
    # Эталонная страница PDF (580×400 pt): подложка 1:1, подменяются только поля заказа.
    LABEL_TEMPLATE_PDF = os.getenv("LABEL_TEMPLATE_PDF", "").strip()
    LABEL_TEMPLATE_PAGE = int(os.getenv("LABEL_TEMPLATE_PAGE", "0"))
    # Размер финальной локальной этикетки берётся со страницы этикетки Ozon (как в «Ленте заказов»).
    # Если Ozon этикетку получить нельзя — сохраняется исходный формат шаблона 580×400 pt.
    # Поля ниже оставлены для совместимости со старыми .env (сейчас генератор их не использует).
    _label_w_default = "40"
    _label_h_default = "30"
    LABEL_WIDTH_MM = float(os.getenv("LABEL_WIDTH_MM", _label_w_default))
    LABEL_HEIGHT_MM = float(os.getenv("LABEL_HEIGHT_MM", _label_h_default))

    # Ожидание блокировки SQLite (параллельно идёт синхронизация WB + запросы UI).
    SQLITE_CONNECT_TIMEOUT_SEC = int(os.getenv("SQLITE_CONNECT_TIMEOUT_SEC", "120"))
    SQLITE_BUSY_TIMEOUT_MS = int(os.getenv("SQLITE_BUSY_TIMEOUT_MS", "120000"))

    DEFAULT_PAGE_SIZE = 500
    ORDERS_CHUNK_SIZE = 500
    MAX_PAGE_SIZE = 1000
