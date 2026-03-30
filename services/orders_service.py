import json
import logging
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from api_clients.ozon_client import OzonApiError, OzonClient
from config import Config
from database.db import get_db_connection
from utils.ozon_product_meta import (
    first_attribute_value,
    resolve_color_and_mfr_size_attr_ids,
)
from utils.label_pdf import build_label_pages_for_order_items, write_order_label_pdf
from utils.ozon_tariff import parse_shipment_tariff_from_raw

EXCLUDED_STATUSES = {"delivered", "cancelled"}
STATUS_LABELS = {
    "awaiting_packaging": "Ожидает сборки",
    "awaiting_deliver": "Ожидает отгрузки",
    "delivering": "Доставляется",
}

SHIPMENT_NAME_PREFIX = "OZON_"
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
            # Keep DB clean from statuses we do not use.
            conn.execute(
                "DELETE FROM order_items WHERE order_id IN (SELECT id FROM orders WHERE status IN ('delivered', 'cancelled'))"
            )
            conn.execute("DELETE FROM orders WHERE status IN ('delivered', 'cancelled')")

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
                    WHERE status IN ({st_ph})
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
                incoming_status == "awaiting_packaging"
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

    def _sync_order_label_pdf(self, conn: sqlite3.Connection, order_id: int) -> None:
        row = conn.execute(
            "SELECT label_pdf_path FROM orders WHERE id = ?",
            (order_id,),
        ).fetchone()
        old = (row["label_pdf_path"] or "").strip() if row else ""
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
        rel = write_order_label_pdf(order_id, pages)
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
                row2 = conn.execute(
                    "SELECT label_pdf_path FROM orders WHERE id = ?",
                    (oid,),
                ).fetchone()
                if row2 and (row2["label_pdf_path"] or "").strip():
                    n_with_pdf += 1
            conn.commit()
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
                "SELECT id, label_pdf_path FROM orders WHERE id = ?",
                (order_id,),
            ).fetchone()
            if not row:
                return None
            rel = (row["label_pdf_path"] or "").strip()
            path = os.path.join(Config.BASE_DIR, rel.replace("/", os.sep)) if rel else ""
            if rel and os.path.isfile(path):
                return rel
            self._sync_order_label_pdf(conn, order_id)
            conn.commit()
            row2 = conn.execute(
                "SELECT label_pdf_path FROM orders WHERE id = ?",
                (order_id,),
            ).fetchone()
            return ((row2["label_pdf_path"] or "").strip() or None) if row2 else None
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
    ) -> Dict[str, Any]:
        conn = get_db_connection()
        try:
            where = ["1=1"]
            params: List[Any] = []

            if status and status != "all":
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
                "SELECT status, COUNT(*) as cnt FROM orders GROUP BY status ORDER BY cnt DESC"
            ).fetchall()
            total_all = conn.execute("SELECT COUNT(*) as cnt FROM orders").fetchone()["cnt"]
            status_options = [
                {"value": s["status"], "label": STATUS_LABELS.get(s["status"], s["status"] or "-")}
                for s in statuses
                if s["status"]
            ]
            status_counts = {str(s["status"]): int(s["cnt"]) for s in statuses if s["status"]}
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
                order["status_label"] = STATUS_LABELS.get(order.get("status"), order.get("status") or "-")
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
            total = conn.execute("SELECT COUNT(*) as cnt FROM orders").fetchone()["cnt"]
            grouped = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM orders GROUP BY status"
            ).fetchall()
            by_status = {row["status"] or "unknown": row["cnt"] for row in grouped}

            recent = conn.execute(
                """
                SELECT o.*, COALESCE(SUM(oi.quantity), 0) as total_qty
                FROM orders o
                LEFT JOIN order_items oi ON oi.order_id = o.id
                GROUP BY o.id
                ORDER BY o.created_at DESC
                LIMIT 10
                """
            ).fetchall()
            return {"total": total, "by_status": by_status, "recent": [dict(r) for r in recent]}
        finally:
            conn.close()
