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
    # PDF этикетки сверстаны под страницу 580×400 pt (как print_* шаблон Ozon).
    _label_w_default = str(round(580 * 25.4 / 72, 4))
    _label_h_default = str(round(400 * 25.4 / 72, 4))
    LABEL_WIDTH_MM = float(os.getenv("LABEL_WIDTH_MM", _label_w_default))
    LABEL_HEIGHT_MM = float(os.getenv("LABEL_HEIGHT_MM", _label_h_default))

    DEFAULT_PAGE_SIZE = 100
    ORDERS_CHUNK_SIZE = 100
    MAX_PAGE_SIZE = 200
