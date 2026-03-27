import os
from dotenv import load_dotenv


load_dotenv()


class Config:
    BASE_DIR = os.path.abspath(os.path.dirname(__file__))
    DB_PATH = os.path.join(BASE_DIR, "instance", "stigma_fbs.db")
    LOG_PATH = os.path.join(BASE_DIR, "logs", "app.log")

    FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "dev-secret-key")

    OZON_CLIENT_ID = os.getenv("OZON_CLIENT_ID", "")
    OZON_API_KEY = os.getenv("OZON_API_KEY", "")
    OZON_BASE_URL = os.getenv("OZON_BASE_URL", "https://api-seller.ozon.ru")

    DEFAULT_PAGE_SIZE = 100
    ORDERS_CHUNK_SIZE = 100
    MAX_PAGE_SIZE = 200
