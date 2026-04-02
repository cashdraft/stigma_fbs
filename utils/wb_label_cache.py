from __future__ import annotations

import os
import re
from typing import Optional

from config import Config


def wb_label_cache_path(order_id: int) -> str:
    safe = re.sub(r"[^\d]+", "_", str(order_id)).strip("_") or "0"
    return os.path.join(Config.BASE_DIR, "instance", "wb_labels_cache", f"{safe}.png")


def load_cached_wb_label(order_id: int) -> Optional[bytes]:
    path = wb_label_cache_path(order_id)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as f:
            data = f.read()
        return data or None
    except OSError:
        return None


def save_cached_wb_label(order_id: int, png: bytes) -> str:
    path = wb_label_cache_path(order_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(png)
    return path
