CREATE_ORDERS_TABLE = """
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    marketplace TEXT NOT NULL,
    posting_number TEXT NOT NULL,
    order_number TEXT,
    status TEXT,
    substatus TEXT,
    created_at TEXT,
    shipment_date TEXT,
    delivery_method TEXT,
    warehouse_name TEXT,
    customer_name TEXT,
    customer_phone TEXT,
    total_price REAL,
    currency_code TEXT,
    is_fbs INTEGER DEFAULT 1,
    raw_json TEXT,
    updated_at TEXT NOT NULL
);
"""

CREATE_ORDER_ITEMS_TABLE = """
CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    sku TEXT,
    offer_id TEXT,
    product_name TEXT,
    quantity INTEGER DEFAULT 0,
    price REAL DEFAULT 0,
    photo_url TEXT,
    category_leaf TEXT,
    color TEXT,
    barcode TEXT,
    manufacturer_size TEXT,
    FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE
);
"""

CREATE_SHIPMENTS_TABLE = """
CREATE TABLE IF NOT EXISTS shipments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    marketplace TEXT NOT NULL DEFAULT 'ozon',
    created_at TEXT NOT NULL
);
"""

CREATE_INDEXES = [
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_marketplace_posting ON orders (marketplace, posting_number);",
    "CREATE INDEX IF NOT EXISTS idx_orders_status ON orders (status);",
    "CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders (created_at);",
    "CREATE INDEX IF NOT EXISTS idx_orders_shipment_id ON orders (shipment_id);",
    "CREATE INDEX IF NOT EXISTS idx_order_items_order_id ON order_items (order_id);",
    "CREATE INDEX IF NOT EXISTS idx_order_items_sku ON order_items (sku);",
]
