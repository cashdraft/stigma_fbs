#!/usr/bin/env python3
"""Полная синхронизация каталога WB в отдельную БД + обновление order_items из каталога.

Запуск из корня проекта (нужен .env и venv с зависимостями):
  cd /srv/stigma_fbs && .venv/bin/python3 sync_wb_catalog.py

Автозапуск: systemd timer (см. deploy/wb-catalog-sync.timer) или cron:
  15 3 * * * cd /srv/stigma_fbs && .venv/bin/python3 sync_wb_catalog.py >> logs/wb_catalog_cron.log 2>&1

Параллельный второй процесс не запускает выгрузку (lock instance/wb_catalog_sync.lock).
Отключить lock: WB_CATALOG_SYNC_USE_LOCK=0 в .env
"""

from __future__ import annotations

import json
import logging
import sys

from services.orders_service import OrdersService
from services.wb_catalog_service import run_full_wb_catalog_sync


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        stream=sys.stderr,
    )

    def progress(ev):
        print(json.dumps(ev, ensure_ascii=False), flush=True)

    stats = run_full_wb_catalog_sync(progress_cb=progress)
    if not stats.get("skipped"):
        try:
            en = OrdersService().enrich_wb_order_items_from_local_catalog()
            stats["order_items_enriched"] = int(en.get("items_updated") or 0)
        except Exception:
            logging.exception("enrich_wb_order_items_from_local_catalog после каталога")
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    if stats.get("skipped"):
        return 0
    return 0 if stats.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
