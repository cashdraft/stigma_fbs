#!/usr/bin/env python3
"""Полная синхронизация каталога WB в отдельную БД. Пример cron (3:15 ночи):
0 3 * * * cd /srv/stigma_fbs && /usr/bin/python3 sync_wb_catalog.py >> /var/log/wb_catalog_sync.log 2>&1
"""

from __future__ import annotations

import json
import logging
import sys

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
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0 if stats.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
