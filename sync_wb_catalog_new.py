#!/usr/bin/env python3
"""Карточки WB (Content API) только для заказов вкладки «Новые» → wb_catalog.db + order_items.

cd /srv/stigma_fbs && python3 sync_wb_catalog_new.py
"""

from __future__ import annotations

import json
import logging
import sys

from services.orders_service import OrdersService


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        stream=sys.stderr,
    )

    def progress(ev):
        print(json.dumps(ev, ensure_ascii=False), flush=True)

    stats = OrdersService().sync_wb_catalog_for_new_orders_only(progress_cb=progress)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0 if stats.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
