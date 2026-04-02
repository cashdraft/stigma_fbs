import os
import sqlite3

from config import Config
from database.models import (
    CREATE_INDEXES,
    CREATE_ORDER_ITEMS_TABLE,
    CREATE_ORDERS_TABLE,
    CREATE_SHIPMENTS_TABLE,
)


def get_db_connection():
    os.makedirs(os.path.dirname(Config.DB_PATH), exist_ok=True)
    timeout = int(Config.SQLITE_CONNECT_TIMEOUT_SEC)
    busy_ms = int(Config.SQLITE_BUSY_TIMEOUT_MS)
    conn = sqlite3.connect(Config.DB_PATH, timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute(f"PRAGMA busy_timeout = {busy_ms};")
    # WAL is much safer for concurrent read/write web access.
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    return conn


def init_db():
    conn = get_db_connection()
    try:
        conn.execute(CREATE_ORDERS_TABLE)
        conn.execute(CREATE_ORDER_ITEMS_TABLE)
        conn.execute(CREATE_SHIPMENTS_TABLE)
        order_columns = {col["name"] for col in conn.execute("PRAGMA table_info(orders)").fetchall()}
        if "shipment_id" not in order_columns:
            conn.execute(
                "ALTER TABLE orders ADD COLUMN shipment_id INTEGER "
                "REFERENCES shipments(id) ON DELETE SET NULL"
            )
        order_columns = {col["name"] for col in conn.execute("PRAGMA table_info(orders)").fetchall()}
        if "label_pdf_path" not in order_columns:
            conn.execute("ALTER TABLE orders ADD COLUMN label_pdf_path TEXT")
        order_columns = {col["name"] for col in conn.execute("PRAGMA table_info(orders)").fetchall()}
        if "wb_in_new_feed" not in order_columns:
            # 1 = id есть в GET /api/v3/orders/new (как вкладка «Новые» в кабинете WB)
            conn.execute("ALTER TABLE orders ADD COLUMN wb_in_new_feed INTEGER DEFAULT 0")
        order_columns = {col["name"] for col in conn.execute("PRAGMA table_info(orders)").fetchall()}
        if "wb_label_path" not in order_columns:
            conn.execute("ALTER TABLE orders ADD COLUMN wb_label_path TEXT")
        columns = conn.execute("PRAGMA table_info(order_items)").fetchall()
        column_names = {col["name"] for col in columns}
        if "photo_url" not in column_names:
            conn.execute("ALTER TABLE order_items ADD COLUMN photo_url TEXT")
        for col in (
            "category_leaf",
            "color",
            "barcode",
            "manufacturer_size",
        ):
            if col not in column_names:
                conn.execute(f"ALTER TABLE order_items ADD COLUMN {col} TEXT")
        for statement in CREATE_INDEXES:
            conn.execute(statement)
        ship_columns = {col["name"] for col in conn.execute("PRAGMA table_info(shipments)").fetchall()}
        if "wb_supply_id" not in ship_columns:
            conn.execute("ALTER TABLE shipments ADD COLUMN wb_supply_id TEXT")
        conn.commit()
    finally:
        conn.close()
