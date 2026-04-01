import os
import sqlite3

from config import Config

CREATE_WB_CATALOG_CARDS = """
CREATE TABLE IF NOT EXISTS wb_catalog_cards (
    nm_id INTEGER PRIMARY KEY NOT NULL,
    vendor_code TEXT,
    title TEXT,
    brand TEXT,
    photo_url TEXT,
    raw_json TEXT NOT NULL,
    wb_updated_at TEXT,
    synced_at TEXT NOT NULL
);
"""

CREATE_WB_CATALOG_META = """
CREATE TABLE IF NOT EXISTS wb_catalog_meta (
    key TEXT PRIMARY KEY NOT NULL,
    value TEXT NOT NULL
);
"""

CREATE_INDEX_VENDOR = (
    "CREATE INDEX IF NOT EXISTS idx_wb_catalog_vendor ON wb_catalog_cards(vendor_code);"
)


def get_catalog_db_connection() -> sqlite3.Connection:
    path = Config.WB_CATALOG_DB_PATH
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA busy_timeout = 60000;")
    return conn


def init_catalog_db() -> None:
    conn = get_catalog_db_connection()
    try:
        conn.execute(CREATE_WB_CATALOG_CARDS)
        conn.execute(CREATE_WB_CATALOG_META)
        conn.execute(CREATE_INDEX_VENDOR)
        conn.commit()
    finally:
        conn.close()
