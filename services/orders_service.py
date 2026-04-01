import json
import logging
import os
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # type: ignore[misc, assignment]

from api_clients.ozon_client import OzonApiError, OzonClient
from api_clients.wb_client import WbApiError, WbClient
from api_clients.wb_content_client import WbContentClient, WbContentError
from services.wb_catalog_service import lookup_wb_cards, save_wb_cards_to_catalog
from config import Config
from database.db import get_db_connection
from utils.ozon_product_meta import (
    first_attribute_value,
    resolve_color_and_mfr_size_attr_ids,
)
from utils.label_pdf import build_label_pages_for_order_items, write_order_label_pdf
from utils.ozon_label_cache import (
    ensure_ozon_label_pdf_for_posting,
    first_page_size_pt,
    load_cached_ozon_label,
)
from utils.ozon_tariff import parse_shipment_tariff_from_raw

EXCLUDED_STATUSES = {"delivered", "cancelled"}
STATUS_LABELS = {
    "awaiting_packaging": "Ожидает сборки",
    "awaiting_deliver": "Ожидает отгрузки",
    "delivering": "Доставляется",
}

# Подписи как в кабинете WB (Маркетплейс FBS): Новые / На сборке / В доставке.
WB_STATUS_LABELS = {
    "new": "Новые",
    "confirm": "На сборке",
    "complete": "В доставке",
    "wbgo": "В доставке",
    "cancel": "Отменён продавцом",
    "cancel_carrier": "Отменён перевозчиком",
}

# supplierStatus из /orders/status — только активная воронка; остальное не пишем и вычищаем из БД.
WB_PORTAL_SUPPLIER_STATUSES = frozenset({"new", "confirm", "complete", "wbgo"})

SHIPMENT_NAME_PREFIX = "OZON_"

# Один раз в лог — если шаблон этикетки недоступен при массовой синхронизации
_label_template_unavailable_logged = False
def _wb_fbs_list_date_range_unix(since: Optional[str], to: Optional[str]) -> Tuple[int, int]:
    """
    Диапазон для GET /api/v3/orders: не более 30 суток по длительности, конец не в будущем.
    Границы по календарю в Europe/Moscow (как у продавцов РФ); иначе WB часто отвечает 400.
    """
    max_span_sec = int(timedelta(days=30).total_seconds())

    if ZoneInfo is not None:
        msk = ZoneInfo("Europe/Moscow")
        now_local = datetime.now(msk)
        end_cap = int(now_local.timestamp())
        default_start_day = now_local.date() - timedelta(days=29)

        def _ts(day: date, *, end_of_day: bool) -> int:
            t = time(23, 59, 59) if end_of_day else time(0, 0, 0)
            return int(datetime.combine(day, t, tzinfo=msk).timestamp())
    else:
        now_utc = datetime.now(timezone.utc)
        end_cap = int(now_utc.timestamp())
        default_start_day = now_utc.date() - timedelta(days=29)

        def _ts(day: date, *, end_of_day: bool) -> int:
            t = time(23, 59, 59) if end_of_day else time(0, 0, 0)
            return int(datetime.combine(day, t, tzinfo=timezone.utc).timestamp())

    if to and len(str(to).strip()) == 10:
        try:
            d_end = date.fromisoformat(str(to).strip())
            end = min(_ts(d_end, end_of_day=True), end_cap)
        except ValueError:
            end = end_cap
    else:
        end = end_cap

    if since and len(str(since).strip()) == 10:
        try:
            d_start = date.fromisoformat(str(since).strip())
            start = _ts(d_start, end_of_day=False)
        except ValueError:
            start = _ts(default_start_day, end_of_day=False)
    else:
        start = _ts(default_start_day, end_of_day=False)

    if start >= end:
        start = end - 3600
    if end - start > max_span_sec:
        start = end - max_span_sec
    return start, end


RU_MONTHS_SHORT = {
    1: "янв",
    2: "фев",
    3: "мар",
    4: "апр",
    5: "май",
    6: "июн",
    7: "июл",
    8: "авг",
    9: "сен",
    10: "окт",
    11: "ноя",
    12: "дек",
}


def _shipment_date_prefix(d: Optional[date] = None) -> str:
    day = d or datetime.now().date()
    return f"{SHIPMENT_NAME_PREFIX}{day.strftime('%d.%m.%y')}/"


def _format_ru_short_datetime(value: Optional[str]) -> str:
    if not value:
        return "-"
    raw = str(value).strip()
    if not raw:
        return "-"
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = datetime.strptime(raw[:16].replace("T", " "), "%Y-%m-%d %H:%M")
        except ValueError:
            return raw[:16].replace("T", " ")
    month = RU_MONTHS_SHORT.get(dt.month, "")
    return f"{dt.day:02d} {month} {dt:%H:%M}"


class OrdersService:
    def __init__(self) -> None:
        self.client = OzonClient()
        self.logger = logging.getLogger(__name__)

    @staticmethod
    def _build_products_expanded_by_unit(
        items: List[Dict[str, Any]],
        max_units_per_order: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        Оzon "Разделить заказ" по смыслу должен разбить позиции на единичные отправления.
        Мы не храним exemplarsIds в нашей БД, поэтому делаем разбиение за счет того,
        что в запрос отправляем товары как список элементов с quantity=1.

        Пример: если в заказе строка qty=3 по одному sku, то в payload будет 3 раза:
          {product_id: sku, quantity: 1}
        """
        products: List[Dict[str, Any]] = []
        n_units = 0
        for it in items:
            sku = str(it.get("sku") or "").strip()
            if not sku:
                continue
            try:
                sku_int = int(sku)
            except ValueError:
                continue
            qty = int(it.get("quantity") or 0)
            if qty <= 0:
                continue

            for _ in range(qty):
                n_units += 1
                if n_units > max_units_per_order:
                    raise ValueError(
                        f"Слишком много единиц для разделения на Ozon: sku={sku_int}, limit={max_units_per_order}"
                    )
                products.append({"product_id": sku_int, "quantity": 1})
        return products

    @staticmethod
    def _normalize_posting(posting: Dict[str, Any]) -> Dict[str, Any]:
        analytics = posting.get("analytics_data", {}) or {}
        financial = posting.get("financial_data", {}) or {}
        products = posting.get("products", []) or []

        customer = posting.get("customer", {}) or {}
        delivery_method = posting.get("delivery_method", {}) or {}

        total_price = 0.0
        currency_code = "RUB"
        if products:
            for p in products:
                quantity = int(p.get("quantity", 0) or 0)
                price = float(p.get("price", 0) or 0)
                total_price += price * quantity
            currency_code = products[0].get("currency_code", "RUB")
        elif isinstance(financial.get("products"), list) and financial.get("products"):
            for p in financial.get("products"):
                quantity = int(p.get("quantity", 0) or 0)
                price = float(p.get("price", 0) or 0)
                total_price += price * quantity

        items = []
        for p in products:
            items.append(
                {
                    "sku": str(p.get("sku", "")),
                    "offer_id": p.get("offer_id", ""),
                    "name": p.get("name", ""),
                    "quantity": int(p.get("quantity", 0) or 0),
                    "price": float(p.get("price", 0) or 0),
                }
            )

        return {
            "marketplace": "ozon",
            "posting_number": posting.get("posting_number", ""),
            "order_number": posting.get("order_id", ""),
            "status": posting.get("status", ""),
            "substatus": posting.get("substatus", ""),
            "created_at": posting.get("in_process_at") or posting.get("created_at"),
            "shipment_date": posting.get("shipment_date"),
            "delivery_method": delivery_method.get("name", ""),
            "warehouse_name": delivery_method.get("warehouse")
            or analytics.get("warehouse")
            or posting.get("warehouse_id", ""),
            "customer_name": customer.get("name", ""),
            "customer_phone": customer.get("phone", ""),
            "total_price": float(total_price or 0),
            "currency_code": currency_code,
            "is_fbs": 1,
            "items": items,
            "raw_json": json.dumps(posting, ensure_ascii=False),
        }

    @staticmethod
    def _wb_price_to_rub(raw: Dict[str, Any]) -> float:
        val = raw.get("finalPrice")
        if val is None:
            val = raw.get("price")
        try:
            x = float(val or 0)
        except (TypeError, ValueError):
            return 0.0
        return x / 100.0

    @staticmethod
    def _wb_currency_code(raw: Dict[str, Any]) -> str:
        c = raw.get("convertedCurrencyCode")
        if c is None:
            c = raw.get("currencyCode")
        if c == 643:
            return "RUB"
        return str(c or "RUB")

    @staticmethod
    def _wb_shipment_date_iso(raw: Dict[str, Any]) -> Optional[str]:
        for key in ("sellerDate", "ddate"):
            s = raw.get(key)
            if not s or not isinstance(s, str):
                continue
            t = s.strip()
            try:
                d = datetime.strptime(t, "%d.%m.%Y")
                return d.isoformat()
            except ValueError:
                continue
        return None

    def _normalize_wb_order(
        self,
        raw: Dict[str, Any],
        supplier_status: str,
        wb_status: str,
    ) -> Dict[str, Any]:
        oid = raw.get("id")
        posting_number = str(oid) if oid is not None else ""
        skus = list(raw.get("skus") or [])
        barcode = str(skus[0]) if skus else ""
        article = str(raw.get("article") or "")
        nm = raw.get("nmId")
        chrt = raw.get("chrtId")
        sku_main = str(nm if nm is not None else chrt or "")

        offices = raw.get("offices") or []
        wh_parts: List[str] = []
        if isinstance(offices, list) and offices:
            wh_parts.extend(str(x) for x in offices if x)
        wid = raw.get("warehouseId")
        if wid is not None and not wh_parts:
            wh_parts.append(str(wid))

        price_one = self._wb_price_to_rub(raw)
        items = [
            {
                "sku": sku_main,
                "offer_id": article,
                "name": article or sku_main or posting_number,
                "quantity": 1,
                "price": price_one,
                "photo_url": "",
                "category_leaf": "",
                "color": str(raw.get("colorCode") or ""),
                "barcode": barcode,
                "manufacturer_size": "",
            }
        ]

        addr = raw.get("address") or {}
        full_addr = ""
        if isinstance(addr, dict):
            full_addr = str(addr.get("fullAddress") or "")

        return {
            "marketplace": "wb",
            "posting_number": posting_number,
            "order_number": str(raw.get("orderUid") or raw.get("rid") or posting_number),
            "status": supplier_status or "new",
            "substatus": wb_status or "",
            "created_at": raw.get("createdAt") or "",
            "shipment_date": self._wb_shipment_date_iso(raw) or raw.get("createdAt"),
            "delivery_method": str(raw.get("deliveryType") or "fbs"),
            "warehouse_name": ", ".join(wh_parts) if wh_parts else "",
            "customer_name": full_addr[:200] if full_addr else "",
            "customer_phone": "",
            "total_price": float(price_one),
            "currency_code": self._wb_currency_code(raw),
            "is_fbs": 1,
            "items": items,
            "raw_json": json.dumps(raw, ensure_ascii=False),
        }

    @staticmethod
    def _wb_color_from_wb_card(card: Dict[str, Any]) -> str:
        """Текст цвета из карточки Content API (характеристика «Цвет» в кабинете WB)."""
        for ch in card.get("characteristics") or []:
            if not isinstance(ch, dict):
                continue
            name_raw = ch.get("name")
            if not isinstance(name_raw, str):
                continue
            name = name_raw.strip().lower()
            if "цвет" not in name and "color" not in name:
                continue
            if "упаковк" in name or "фурнитур" in name:
                continue
            vals = ch.get("value")
            if isinstance(vals, list):
                parts = [str(v).strip() for v in vals if v is not None and str(v).strip()]
                if parts:
                    return ", ".join(parts)[:500]
            if vals is not None:
                s = str(vals).strip()
                if s:
                    return s[:500]
        return ""

    @staticmethod
    def _wb_apply_content_card_to_item(
        item: Dict[str, Any],
        raw: Dict[str, Any],
        card: Dict[str, Any],
    ) -> None:
        """Дополняет позицию заказа WB данными карточки Content API (title, photos, sizes[].chrtID, цвет)."""
        title = (card.get("title") or card.get("imt_name") or "").strip()
        if title:
            item["name"] = title

        subj = card.get("subjectName")
        if isinstance(subj, str) and subj.strip():
            item["category_leaf"] = subj.strip()[:500]

        color_txt = OrdersService._wb_color_from_wb_card(card)
        if color_txt:
            item["color"] = color_txt

        photos = card.get("photos") or []
        if isinstance(photos, list) and photos:
            p0 = photos[0]
            if isinstance(p0, dict):
                url = ""
                for key in ("big", "c516x688", "hq", "square", "tm", "c246x328"):
                    v = p0.get(key)
                    if isinstance(v, str) and v.strip().startswith("http"):
                        url = v.strip()
                        break
                if not url:
                    for v in p0.values():
                        if isinstance(v, str) and v.strip().startswith("http"):
                            url = v.strip()
                            break
                if url:
                    item["photo_url"] = url

        chrt_raw = raw.get("chrtId")
        if chrt_raw is None:
            return
        try:
            chrt_target = int(chrt_raw)
        except (TypeError, ValueError):
            return
        for sz in card.get("sizes") or []:
            if not isinstance(sz, dict):
                continue
            cid = sz.get("chrtID")
            if cid is None:
                cid = sz.get("chrtId")
            if cid is None:
                continue
            try:
                if int(cid) != chrt_target:
                    continue
            except (TypeError, ValueError):
                continue
            wb_sz = sz.get("wbSize")
            tech = sz.get("techSize")

            def _wb_size_token(v: Any) -> str:
                if isinstance(v, str) and v.strip() and v.strip() != "0":
                    return v.strip()
                if v not in (None, "", 0):
                    s = str(v).strip()
                    if s and s != "0":
                        return s
                return ""

            # techSize — размер продавца (S, M, L, 3XL в ЛК); wbSize — сетка WB для покупателя (42-44 и т.п.)
            tech_t = _wb_size_token(tech)
            wb_t = _wb_size_token(wb_sz)
            size_label = tech_t or wb_t
            if size_label:
                item["manufacturer_size"] = size_label
            break

    def sync_from_ozon(
        self,
        status: Optional[str] = None,
        statuses: Optional[List[str]] = None,
        since: Optional[str] = None,
        to: Optional[str] = None,
        limit: int = 100,
        max_records: int = 5000,
    ) -> Dict[str, int]:
        # Ozon FBS имеет несколько последовательных статусов.
        # Чтобы локальные статусы не "застревали" (ожидает отгрузки -> доставляется),
        # синхронизируем сразу все 3 нужных статуса и затем очищаем базу от posting,
        # которых больше нет ни в одном из трёх статусов Ozon.
        main_statuses = ["awaiting_packaging", "awaiting_deliver", "delivering"]
        statuses_to_fetch: List[str] = []
        if statuses:
            for s in statuses:
                if s and s not in statuses_to_fetch:
                    statuses_to_fetch.append(s)
        elif status in (None, "all"):
            statuses_to_fetch = main_statuses.copy()
        elif status in main_statuses:
            statuses_to_fetch = [status]
        else:
            statuses_to_fetch = [status] if status else main_statuses.copy()

        now = datetime.now(timezone.utc)
        since_default = now - timedelta(days=30)
        to_default = now + timedelta(days=1)
        effective_since_iso = self.client._to_iso8601(since, since_default)
        effective_to_iso = self.client._to_iso8601(to, to_default, end_of_day=True)

        postings: List[Dict[str, Any]] = []
        hit_cap = False

        batch_size_base = max(1, min(limit, 1000))
        for st in statuses_to_fetch:
            offset = 0
            while True:
                remaining = max_records - len(postings)
                if remaining <= 0:
                    hit_cap = True
                    postings = postings[:max_records]
                    break

                batch_size = min(batch_size_base, remaining)
                page = self.client.get_fbs_postings(
                    status=st,
                    since=since,
                    to=to,
                    limit=batch_size,
                    offset=offset,
                )
                if not page:
                    break

                filtered_page = [p for p in page if p.get("status") not in EXCLUDED_STATUSES]
                postings.extend(filtered_page)

                if len(page) < batch_size:
                    break

                offset += batch_size

            if hit_cap:
                break

        sku_values: List[int] = []
        for posting in postings:
            for product in posting.get("products", []) or []:
                sku = product.get("sku")
                if sku is None:
                    continue
                try:
                    sku_values.append(int(sku))
                except (TypeError, ValueError):
                    continue

        image_map: Dict[str, str] = {}
        unique_skus = sorted(set(sku_values))
        chunk_size = 100
        for i in range(0, len(unique_skus), chunk_size):
            chunk = unique_skus[i : i + chunk_size]
            image_map.update(self.client.get_product_images_by_sku(chunk))

        type_names = self.client.get_type_id_to_leaf_name()
        details_by_sku: Dict[str, Dict[str, Any]] = {}
        for i in range(0, len(unique_skus), chunk_size):
            chunk = unique_skus[i : i + chunk_size]
            details_by_sku.update(self.client.get_product_attributes_by_sku(chunk))

        attr_id_cache: Dict[Tuple[int, int], Tuple[Optional[int], Optional[int]]] = {}

        def enrich_line_item(item: Dict[str, Any]) -> None:
            sku = item.get("sku") or ""
            row = details_by_sku.get(sku) or {}
            barcode = (row.get("barcode") or "").strip()
            item["barcode"] = barcode
            item["category_leaf"] = ""
            item["color"] = ""
            item["manufacturer_size"] = ""

            tid = row.get("type_id")
            desc = row.get("description_category_id")
            if tid is not None:
                try:
                    item["category_leaf"] = type_names.get(int(tid), "")
                except (TypeError, ValueError):
                    pass

            attrs = row.get("attributes") or []
            if desc is None or tid is None:
                return
            try:
                d_int = int(desc)
                t_int = int(tid)
            except (TypeError, ValueError):
                return

            cache_key = (d_int, t_int)
            if cache_key not in attr_id_cache:
                meta = self.client.get_description_category_attributes(d_int, t_int)
                attr_id_cache[cache_key] = resolve_color_and_mfr_size_attr_ids(meta)
            color_id, size_id = attr_id_cache[cache_key]
            item["color"] = first_attribute_value(attrs, color_id)
            item["manufacturer_size"] = first_attribute_value(attrs, size_id)

        created = 0
        updated = 0

        conn = get_db_connection()
        try:
            # Keep DB clean from statuses we do not use (только Ozon).
            conn.execute(
                "DELETE FROM order_items WHERE order_id IN ("
                "SELECT id FROM orders WHERE marketplace = 'ozon' "
                "AND status IN ('delivered', 'cancelled'))"
            )
            conn.execute(
                "DELETE FROM orders WHERE marketplace = 'ozon' AND status IN ('delivered', 'cancelled')"
            )

            for posting in postings:
                order = self._normalize_posting(posting)
                for item in order["items"]:
                    item["photo_url"] = image_map.get(item["sku"], "")
                    enrich_line_item(item)
                existed, order_id = self._upsert_order(conn, order)
                self._replace_order_items(conn, order_id, order["items"])
                self._sync_order_label_pdf(conn, order_id)
                if existed:
                    updated += 1
                else:
                    created += 1
            # Cleanup: delete orders which are not present on Ozon anymore (within the same sync window),
            # but only for the 3 statuses we actually keep history for.
            deleted = 0
            if not hit_cap and statuses_to_fetch:
                posting_numbers = sorted(
                    {str(p.get("posting_number") or "") for p in postings if p.get("posting_number")}
                )
                conn.execute("DROP TABLE IF EXISTS tmp_ozon_postings")
                conn.execute(
                    "CREATE TEMP TABLE tmp_ozon_postings (posting_number TEXT PRIMARY KEY)"
                )
                if posting_numbers:
                    chunk = 500
                    for i in range(0, len(posting_numbers), chunk):
                        part = posting_numbers[i : i + chunk]
                        conn.executemany(
                            "INSERT INTO tmp_ozon_postings (posting_number) VALUES (?)",
                            [(x,) for x in part],
                        )

                st_ph = ",".join("?" * len(statuses_to_fetch))
                cur_del = conn.execute(
                    f"""
                    DELETE FROM orders
                    WHERE marketplace = 'ozon'
                      AND status IN ({st_ph})
                      AND created_at >= ?
                      AND created_at <= ?
                      AND posting_number NOT IN (SELECT posting_number FROM tmp_ozon_postings)
                    """,
                    [*statuses_to_fetch, effective_since_iso, effective_to_iso],
                )
                deleted = int(getattr(cur_del, "rowcount", 0) or 0)

            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        self.logger.info("Синхронизация заказов завершена: created=%s updated=%s", created, updated)
        return {
            "created": created,
            "updated": updated,
            "total": len(postings),
            "deleted": deleted,
            "synced_statuses": statuses_to_fetch,
        }

    def sync_from_wb(
        self,
        since: Optional[str] = None,
        to: Optional[str] = None,
        max_records: int = 5000,
        progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, int]:
        """
        Синхронизация сборочных заданий FBS WB: /api/v3/orders/new + /api/v3/orders + статусы.

        В локальную БД попадают только заказы воронки как в кабинете: new / confirm / complete / wbgo
        (отмены и прочие supplierStatus не сохраняются, старые такие строки удаляются).

        progress_cb вызывается из того же потока (для стриминга прогресса в UI).
        """

        def _p(ev: Dict[str, Any]) -> None:
            if progress_cb:
                progress_cb(ev)

        client = WbClient()
        date_from, date_to = _wb_fbs_list_date_range_unix(since, to)

        by_id: Dict[int, Dict[str, Any]] = {}
        hit_cap = False

        new_ids_feed: set[int] = set()
        _p({"step": "new_orders_start"})
        for o in client.get_new_orders():
            oid = o.get("id")
            if oid is None:
                continue
            try:
                o_int = int(oid)
            except (TypeError, ValueError):
                continue
            new_ids_feed.add(o_int)
            by_id[o_int] = o
        _p({"step": "new_orders_done", "known": len(by_id), "in_new_feed": len(new_ids_feed)})

        next_cursor = 0
        prev_next: Optional[int] = None
        page_num = 0
        for _ in range(250):
            if len(by_id) >= max_records:
                hit_cap = True
                break
            page = client.get_orders_page(
                limit=min(1000, max_records - len(by_id) + 100),
                next_cursor=next_cursor,
                date_from=date_from,
                date_to=date_to,
            )
            orders = list(page.get("orders") or [])
            if not orders:
                break
            page_num += 1
            for o in orders:
                oid = o.get("id")
                if oid is not None:
                    by_id[int(oid)] = o
            _p(
                {
                    "step": "orders_page",
                    "page": page_num,
                    "batch": len(orders),
                    "known": len(by_id),
                }
            )
            next_raw = page.get("next")
            if next_raw is None:
                break
            try:
                next_int = int(next_raw)
            except (TypeError, ValueError):
                break
            if next_int == 0:
                break
            if prev_next is not None and next_int == prev_next:
                break
            prev_next = next_int
            next_cursor = next_int

        _p({"step": "orders_pages_done", "pages": page_num, "known": len(by_id)})

        ids_sorted = sorted(by_id.keys())
        status_map: Dict[int, Tuple[str, str]] = {}
        chunk_sz = 1000
        n_chunks = (len(ids_sorted) + chunk_sz - 1) // chunk_sz if ids_sorted else 0
        for ci, i in enumerate(range(0, len(ids_sorted), chunk_sz), start=1):
            chunk = ids_sorted[i : i + chunk_sz]
            _p(
                {
                    "step": "status_chunk",
                    "chunk": ci,
                    "chunks_total": n_chunks,
                    "ids": len(chunk),
                }
            )
            rows = client.get_orders_statuses(chunk)
            for r in rows:
                oid = r.get("id")
                if oid is None:
                    continue
                try:
                    oid_int = int(oid)
                except (TypeError, ValueError):
                    continue
                ss = r.get("supplierStatus")
                ws = r.get("wbStatus")
                status_map[oid_int] = (
                    str(ss if ss is not None else "new"),
                    str(ws if ws is not None else ""),
                )

        # Повторяем запрос только для id, которых нет в ответе (иначе все получают new → завышение «Новые»).
        for retry_round in range(2):
            missing_status = [oid for oid in ids_sorted if oid not in status_map]
            if not missing_status:
                break
            self.logger.warning(
                "WB /orders/status: в ответе нет %s id, повтор %s/2",
                len(missing_status),
                retry_round + 1,
            )
            _p(
                {
                    "step": "status_retry",
                    "missing": len(missing_status),
                    "round": retry_round + 1,
                }
            )
            for i in range(0, len(missing_status), chunk_sz):
                chunk = missing_status[i : i + chunk_sz]
                rows = client.get_orders_statuses(chunk)
                for r in rows:
                    oid = r.get("id")
                    if oid is None:
                        continue
                    try:
                        oid_int = int(oid)
                    except (TypeError, ValueError):
                        continue
                    ss = r.get("supplierStatus")
                    ws = r.get("wbStatus")
                    status_map[oid_int] = (
                        str(ss if ss is not None else "new"),
                        str(ws if ws is not None else ""),
                    )

        _p({"step": "statuses_done", "chunks_total": n_chunks})

        def _wb_supplier_from_status_map(oid: int) -> str:
            if oid in status_map:
                return status_map[oid][0]
            return "new"

        portal_order_pairs: List[Tuple[int, Dict[str, Any]]] = []
        for oid, raw in by_id.items():
            if _wb_supplier_from_status_map(oid) not in WB_PORTAL_SUPPLIER_STATUSES:
                continue
            portal_order_pairs.append((oid, raw))

        unique_nm: List[int] = []
        seen_nm: set[int] = set()
        for _oid, raw_o in portal_order_pairs:
            nm = raw_o.get("nmId")
            if nm is None:
                continue
            try:
                nmi = int(nm)
            except (TypeError, ValueError):
                continue
            if nmi not in seen_nm:
                seen_nm.add(nmi)
                unique_nm.append(nmi)

        cards_by_nm: Dict[int, Dict[str, Any]] = {}
        if Config.WB_FETCH_CONTENT_CARDS and unique_nm:
            needed_set = set(unique_nm)
            total_nm = len(needed_set)
            _p({"step": "content_cards_start", "total": total_nm})
            auth_stopped = False
            try:
                if Config.WB_USE_CATALOG_FOR_CARDS:
                    cards_by_nm = lookup_wb_cards(needed_set)
                    _p(
                        {
                            "step": "content_cards",
                            "mode": "local_db",
                            "found": len(cards_by_nm),
                            "needed": total_nm,
                        }
                    )
                    missing = set(needed_set) - set(cards_by_nm.keys())
                else:
                    cards_by_nm = {}
                    missing = set(needed_set)

                if missing and Config.WB_CATALOG_FILL_GAPS_FROM_API:
                    cc = WbContentClient()

                    def _content_progress(ev: Dict[str, Any]) -> None:
                        _p({"step": "content_cards", **ev})

                    fetched = cc.fetch_cards_for_nm_ids(
                        missing,
                        progress_cb=_content_progress,
                    )
                    cards_by_nm.update(fetched)
                    if fetched and Config.WB_CATALOG_SAVE_API_GAPS:
                        try:
                            save_wb_cards_to_catalog(fetched)
                        except (OSError, sqlite3.Error) as exc:
                            self.logger.warning("Не удалось записать добор карточек в wb_catalog.db: %s", exc)
            except WbContentError as exc:
                low = str(exc).lower()
                if "авториз" in low or "401" in low or "403" in low:
                    self.logger.warning(
                        "WB Content API: %s — добор карточек из API не выполнен.",
                        exc,
                    )
                    auth_stopped = True
                else:
                    self.logger.warning("WB Content API: %s", exc)
            _p(
                {
                    "step": "content_cards_done",
                    "loaded": len(cards_by_nm),
                    "requested": total_nm,
                    "auth_stopped": auth_stopped,
                }
            )

        created = 0
        updated = 0
        deleted = 0
        conn = get_db_connection()
        try:
            prev_db_status: Dict[int, Tuple[str, str]] = {}
            if ids_sorted:
                for i in range(0, len(ids_sorted), 400):
                    part = ids_sorted[i : i + 400]
                    ph = ",".join("?" * len(part))
                    qmarks = [str(x) for x in part]
                    for row in conn.execute(
                        f"""
                        SELECT posting_number, status, substatus
                        FROM orders WHERE marketplace = 'wb' AND posting_number IN ({ph})
                        """,
                        qmarks,
                    ):
                        try:
                            pn = int(str(row["posting_number"]))
                        except (TypeError, ValueError):
                            continue
                        prev_db_status[pn] = (
                            str(row["status"] or "new"),
                            str(row["substatus"] or ""),
                        )

            posting_numbers: List[str] = []
            wb_ids_for_labels: List[int] = []
            n_save = len(portal_order_pairs)
            _p({"step": "database_start", "total": n_save})
            for idx, (oid, raw) in enumerate(portal_order_pairs, start=1):
                if oid in status_map:
                    sup, wbst = status_map[oid]
                elif oid in prev_db_status:
                    sup, wbst = prev_db_status[oid]
                    self.logger.debug(
                        "WB заказ id=%s: статус из локальной БД (нет в ответе /orders/status)",
                        oid,
                    )
                else:
                    sup, wbst = ("new", "")
                order = self._normalize_wb_order(raw, sup, wbst)
                if order.get("items") and cards_by_nm:
                    try:
                        nmi = int(raw["nmId"]) if raw.get("nmId") is not None else None
                    except (TypeError, ValueError):
                        nmi = None
                    if nmi is not None:
                        ccard = cards_by_nm.get(nmi)
                        if ccard:
                            self._wb_apply_content_card_to_item(order["items"][0], raw, ccard)
                if not order["posting_number"]:
                    continue
                posting_numbers.append(order["posting_number"])
                existed, order_id = self._upsert_order(conn, order)
                self._replace_order_items(conn, order_id, order["items"])
                wb_ids_for_labels.append(order_id)
                if existed:
                    updated += 1
                else:
                    created += 1
                if n_save and (idx == 1 or idx % 40 == 0 or idx == n_save):
                    _p({"step": "database_save", "current": idx, "total": n_save})
                # Commit на каждый заказ: короткие транзакции — меньше «database is locked» при PDF/параллельном UI.
                conn.commit()

            if not hit_cap and posting_numbers:
                conn.execute("DROP TABLE IF EXISTS tmp_wb_postings")
                conn.execute("CREATE TEMP TABLE tmp_wb_postings (posting_number TEXT PRIMARY KEY)")
                for i in range(0, len(posting_numbers), 500):
                    part = posting_numbers[i : i + 500]
                    conn.executemany(
                        "INSERT OR IGNORE INTO tmp_wb_postings (posting_number) VALUES (?)",
                        [(x,) for x in part],
                    )
                st_ph = ",".join("?" * len(WB_PORTAL_SUPPLIER_STATUSES))
                cur_del = conn.execute(
                    f"""
                    DELETE FROM orders
                    WHERE marketplace = 'wb'
                      AND status IN ({st_ph})
                      AND posting_number NOT IN (SELECT posting_number FROM tmp_wb_postings)
                    """,
                    list(WB_PORTAL_SUPPLIER_STATUSES),
                )
                deleted = int(getattr(cur_del, "rowcount", 0) or 0)

            # Отмены и прочие статусы вне воронки кабинета — убираем из локальной БД.
            conn.execute(
                """
                DELETE FROM orders
                WHERE marketplace = 'wb'
                  AND status NOT IN ('new', 'confirm', 'complete', 'wbgo')
                """
            )

            # В кабинете WB «Новые» = лента /api/v3/orders/new, а не все supplierStatus=new из архива.
            conn.execute("UPDATE orders SET wb_in_new_feed = 0 WHERE marketplace = 'wb'")
            for nid in new_ids_feed:
                conn.execute(
                    "UPDATE orders SET wb_in_new_feed = 1 WHERE marketplace = 'wb' AND posting_number = ?",
                    (str(nid),),
                )

            conn.commit()

            if Config.WB_SYNC_BUILD_LABEL_PDF and wb_ids_for_labels:
                n_lab = len(wb_ids_for_labels)
                _p({"step": "wb_labels_start", "total": n_lab})
                for li, order_id in enumerate(wb_ids_for_labels, start=1):
                    self._sync_order_label_pdf(conn, order_id, fetch_ozon_for_size=False)
                    conn.commit()
                    if n_lab and (li == 1 or li % 40 == 0 or li == n_lab):
                        _p({"step": "wb_labels_save", "current": li, "total": n_lab})
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        self.logger.info(
            "Синхронизация WB завершена: created=%s updated=%s deleted=%s new_feed=%s",
            created,
            updated,
            deleted,
            len(new_ids_feed),
        )
        return {
            "created": created,
            "updated": updated,
            "total": len(by_id),
            "deleted": deleted,
            "wb_new_feed": len(new_ids_feed),
        }

    @staticmethod
    def _wb_item_dict_from_order_item_row(it_row: Any) -> Dict[str, Any]:
        """Собирает dict позиции как при нормализации WB (для _wb_apply_content_card_to_item)."""
        return {
            "sku": str(it_row["sku"] or ""),
            "offer_id": str(it_row["offer_id"] or ""),
            "name": str(it_row["product_name"] or ""),
            "quantity": int(it_row["quantity"] or 0) or 1,
            "price": float(it_row["price"] or 0),
            "photo_url": str(it_row["photo_url"] or ""),
            "category_leaf": str(it_row["category_leaf"] or ""),
            "color": str(it_row["color"] or ""),
            "barcode": str(it_row["barcode"] or ""),
            "manufacturer_size": str(it_row["manufacturer_size"] or ""),
        }

    def enrich_wb_order_items_from_local_catalog(self) -> Dict[str, Any]:
        """
        Подтягивает название, фото, размер, категорию, цвет в order_items из wb_catalog.db по nmId в orders.raw_json.
        Не вызывает Content API. Нужно после полной синхронизации каталога или если заказы были сохранены без enrich.
        """
        if not Config.WB_USE_CATALOG_FOR_CARDS:
            return {
                "items_updated": 0,
                "orders_seen": 0,
                "skipped": True,
                "reason": "WB_USE_CATALOG_FOR_CARDS off",
            }

        conn = get_db_connection()
        items_updated = 0
        orders_seen = 0
        orders_for_label: Set[int] = set()
        try:
            rows = list(
                conn.execute(
                    """
                    SELECT id, raw_json FROM orders
                    WHERE marketplace = 'wb'
                      AND status IN ('new', 'confirm', 'complete', 'wbgo')
                      AND raw_json IS NOT NULL
                      AND TRIM(raw_json) != ''
                    """
                )
            )
            orders_seen = len(rows)

            nm_to_order_ids: Dict[int, List[int]] = {}
            order_raw: Dict[int, Dict[str, Any]] = {}
            for row in rows:
                oid = int(row["id"])
                try:
                    raw = json.loads(row["raw_json"])
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(raw, dict):
                    continue
                nm = raw.get("nmId")
                if nm is None:
                    continue
                try:
                    nmi = int(nm)
                except (TypeError, ValueError):
                    continue
                nm_to_order_ids.setdefault(nmi, []).append(oid)
                order_raw[oid] = raw

            if not nm_to_order_ids:
                return {"items_updated": 0, "orders_seen": orders_seen, "skipped": False}

            cards = lookup_wb_cards(set(nm_to_order_ids.keys()))

            for nmi, oids in nm_to_order_ids.items():
                card = cards.get(nmi)
                if not card:
                    continue
                for oid in oids:
                    raw = order_raw.get(oid)
                    if not raw:
                        continue
                    item_rows = conn.execute(
                        "SELECT * FROM order_items WHERE order_id = ? ORDER BY id",
                        (oid,),
                    ).fetchall()
                    for it_row in item_rows:
                        item = self._wb_item_dict_from_order_item_row(it_row)
                        self._wb_apply_content_card_to_item(item, raw, card)
                        conn.execute(
                            """
                            UPDATE order_items SET
                                product_name = ?,
                                photo_url = ?,
                                category_leaf = ?,
                                color = ?,
                                manufacturer_size = ?
                            WHERE id = ?
                            """,
                            (
                                item["name"],
                                item.get("photo_url", ""),
                                item.get("category_leaf", ""),
                                item.get("color", ""),
                                item.get("manufacturer_size", ""),
                                it_row["id"],
                            ),
                        )
                        items_updated += 1
                        orders_for_label.add(oid)

            for oid in sorted(orders_for_label):
                self._sync_order_label_pdf(conn, oid, fetch_ozon_for_size=False)
                conn.commit()

            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        return {
            "items_updated": items_updated,
            "orders_seen": orders_seen,
            "skipped": False,
        }

    def sync_wb_catalog_for_new_orders_only(
        self,
        progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        """
        Карточки Content API только по nmId из заказов вкладки «Новые» (WB, лента /new),
        запись в wb_catalog.db (если включено) и UPDATE order_items: название, фото, размер, категория.
        """

        def _p(ev: Dict[str, Any]) -> None:
            if progress_cb:
                progress_cb(ev)

        conn = get_db_connection()
        try:
            order_rows = list(
                conn.execute(
                    """
                    SELECT id, raw_json FROM orders
                    WHERE marketplace = 'wb'
                      AND status = 'new'
                      AND COALESCE(wb_in_new_feed, 0) = 1
                      AND raw_json IS NOT NULL
                      AND TRIM(raw_json) != ''
                    """
                )
            )
        finally:
            conn.close()

        _p({"step": "wb_new_catalog_scan", "orders_in_tab_new": len(order_rows)})

        nm_ids: Set[int] = set()
        orders_payload: List[Tuple[int, Dict[str, Any]]] = []
        for row in order_rows:
            try:
                raw = json.loads(row["raw_json"])
            except (json.JSONDecodeError, TypeError):
                self.logger.warning("WB order id=%s: битый raw_json", row["id"])
                continue
            if not isinstance(raw, dict):
                continue
            nm = raw.get("nmId")
            if nm is None:
                continue
            try:
                nmi = int(nm)
            except (TypeError, ValueError):
                continue
            nm_ids.add(nmi)
            orders_payload.append((int(row["id"]), raw))

        if not nm_ids:
            _p(
                {
                    "step": "wb_new_catalog_skip",
                    "reason": "no_nmId",
                    "orders_in_tab_new": len(order_rows),
                }
            )
            return {
                "ok": True,
                "unique_nm": 0,
                "orders_seen": len(order_rows),
                "cards_fetched": 0,
                "items_updated": 0,
                "message": "Нет заказов «Новые» или без nmId в raw_json",
            }

        _p(
            {
                "step": "wb_new_catalog_start",
                "unique_nm": len(nm_ids),
                "orders": len(orders_payload),
            }
        )

        cards: Dict[int, Dict[str, Any]] = {}
        err_msg: Optional[str] = None
        try:
            if Config.WB_USE_CATALOG_FOR_CARDS:
                cards = lookup_wb_cards(nm_ids)
                _p(
                    {
                        "step": "wb_new_catalog_local",
                        "found": len(cards),
                        "needed": len(nm_ids),
                    }
                )
            missing = set(nm_ids) - set(cards.keys())
            if missing and Config.WB_CATALOG_FILL_GAPS_FROM_API:
                _p(
                    {
                        "step": "wb_new_catalog_api_start",
                        "missing_nm": len(missing),
                    }
                )
                cc = WbContentClient()

                def _content_progress(ev: Dict[str, Any]) -> None:
                    _p({"step": "wb_new_catalog_api", **ev})

                # Не гоняем весь каталог WB: API не умеет выборку по списку nmId, только textSearch по одному.
                fetched = cc.fetch_cards_for_nm_ids(
                    missing,
                    progress_cb=_content_progress,
                    use_catalog_pagination=False,
                )
                cards.update(fetched)
                if fetched and Config.WB_CATALOG_SAVE_API_GAPS:
                    try:
                        save_wb_cards_to_catalog(fetched)
                    except (OSError, sqlite3.Error) as exc:
                        self.logger.warning("wb_catalog.db: не записали добор: %s", exc)
        except WbContentError as exc:
            err_msg = str(exc)
            self.logger.warning("Content API (каталог для «Новые»): %s", exc)

        _p(
            {
                "step": "wb_new_catalog_fetch_done",
                "cards": len(cards),
                "needed_nm": len(nm_ids),
                "error": err_msg,
            }
        )

        n_ord = len(orders_payload)
        _p({"step": "wb_new_catalog_db_start", "total": n_ord})

        items_updated = 0
        conn = get_db_connection()
        try:
            for idx, (order_id, raw) in enumerate(orders_payload, start=1):
                try:
                    nmi = int(raw["nmId"])
                except (TypeError, ValueError, KeyError):
                    continue
                card = cards.get(nmi)
                if not card:
                    continue
                oi = conn.execute(
                    "SELECT * FROM order_items WHERE order_id = ? ORDER BY id LIMIT 1",
                    (order_id,),
                ).fetchone()
                if not oi:
                    continue
                item = dict(oi)
                item["name"] = (item.get("product_name") or "").strip() or (
                    str(item.get("offer_id") or "") or str(nmi)
                )
                self._wb_apply_content_card_to_item(item, raw, card)
                cur = conn.execute(
                    """
                    UPDATE order_items SET
                        product_name = ?,
                        photo_url = ?,
                        manufacturer_size = ?,
                        category_leaf = ?,
                        color = ?
                    WHERE id = ?
                    """,
                    (
                        item.get("name") or "",
                        item.get("photo_url") or "",
                        item.get("manufacturer_size") or "",
                        item.get("category_leaf") or "",
                        item.get("color") or "",
                        item["id"],
                    ),
                )
                if cur.rowcount:
                    items_updated += int(cur.rowcount)
                    self._sync_order_label_pdf(conn, order_id, fetch_ozon_for_size=False)
                if n_ord and (idx == 1 or idx % 20 == 0 or idx == n_ord):
                    _p(
                        {
                            "step": "wb_new_catalog_db",
                            "current": idx,
                            "total": n_ord,
                            "items_updated": items_updated,
                        }
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        return {
            "ok": err_msg is None or len(cards) > 0,
            "unique_nm": len(nm_ids),
            "orders_seen": len(order_rows),
            "orders_parsed": len(orders_payload),
            "cards_fetched": len(cards),
            "items_updated": items_updated,
            "error": err_msg,
        }

    def _upsert_order(self, conn, order: Dict[str, Any]) -> Tuple[bool, int]:
        now = datetime.utcnow().isoformat()
        row = conn.execute(
            "SELECT id, status, shipment_id FROM orders WHERE marketplace = ? AND posting_number = ?",
            (order["marketplace"], order["posting_number"]),
        ).fetchone()

        if row:
            incoming_status = order["status"]
            current_status = str(row["status"] or "")
            current_shipment_id = row["shipment_id"]
            # Ozon may return stale awaiting_packaging for a short time right after ship.
            # If the order is already attached to a shipment and locally moved to awaiting_deliver,
            # do not regress it back to awaiting_packaging on this sync tick.
            if (
                order["marketplace"] == "ozon"
                and incoming_status == "awaiting_packaging"
                and current_status == "awaiting_deliver"
                and current_shipment_id is not None
            ):
                incoming_status = "awaiting_deliver"

            conn.execute(
                """
                UPDATE orders SET
                    order_number = ?, status = ?, substatus = ?, created_at = ?, shipment_date = ?,
                    delivery_method = ?, warehouse_name = ?, customer_name = ?, customer_phone = ?,
                    total_price = ?, currency_code = ?, is_fbs = ?, raw_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    order["order_number"],
                    incoming_status,
                    order["substatus"],
                    order["created_at"],
                    order["shipment_date"],
                    order["delivery_method"],
                    order["warehouse_name"],
                    order["customer_name"],
                    order["customer_phone"],
                    order["total_price"],
                    order["currency_code"],
                    order["is_fbs"],
                    order["raw_json"],
                    now,
                    row["id"],
                ),
            )
            return True, row["id"]

        cursor = conn.execute(
            """
            INSERT INTO orders (
                marketplace, posting_number, order_number, status, substatus, created_at,
                shipment_date, delivery_method, warehouse_name, customer_name, customer_phone,
                total_price, currency_code, is_fbs, raw_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order["marketplace"],
                order["posting_number"],
                order["order_number"],
                order["status"],
                order["substatus"],
                order["created_at"],
                order["shipment_date"],
                order["delivery_method"],
                order["warehouse_name"],
                order["customer_name"],
                order["customer_phone"],
                order["total_price"],
                order["currency_code"],
                order["is_fbs"],
                order["raw_json"],
                now,
            ),
        )
        return False, cursor.lastrowid

    @staticmethod
    def _replace_order_items(conn, order_id: int, items: List[Dict[str, Any]]) -> None:
        conn.execute("DELETE FROM order_items WHERE order_id = ?", (order_id,))
        for item in items:
            conn.execute(
                """
                INSERT INTO order_items (
                    order_id, sku, offer_id, product_name, quantity, price, photo_url,
                    category_leaf, color, barcode, manufacturer_size
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    item["sku"],
                    item["offer_id"],
                    item["name"],
                    item["quantity"],
                    item["price"],
                    item.get("photo_url", ""),
                    item.get("category_leaf", ""),
                    item.get("color", ""),
                    item.get("barcode", ""),
                    item.get("manufacturer_size", ""),
                ),
            )

    @staticmethod
    def _unlink_label_file(rel: str) -> None:
        if not rel:
            return
        path = rel if os.path.isabs(rel) else os.path.join(Config.BASE_DIR, rel.replace("/", os.sep))
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass

    def _sync_order_label_pdf(
        self,
        conn: sqlite3.Connection,
        order_id: int,
        *,
        fetch_ozon_for_size: bool = False,
    ) -> None:
        row = conn.execute(
            "SELECT label_pdf_path, posting_number, marketplace FROM orders WHERE id = ?",
            (order_id,),
        ).fetchone()
        old = (row["label_pdf_path"] or "").strip() if row else ""
        posting = str(row["posting_number"] or "").strip() if row else ""
        mp = str(row["marketplace"] or "").strip() if row else ""
        item_rows = conn.execute(
            "SELECT * FROM order_items WHERE order_id = ? ORDER BY id",
            (order_id,),
        ).fetchall()
        dicts = [dict(r) for r in item_rows]
        pages = build_label_pages_for_order_items(dicts)
        if not pages:
            conn.execute("UPDATE orders SET label_pdf_path = NULL WHERE id = ?", (order_id,))
            if old:
                self._unlink_label_file(old)
            return
        target_pt = None
        fit_letterbox = False
        if posting and mp == "ozon":
            # Во время sync_from_ozon только кэш: иначе сотни HTTP-запросов внутри одной
            # транзакции SQLite → database is locked и шум 400 от Ozon для не-отгрузки.
            if fetch_ozon_for_size:
                ozon_pdf = ensure_ozon_label_pdf_for_posting(
                    posting, self.client.get_fbs_package_label_pdf
                )
            else:
                ozon_pdf = load_cached_ozon_label(posting)
            if ozon_pdf:
                try:
                    target_pt = first_page_size_pt(ozon_pdf)
                    fit_letterbox = True
                except Exception:
                    self.logger.warning(
                        "Не удалось прочитать размер страницы этикетки Ozon для %s",
                        posting,
                    )

        try:
            rel = write_order_label_pdf(
                order_id,
                pages,
                target_page_pt=target_pt,
                target_fit_letterbox=fit_letterbox,
            )
        except (FileNotFoundError, ValueError) as exc:
            global _label_template_unavailable_logged
            if not _label_template_unavailable_logged:
                _label_template_unavailable_logged = True
                self.logger.warning(
                    "Этикетки ШК не создаются: %s. "
                    "Задайте LABEL_TEMPLATE_PDF в .env или положите print_2026_03_25_21_41.pdf в корень проекта.",
                    exc,
                )
            conn.execute("UPDATE orders SET label_pdf_path = NULL WHERE id = ?", (order_id,))
            if old:
                self._unlink_label_file(old)
            return
        except Exception as exc:
            self.logger.warning("Не удалось собрать PDF этикетки для order_id=%s: %s", order_id, exc)
            conn.execute("UPDATE orders SET label_pdf_path = NULL WHERE id = ?", (order_id,))
            if old:
                self._unlink_label_file(old)
            return

        conn.execute(
            "UPDATE orders SET label_pdf_path = ? WHERE id = ?",
            (rel, order_id),
        )
        if old and old != rel:
            self._unlink_label_file(old)

    def rebuild_all_label_pdfs(self) -> Dict[str, int]:
        """Пересоздать PDF этикетки для каждого заказа (по строкам order_items с баркодом)."""
        conn = get_db_connection()
        n_orders = 0
        n_with_pdf = 0
        try:
            rows = conn.execute("SELECT id FROM orders ORDER BY id").fetchall()
            for r in rows:
                oid = int(r["id"])
                self._sync_order_label_pdf(conn, oid)
                n_orders += 1
                conn.commit()
                row2 = conn.execute(
                    "SELECT label_pdf_path FROM orders WHERE id = ?",
                    (oid,),
                ).fetchone()
                if row2 and (row2["label_pdf_path"] or "").strip():
                    n_with_pdf += 1
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        self.logger.info(
            "Пересборка этикеток: заказов=%s, с PDF=%s",
            n_orders,
            n_with_pdf,
        )
        return {"orders": n_orders, "with_label_pdf": n_with_pdf}

    def ensure_order_label_pdf_file(self, order_id: int) -> Optional[str]:
        conn = get_db_connection()
        try:
            row = conn.execute(
                "SELECT id, label_pdf_path, marketplace FROM orders WHERE id = ?",
                (order_id,),
            ).fetchone()
            if not row:
                return None
            rel = (row["label_pdf_path"] or "").strip()
            path = os.path.join(Config.BASE_DIR, rel.replace("/", os.sep)) if rel else ""
            if rel and os.path.isfile(path):
                return rel
            fetch_ozon = str(row["marketplace"] or "") == "ozon"
            self._sync_order_label_pdf(conn, order_id, fetch_ozon_for_size=fetch_ozon)
            conn.commit()
            row2 = conn.execute(
                "SELECT label_pdf_path FROM orders WHERE id = ?",
                (order_id,),
            ).fetchone()
            return ((row2["label_pdf_path"] or "").strip() or None) if row2 else None
        finally:
            conn.close()

    def get_order_marketplace(self, order_id: int) -> Optional[str]:
        conn = get_db_connection()
        try:
            row = conn.execute(
                "SELECT marketplace FROM orders WHERE id = ?",
                (order_id,),
            ).fetchone()
            if not row:
                return None
            return str(row["marketplace"] or "").strip() or None
        finally:
            conn.close()

    def get_order_posting_number(self, order_id: int) -> Optional[str]:
        conn = get_db_connection()
        try:
            row = conn.execute(
                "SELECT posting_number FROM orders WHERE id = ?",
                (order_id,),
            ).fetchone()
            if not row or not row["posting_number"]:
                return None
            return str(row["posting_number"])
        finally:
            conn.close()

    def get_order_posting_and_status(self, order_id: int) -> Optional[Dict[str, str]]:
        conn = get_db_connection()
        try:
            row = conn.execute(
                "SELECT posting_number, status FROM orders WHERE id = ?",
                (order_id,),
            ).fetchone()
            if not row:
                return None
            return {
                "posting_number": str(row["posting_number"] or ""),
                "status": str(row["status"] or ""),
            }
        finally:
            conn.close()

    def get_orders(
        self,
        status: str = "all",
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        query: Optional[str] = None,
        page: int = 1,
        per_page: int = 20,
        marketplace: str = "ozon",
    ) -> Dict[str, Any]:
        conn = get_db_connection()
        try:
            where = ["1=1"]
            params: List[Any] = []

            if marketplace:
                where.append("o.marketplace = ?")
                params.append(marketplace)

            if marketplace == "wb" and status == "new":
                where.append("o.status = 'new'")
                where.append("COALESCE(o.wb_in_new_feed, 0) = 1")
            elif marketplace == "wb" and status == "in_delivery":
                where.append("o.status IN ('complete', 'wbgo')")
            elif marketplace == "wb" and status == "new_stale":
                # Старые ссылки: всё ещё status=new, но без фильтра ленты
                where.append("o.status = 'new'")
                where.append("COALESCE(o.wb_in_new_feed, 0) = 0")
            elif status and status != "all":
                where.append("o.status = ?")
                params.append(status)
            if date_from:
                where.append("o.created_at >= ?")
                params.append(date_from)
            if date_to:
                where.append("o.created_at <= ?")
                params.append(date_to + "T23:59:59")
            if query:
                where.append(
                    """(
                        o.posting_number LIKE ?
                        OR o.order_number LIKE ?
                        OR EXISTS (
                            SELECT 1 FROM order_items oi
                            WHERE oi.order_id = o.id AND (
                                oi.sku LIKE ? OR oi.offer_id LIKE ?
                                OR IFNULL(oi.barcode, '') LIKE ?
                                OR IFNULL(oi.color, '') LIKE ?
                                OR IFNULL(oi.manufacturer_size, '') LIKE ?
                                OR IFNULL(oi.category_leaf, '') LIKE ?
                            )
                        )
                    )"""
                )
                q = f"%{query}%"
                params.extend([q, q, q, q, q, q, q, q])

            where_sql = " AND ".join(where)
            total = conn.execute(
                f"SELECT COUNT(*) as cnt FROM orders o WHERE {where_sql}",
                params,
            ).fetchone()["cnt"]

            offset = (page - 1) * per_page
            rows = conn.execute(
                f"""
                SELECT
                    o.*,
                    COALESCE(SUM(oi.quantity), 0) as total_qty,
                    s.name AS shipment_name,
                    COALESCE(sc.orders_count, 0) AS shipment_orders_count
                FROM orders o
                LEFT JOIN order_items oi ON oi.order_id = o.id
                LEFT JOIN shipments s ON s.id = o.shipment_id
                LEFT JOIN (
                    SELECT shipment_id, COUNT(*) AS orders_count
                    FROM orders
                    WHERE shipment_id IS NOT NULL
                    GROUP BY shipment_id
                ) sc ON sc.shipment_id = o.shipment_id
                WHERE {where_sql}
                GROUP BY o.id
                ORDER BY o.created_at DESC
                LIMIT ? OFFSET ?
                """,
                params + [per_page, offset],
            ).fetchall()

            statuses = conn.execute(
                """
                SELECT status, COUNT(*) as cnt FROM orders
                WHERE marketplace = ?
                GROUP BY status ORDER BY cnt DESC
                """,
                (marketplace,),
            ).fetchall()
            total_all = conn.execute(
                "SELECT COUNT(*) as cnt FROM orders WHERE marketplace = ?",
                (marketplace,),
            ).fetchone()["cnt"]
            status_counts = {str(s["status"]): int(s["cnt"]) for s in statuses if s["status"]}
            if marketplace == "wb":
                status_options = [
                    {"value": "new", "label": "Новые"},
                    {"value": "confirm", "label": "На сборке"},
                    {"value": "in_delivery", "label": "В доставке"},
                ]
                new_feed_n = conn.execute(
                    """
                    SELECT COUNT(*) as cnt FROM orders
                    WHERE marketplace = 'wb' AND status = 'new' AND COALESCE(wb_in_new_feed, 0) = 1
                    """
                ).fetchone()["cnt"]
                in_delivery_n = conn.execute(
                    """
                    SELECT COUNT(*) as cnt FROM orders
                    WHERE marketplace = 'wb' AND status IN ('complete', 'wbgo')
                    """
                ).fetchone()["cnt"]
                wb_tabs_order = [
                    ("new", "Новые"),
                    ("confirm", "На сборке"),
                    ("in_delivery", "В доставке"),
                ]
                tab_counts = {
                    "new": int(new_feed_n),
                    "confirm": int(status_counts.get("confirm", 0)),
                    "in_delivery": int(in_delivery_n),
                }
                status_tabs = [
                    {"value": code, "label": label, "count": tab_counts.get(code, 0)}
                    for code, label in wb_tabs_order
                ]
                status_tabs.append({"value": "all", "label": "Все", "count": int(total_all)})
            else:
                status_options = [
                    {"value": s["status"], "label": STATUS_LABELS.get(s["status"], s["status"] or "-")}
                    for s in statuses
                    if s["status"]
                ]
                status_tabs = [
                    {
                        "value": "awaiting_packaging",
                        "label": "Ожидают сборки",
                        "count": int(status_counts.get("awaiting_packaging", 0)),
                    },
                    {
                        "value": "awaiting_deliver",
                        "label": "Ожидают отгрузки",
                        "count": int(status_counts.get("awaiting_deliver", 0)),
                    },
                    {
                        "value": "delivering",
                        "label": "Доставляются",
                        "count": int(status_counts.get("delivering", 0)),
                    },
                    {"value": "all", "label": "Все", "count": int(total_all)},
                ]

            orders = [dict(r) for r in rows]
            for order in orders:
                items = conn.execute(
                    "SELECT * FROM order_items WHERE order_id = ?",
                    (order["id"],),
                ).fetchall()
                order["items"] = [dict(i) for i in items]
                # Used by UI to warn about splitting into multiple shipments (like "Разделить заказ").
                order["unit_count"] = sum(int(i.get("quantity") or 0) for i in order["items"])
                order["has_multi_unit"] = order["unit_count"] > 1
                if marketplace == "wb":
                    if (
                        order.get("status") == "new"
                        and not int(order.get("wb_in_new_feed") or 0)
                    ):
                        order["status_label"] = "Новые · не в ленте /new"
                    else:
                        order["status_label"] = WB_STATUS_LABELS.get(
                            order.get("status"), order.get("status") or "-"
                        )
                else:
                    order["status_label"] = STATUS_LABELS.get(
                        order.get("status"), order.get("status") or "-"
                    )
                order["created_at_display"] = _format_ru_short_datetime(order.get("created_at"))
                order["shipment_date_display"] = _format_ru_short_datetime(order.get("shipment_date"))
                tinfo = parse_shipment_tariff_from_raw(order.get("raw_json"))
                order["tariff_label"] = tinfo["label"]
                order["tariff_hint"] = tinfo["hint"]
                order["tariff_segment_active"] = tinfo["segment_active"]
                order["tariff_segment_count"] = tinfo["segment_count"]

            return {
                "orders": orders,
                "status_options": status_options,
                "status_tabs": status_tabs,
                "total": total,
                "page": page,
                "per_page": per_page,
                "pages": max(1, (total + per_page - 1) // per_page),
            }
        finally:
            conn.close()

    def suggest_next_shipment_name(self, on_day: Optional[date] = None) -> str:
        prefix = _shipment_date_prefix(on_day)
        conn = get_db_connection()
        try:
            max_n = 0
            like_arg = prefix + "%"
            for row in conn.execute(
                "SELECT name FROM shipments WHERE name LIKE ?",
                (like_arg,),
            ):
                name = row["name"] or ""
                if not name.startswith(prefix):
                    continue
                tail = name[len(prefix) :]
                try:
                    max_n = max(max_n, int(tail))
                except ValueError:
                    continue
            return f"{prefix}{max_n + 1}"
        finally:
            conn.close()

    def get_shipments_available_for_awaiting_deliver(self, marketplace: str = "ozon") -> List[Dict[str, Any]]:
        """
        Поставки для выпадающего списка "Добавить в существующую".
        Берём только те поставки, к которым уже привязаны заказы в статусе awaiting_deliver.
        """
        conn = get_db_connection()
        try:
            rows = conn.execute(
                """
                SELECT DISTINCT s.id, s.name
                FROM shipments s
                JOIN orders o ON o.shipment_id = s.id
                WHERE o.status = ?
                  AND o.shipment_id IS NOT NULL
                  AND s.marketplace = ?
                ORDER BY s.created_at DESC
                """,
                ["awaiting_deliver", marketplace],
            ).fetchall()
            return [{"id": int(r["id"]), "name": r["name"]} for r in rows]
        finally:
            conn.close()

    def get_shipments_with_awaiting_deliver_orders(
        self,
        marketplace: str = "ozon",
    ) -> List[Dict[str, Any]]:
        """Список созданных поставок, где есть хотя бы один заказ в awaiting_deliver."""
        conn = get_db_connection()
        try:
            rows = conn.execute(
                """
                SELECT
                    s.id,
                    s.name,
                    s.created_at,
                    COUNT(o.id) AS orders_count
                FROM shipments s
                JOIN orders o ON o.shipment_id = s.id
                WHERE o.status = ?
                  AND o.shipment_id IS NOT NULL
                  AND s.marketplace = ?
                GROUP BY s.id, s.name, s.created_at
                ORDER BY s.created_at DESC
                """,
                ["awaiting_deliver", marketplace],
            ).fetchall()
            return [
                {
                    "id": int(r["id"]),
                    "name": r["name"],
                    "created_at": r["created_at"],
                    "orders_count": int(r["orders_count"]),
                }
                for r in rows
            ]
        finally:
            conn.close()

    def get_shipment_detail(
        self,
        shipment_id: int,
        marketplace: str = "ozon",
    ) -> Dict[str, Any]:
        """Детальная страница поставки: все заказы внутри неё (все статусы)."""
        conn = get_db_connection()
        try:
            ship = conn.execute(
                """
                SELECT id, name, marketplace, created_at
                FROM shipments
                WHERE id = ? AND marketplace = ?
                """,
                [shipment_id, marketplace],
            ).fetchone()
            if not ship:
                return {"shipment": None, "orders": []}

            rows = conn.execute(
                """
                SELECT
                    o.*,
                    s.name AS shipment_name,
                    COALESCE(sc.orders_count, 0) AS shipment_orders_count
                FROM orders o
                LEFT JOIN shipments s ON s.id = o.shipment_id
                LEFT JOIN (
                    SELECT shipment_id, COUNT(*) AS orders_count
                    FROM orders
                    WHERE shipment_id IS NOT NULL
                    GROUP BY shipment_id
                ) sc ON sc.shipment_id = o.shipment_id
                WHERE o.shipment_id = ?
                  AND s.marketplace = ?
                ORDER BY o.created_at DESC
                """,
                [shipment_id, marketplace],
            ).fetchall()

            orders = [dict(r) for r in rows]
            for order in orders:
                items = conn.execute(
                    "SELECT * FROM order_items WHERE order_id = ? ORDER BY id",
                    (order["id"],),
                ).fetchall()
                order["items"] = [dict(i) for i in items]

                order["status_label"] = STATUS_LABELS.get(
                    order.get("status"), order.get("status") or "-"
                )
                order["created_at_display"] = _format_ru_short_datetime(order.get("created_at"))
                order["shipment_date_display"] = _format_ru_short_datetime(order.get("shipment_date"))
                tinfo = parse_shipment_tariff_from_raw(order.get("raw_json"))
                order["tariff_label"] = tinfo["label"]
                order["tariff_hint"] = tinfo["hint"]
                order["tariff_segment_active"] = tinfo["segment_active"]
                order["tariff_segment_count"] = tinfo["segment_count"]

            return {"shipment": dict(ship), "orders": orders}
        finally:
            conn.close()

    def add_orders_to_existing_shipment(
        self,
        shipment_id: int,
        order_ids: List[int],
        marketplace: str = "ozon",
    ) -> Dict[str, Any]:
        """
        Добавить выбранные заказы (из awaiting_packaging) в существующую поставку:
        1) вызываем Ozon ship для posting_number
        2) после успеха обновляем локально orders.shipment_id и статус awaiting_deliver
        """
        if not shipment_id:
            return {"ok": False, "error": "Не выбрана поставка"}

        # De-dupe and normalize ids
        ids: List[int] = []
        seen: set = set()
        for i in order_ids:
            try:
                v = int(i)
            except (TypeError, ValueError):
                continue
            if v not in seen:
                seen.add(v)
                ids.append(v)
        if not ids:
            return {"ok": False, "error": "Не выбрано ни одного заказа"}

        conn = get_db_connection()
        try:
            placeholders = ",".join("?" * len(ids))
            srow = conn.execute(
                "SELECT id, name FROM shipments WHERE id = ? AND marketplace = ?",
                [shipment_id, marketplace],
            ).fetchone()
            if not srow:
                return {"ok": False, "error": "Поставка не найдена"}

            eligible_rows = conn.execute(
                f"""
                SELECT id, posting_number
                FROM orders
                WHERE id IN ({placeholders})
                  AND status = ?
                """,
                [*ids, "awaiting_packaging"],
            ).fetchall()
            eligible_ids = [int(r["id"]) for r in eligible_rows]
            source_postings = [str(r["posting_number"] or "") for r in eligible_rows if str(r["posting_number"] or "")]

            # Ensure user action is explicit/consistent.
            if len(eligible_ids) != len(ids):
                return {
                    "ok": False,
                    "error": "Добавлять можно только заказы в статусе \"Ожидает сборки\"",
                }

            n_ozon = 0
            shipped_ids: List[int] = []
            failed_postings: List[str] = []
            for orow in eligible_rows:
                oid = int(orow["id"])
                posting_number = str(orow["posting_number"] or "")
                if not posting_number:
                    raise ValueError(f"Нет posting_number для order_id={oid}")

                items = conn.execute(
                    "SELECT sku, quantity FROM order_items WHERE order_id = ?",
                    (oid,),
                ).fetchall()

                items_dicts = [dict(x) for x in items]
                products = self._build_products_expanded_by_unit(items_dicts)

                if not products:
                    continue

                try:
                    for unit_product in products:
                        self.client.ship_fbs_posting(
                            posting_number=posting_number,
                            products=[unit_product],
                        )
                        n_ozon += 1
                    shipped_ids.append(oid)
                except OzonApiError:
                    # Skip only this posting; continue processing other selected orders.
                    self.logger.exception(
                        "Ozon ship failed for posting=%s while adding to shipment=%s",
                        posting_number,
                        shipment_id,
                    )
                    failed_postings.append(posting_number)
                    continue

            now = datetime.utcnow().isoformat()
            if shipped_ids:
                st_placeholders = ",".join("?" * len(shipped_ids))
                conn.execute(
                    f"""
                    UPDATE orders
                    SET shipment_id = ?,
                        status = ?,
                        updated_at = ?
                    WHERE id IN ({st_placeholders})
                    """,
                    [shipment_id, "awaiting_deliver", now, *shipped_ids],
                )
            conn.commit()
            if not shipped_ids:
                return {
                    "ok": False,
                    "error": "Ozon не принял ни один из выбранных заказов",
                    "shipment_id": shipment_id,
                    "shipment_name": srow["name"],
                    "count": 0,
                    "requested_count": len(eligible_ids),
                    "ozon_ship_requests": n_ozon,
                    "source_postings": source_postings,
                    "failed_postings": failed_postings,
                }
            return {
                "ok": bool(shipped_ids),
                "shipment_id": shipment_id,
                "shipment_name": srow["name"],
                "count": len(shipped_ids),
                "requested_count": len(eligible_ids),
                "ozon_ship_requests": n_ozon,
                "source_postings": source_postings,
                "failed_postings": failed_postings,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def create_shipment_with_orders(
        self,
        name: str,
        order_ids: List[int],
        marketplace: str = "ozon",
        ship_after: bool = False,
    ) -> Dict[str, Any]:
        clean = (name or "").strip()
        if not clean:
            return {"ok": False, "error": "Укажите название поставки"}
        ids: List[int] = []
        seen: set = set()
        for i in order_ids:
            if i is None:
                continue
            try:
                v = int(i)
            except (TypeError, ValueError):
                continue
            if v not in seen:
                seen.add(v)
                ids.append(v)
        if not ids:
            return {"ok": False, "error": "Не выбрано ни одного заказа"}
        now = datetime.utcnow().isoformat()
        conn = get_db_connection()
        try:
            placeholders = ",".join("?" * len(ids))
            found = conn.execute(
                f"SELECT COUNT(*) AS c FROM orders WHERE id IN ({placeholders})",
                ids,
            ).fetchone()["c"]
            if found != len(ids):
                return {"ok": False, "error": "Часть заказов не найдена в базе"}

            # Business rule:
            # Мы можем "перенести в Ожидает отгрузки" на стороне Ozon только когда заказ
            # находится в нашей "поставке" (internal shipment).
            #
            # Этот endpoint как раз и создает shipment и присваивает shipment_id заказам,
            # поэтому переносим статус только для тех заказов, которые в момент нажатия
            # находятся в "Ожидает сборки" (awaiting_packaging).
            eligible_for_status = [
                int(r["id"])
                for r in conn.execute(
                    f"SELECT id FROM orders WHERE id IN ({placeholders}) AND status = ?",
                    [*ids, "awaiting_packaging"],
                ).fetchall()
            ]

            cur = conn.execute(
                "INSERT INTO shipments (name, marketplace, created_at) VALUES (?, ?, ?)",
                (clean, marketplace, now),
            )
            shipment_id = cur.lastrowid
            conn.execute(
                f"""
                UPDATE orders SET shipment_id = ?
                WHERE id IN ({placeholders})
                """,
                [shipment_id] + ids,
            )

            n_shipped = 0
            shipped_ok_ids: List[int] = []
            failed_postings: List[str] = []
            if ship_after and eligible_for_status:
                # 1) Ship on Ozon for each eligible posting.
                eligible_ph = ",".join("?" * len(eligible_for_status))
                posting_rows = conn.execute(
                    f"""
                    SELECT id, posting_number
                    FROM orders
                    WHERE id IN ({eligible_ph})
                    AND status = ?
                    """,
                    [*eligible_for_status, "awaiting_packaging"],
                ).fetchall()

                source_postings = [str(r["posting_number"] or "") for r in posting_rows if str(r["posting_number"] or "")]
                for orow in posting_rows:
                    oid = int(orow["id"])
                    posting_number = str(orow["posting_number"] or "")
                    if not posting_number:
                        raise ValueError(f"Нет posting_number для order_id={oid}")

                    items = conn.execute(
                        "SELECT sku, quantity FROM order_items WHERE order_id = ?",
                        (oid,),
                    ).fetchall()

                    # Split by 1 unit (как "Разделить заказ" в кабинете):
                    # делаем список {product_id, quantity=1} с повторами по quantity.
                    items_dicts = [dict(x) for x in items]
                    products = self._build_products_expanded_by_unit(items_dicts)

                    if not products:
                        continue

                    # Split into unit-level shipments (like "Разделить заказ"),
                    # by shipping one unit at a time.
                    try:
                        for unit_product in products:
                            self.client.ship_fbs_posting(
                                posting_number=posting_number,
                                products=[unit_product],
                            )
                            n_shipped += 1
                        shipped_ok_ids.append(oid)
                    except OzonApiError:
                        self.logger.exception(
                            "Ozon ship failed for posting=%s while creating shipment=%s",
                            posting_number,
                            shipment_id,
                        )
                        failed_postings.append(posting_number)
                        continue

                # 2) After Ozon ship success, move local statuses.
                if shipped_ok_ids:
                    st_placeholders = ",".join("?" * len(shipped_ok_ids))
                    conn.execute(
                        f"""
                        UPDATE orders
                        SET status = ?
                        WHERE id IN ({st_placeholders})
                        """,
                        ["awaiting_deliver", *shipped_ok_ids],
                    )
                # Remove shipment link for failed ones so they can be retried cleanly.
                failed_ids = [oid for oid in eligible_for_status if oid not in set(shipped_ok_ids)]
                if failed_ids:
                    failed_ph = ",".join("?" * len(failed_ids))
                    conn.execute(
                        f"""
                        UPDATE orders
                        SET shipment_id = NULL
                        WHERE id IN ({failed_ph})
                        """,
                        [*failed_ids],
                    )
            elif eligible_for_status:
                # Without Ozon call (debug / old mode): just move status locally.
                st_placeholders = ",".join("?" * len(eligible_for_status))
                conn.execute(
                    f"""
                    UPDATE orders
                    SET status = ?
                    WHERE id IN ({st_placeholders})
                    """,
                    ["awaiting_deliver", *eligible_for_status],
                )

            conn.commit()
            if ship_after and not shipped_ok_ids and eligible_for_status:
                return {
                    "ok": False,
                    "error": "Ozon не принял ни один из выбранных заказов",
                    "shipment_id": shipment_id,
                    "name": clean,
                    "count": 0,
                    "requested_count": len(ids),
                    "status_moved_to_awaiting_deliver": 0,
                    "ozon_ship_requests": n_shipped,
                    "source_postings": source_postings if ship_after else [],
                    "failed_postings": failed_postings if ship_after else [],
                }
            return {
                "ok": bool(shipped_ok_ids or (not ship_after and ids)),
                "shipment_id": shipment_id,
                "name": clean,
                "count": len(shipped_ok_ids) if ship_after else len(ids),
                "requested_count": len(ids),
                "status_moved_to_awaiting_deliver": len(shipped_ok_ids) if ship_after else len(eligible_for_status),
                "ozon_ship_requests": n_shipped if ship_after else 0,
                "source_postings": source_postings if ship_after and eligible_for_status else [],
                "failed_postings": failed_postings if ship_after else [],
            }
        except sqlite3.IntegrityError:
            conn.rollback()
            return {"ok": False, "error": "Поставка с таким названием уже существует"}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def attach_split_children_to_shipment(
        self,
        shipment_id: int,
        source_postings: List[str],
        marketplace: str = "ozon",
    ) -> int:
        """
        После split на Ozon появляются новые postings (дети) с parent_posting_number.
        Привязываем такие локальные заказы к нашей shipment_id.
        """
        parents = {str(p or "").strip() for p in source_postings if str(p or "").strip()}
        if not shipment_id or not parents:
            return 0

        conn = get_db_connection()
        try:
            rows = conn.execute(
                """
                SELECT id, raw_json
                FROM orders
                WHERE marketplace = ?
                """,
                [marketplace],
            ).fetchall()

            matched_ids: List[int] = []
            for r in rows:
                raw = r["raw_json"]
                if not raw:
                    continue
                try:
                    payload = json.loads(raw)
                except Exception:
                    continue
                parent = str((payload or {}).get("parent_posting_number") or "").strip()
                if parent and parent in parents:
                    matched_ids.append(int(r["id"]))

            if not matched_ids:
                return 0

            ph = ",".join("?" * len(matched_ids))
            cur = conn.execute(
                f"""
                UPDATE orders
                SET shipment_id = ?,
                    status = CASE
                        WHEN status = 'awaiting_packaging' THEN 'awaiting_deliver'
                        ELSE status
                    END
                WHERE id IN ({ph})
                """,
                [shipment_id, *matched_ids],
            )
            conn.commit()
            return int(getattr(cur, "rowcount", 0) or 0)
        finally:
            conn.close()

    def get_summary(self) -> Dict[str, Any]:
        conn = get_db_connection()
        try:
            total = conn.execute(
                "SELECT COUNT(*) as cnt FROM orders WHERE marketplace = 'ozon'"
            ).fetchone()["cnt"]
            grouped = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM orders WHERE marketplace = 'ozon' GROUP BY status"
            ).fetchall()
            by_status = {row["status"] or "unknown": row["cnt"] for row in grouped}

            recent = conn.execute(
                """
                SELECT o.*, COALESCE(SUM(oi.quantity), 0) as total_qty
                FROM orders o
                LEFT JOIN order_items oi ON oi.order_id = o.id
                WHERE o.marketplace = 'ozon'
                GROUP BY o.id
                ORDER BY o.created_at DESC
                LIMIT 10
                """
            ).fetchall()
            return {"total": total, "by_status": by_status, "recent": [dict(r) for r in recent]}
        finally:
            conn.close()
