"""Локальный каталог карточек WB (отдельная БД) + полная синхронизация из Content API."""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Generator, List, Optional, Set

try:
    import fcntl
except ImportError:  # Windows и прочие без flock
    fcntl = None  # type: ignore[misc, assignment]

from api_clients.wb_content_client import WbContentClient, WbContentError
from config import Config
from database.catalog_db import get_catalog_db_connection, init_catalog_db

logger = logging.getLogger(__name__)


@contextmanager
def _catalog_full_sync_lock() -> Generator[bool, None, None]:
    """
    Неблокирующий эксклюзивный lock на файл (fcntl).
    yield True — lock взят; False — уже занят другим процессом.
    """
    if not getattr(Config, "WB_CATALOG_SYNC_USE_LOCK", True):
        yield True
        return

    if fcntl is None:
        logger.warning("fcntl недоступен — lock полной синхронизации каталога WB отключён")
        yield True
        return

    path = getattr(Config, "WB_CATALOG_SYNC_LOCK_PATH", "") or ""
    if not path.strip():
        yield True
        return

    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    fh = open(path, "a+", encoding="utf-8")
    locked = False
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return

        locked = True
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n{datetime.now(timezone.utc).isoformat()}\n")
        fh.flush()
        yield True
    finally:
        if locked:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        try:
            fh.close()
        except OSError:
            pass


def _card_nm_id(card: Dict[str, Any]) -> Optional[int]:
    cid = card.get("nmID")
    if cid is None:
        cid = card.get("nmId")
    if cid is None:
        return None
    try:
        return int(cid)
    except (TypeError, ValueError):
        return None


def _best_photo_url(photos: Any) -> str:
    if not isinstance(photos, list) or not photos:
        return ""
    p0 = photos[0]
    if not isinstance(p0, dict):
        return ""
    for key in ("big", "c516x688", "hq", "square", "tm", "c246x328"):
        v = p0.get(key)
        if isinstance(v, str) and v.strip().startswith("http"):
            return v.strip()
    for v in p0.values():
        if isinstance(v, str) and v.strip().startswith("http"):
            return v.strip()
    return ""


def _wb_updated_at(card: Dict[str, Any]) -> Optional[str]:
    u = card.get("updatedAt")
    if isinstance(u, str) and u.strip():
        return u.strip()[:120]
    return None


def _card_json_for_storage(card: Dict[str, Any]) -> str:
    """Только поля, нужные для строк заказов (название, фото, размер, категория, цвет)."""
    nm = _card_nm_id(card)
    slim: Dict[str, Any] = {}
    if nm is not None:
        slim["nmID"] = nm
    for key in ("title", "imt_name", "subjectName", "brand", "vendorCode", "updatedAt"):
        v = card.get(key)
        if v is not None and v != "":
            slim[key] = v
    ch = card.get("characteristics")
    if isinstance(ch, list) and ch:
        slim["characteristics"] = [
            {k: x[k] for k in ("name", "value") if k in x}
            for x in ch
            if isinstance(x, dict)
        ]
    ph = card.get("photos")
    if isinstance(ph, list) and ph:
        slim["photos"] = ph
    sz = card.get("sizes")
    if isinstance(sz, list) and sz:
        pruned: List[Dict[str, Any]] = []
        for x in sz:
            if not isinstance(x, dict):
                continue
            d = {k: x[k] for k in ("chrtID", "chrtId", "techSize", "wbSize") if k in x}
            if d:
                pruned.append(d)
        if pruned:
            slim["sizes"] = pruned
    return json.dumps(slim, ensure_ascii=False)


def upsert_wb_card_batch(conn: Any, cards: List[Dict[str, Any]]) -> int:
    """UPSERT пачки карточек. Возвращает число принятых строк."""
    now = datetime.now(timezone.utc).isoformat()
    rows: List[tuple] = []
    for card in cards:
        if not isinstance(card, dict):
            continue
        nm = _card_nm_id(card)
        if nm is None:
            continue
        raw = _card_json_for_storage(card)
        title = (card.get("title") or "")[:4000]
        brand = (card.get("brand") or "")[:500]
        vc = (card.get("vendorCode") or "")[:500]
        photo = (_best_photo_url(card.get("photos") or []))[:2000]
        wb_up = _wb_updated_at(card)
        rows.append((nm, vc or None, title or None, brand or None, photo or None, raw, wb_up, now))

    if not rows:
        return 0

    conn.executemany(
        """
        INSERT INTO wb_catalog_cards (
            nm_id, vendor_code, title, brand, photo_url, raw_json, wb_updated_at, synced_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(nm_id) DO UPDATE SET
            vendor_code = excluded.vendor_code,
            title = excluded.title,
            brand = excluded.brand,
            photo_url = excluded.photo_url,
            raw_json = excluded.raw_json,
            wb_updated_at = excluded.wb_updated_at,
            synced_at = excluded.synced_at
        """,
        rows,
    )
    return len(rows)


def save_wb_cards_to_catalog(cards: Dict[int, Dict[str, Any]]) -> None:
    """UPSERT карточек в локальный каталог (например после добора из API)."""
    if not cards:
        return
    init_catalog_db()
    conn = get_catalog_db_connection()
    try:
        upsert_wb_card_batch(conn, list(cards.values()))
        conn.commit()
    finally:
        conn.close()


def lookup_wb_cards(nm_ids: Set[int]) -> Dict[int, Dict[str, Any]]:
    """Возвращает карточки как dict (как в Content API) по nm_id из локальной БД."""
    if not nm_ids:
        return {}
    lst = sorted({int(x) for x in nm_ids})
    conn = get_catalog_db_connection()
    out: Dict[int, Dict[str, Any]] = {}
    try:
        chunk = 400
        for i in range(0, len(lst), chunk):
            part = lst[i : i + chunk]
            ph = ",".join("?" * len(part))
            for row in conn.execute(
                f"SELECT nm_id, raw_json FROM wb_catalog_cards WHERE nm_id IN ({ph})",
                part,
            ):
                try:
                    data = json.loads(row["raw_json"])
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(data, dict):
                    out[int(row["nm_id"])] = data
    finally:
        conn.close()
    return out


def run_full_wb_catalog_sync(
    *,
    progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
    max_pages: int = 5000,
) -> Dict[str, Any]:
    """
    Полный обход каталога WB (пагинация по 100) и запись в wb_catalog.db.
    Вызывать из cron ночью или вручную: flask sync-wb-catalog / python sync_wb_catalog.py

    Параллельный второй запуск не стартует (fcntl lock на WB_CATALOG_SYNC_LOCK_PATH),
    пока первый держит lock.

    cancel_check — если возвращает True, обход останавливается (уже закоммиченные пачки остаются в БД).
    """
    pages = 0
    batches = 0
    cards_written = 0
    err: Optional[str] = None
    skipped_lock = False
    cancelled = False

    with _catalog_full_sync_lock() as got_lock:
        if not got_lock:
            logger.info(
                "Полная синхронизация каталога WB пропущена: lock занят (%s)",
                getattr(Config, "WB_CATALOG_SYNC_LOCK_PATH", ""),
            )
            skipped_lock = True
            if progress_cb:
                progress_cb({"step": "wb_full_catalog_skip_lock"})
        else:
            if progress_cb:
                progress_cb({"step": "wb_full_catalog_start"})
            init_catalog_db()
            client = WbContentClient()
            conn = get_catalog_db_connection()
            try:
                try:
                    for page_num, cards, _cinfo in client.iter_catalog_pages(
                        max_pages=max_pages,
                        cancel_check=cancel_check,
                    ):
                        pages = page_num
                        if cards:
                            n = upsert_wb_card_batch(conn, cards)
                            conn.commit()
                            batches += 1
                            cards_written += n
                        if progress_cb:
                            progress_cb(
                                {
                                    "step": "wb_full_catalog_page",
                                    "page": page_num,
                                    "batch": len(cards),
                                    "cards_written": cards_written,
                                }
                            )

                    cancelled = bool(cancel_check and cancel_check())
                    if cancelled:
                        logger.info(
                            "Полная синхронизация каталога WB остановлена по отмене "
                            "(страниц обработано: %s, карточек upsert: %s)",
                            pages,
                            cards_written,
                        )

                    if progress_cb and not cancelled:
                        progress_cb(
                            {
                                "step": "wb_full_catalog_pages_done",
                                "pages": pages,
                                "batches_committed": batches,
                                "cards_upserted": cards_written,
                            }
                        )

                    if not cancelled:
                        now_iso = datetime.now(timezone.utc).isoformat()
                        conn.execute(
                            """
                            INSERT INTO wb_catalog_meta (key, value) VALUES (?, ?)
                            ON CONFLICT(key) DO UPDATE SET value = excluded.value
                            """,
                            ("last_full_sync_at", now_iso),
                        )
                        conn.execute(
                            """
                            INSERT INTO wb_catalog_meta (key, value) VALUES (?, ?)
                            ON CONFLICT(key) DO UPDATE SET value = excluded.value
                            """,
                            ("last_full_sync_pages", str(pages)),
                        )
                        conn.execute(
                            """
                            INSERT INTO wb_catalog_meta (key, value) VALUES (?, ?)
                            ON CONFLICT(key) DO UPDATE SET value = excluded.value
                            """,
                            ("last_full_sync_cards_upserted", str(cards_written)),
                        )
                        conn.commit()
                except WbContentError as exc:
                    err = str(exc)
                    logger.warning("Полная синхронизация каталога WB прервана: %s", exc)
                    conn.rollback()
            finally:
                conn.close()

    if skipped_lock:
        return {
            "ok": True,
            "skipped": True,
            "cancelled": False,
            "skip_reason": "lock_already_held",
            "error": None,
            "pages": 0,
            "batches_committed": 0,
            "cards_upserted": 0,
        }

    return {
        "ok": err is None and not cancelled,
        "skipped": False,
        "cancelled": cancelled,
        "error": err,
        "pages": pages,
        "batches_committed": batches,
        "cards_upserted": cards_written,
    }
