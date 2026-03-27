import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from api_clients.ozon_client import OzonClient
from database.db import get_db_connection
from utils.ozon_product_meta import (
    first_attribute_value,
    resolve_color_and_mfr_size_attr_ids,
)
from utils.ozon_tariff import parse_shipment_tariff_from_raw

EXCLUDED_STATUSES = {"delivered", "cancelled"}
STATUS_LABELS = {
    "awaiting_packaging": "Ожидает сборки",
    "awaiting_deliver": "Ожидает отгрузки",
    "delivering": "Доставляется",
}


class OrdersService:
    def __init__(self) -> None:
        self.client = OzonClient()
        self.logger = logging.getLogger(__name__)

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
        since: Optional[str] = None,
        to: Optional[str] = None,
        limit: int = 100,
        max_records: int = 5000,
    ) -> Dict[str, int]:
        postings: List[Dict[str, Any]] = []
        offset = 0
        batch_size = max(1, min(limit, 1000))

        while True:
            page = self.client.get_fbs_postings(
                status=status,
                since=since,
                to=to,
                limit=batch_size,
                offset=offset,
            )
            if not page:
                break

            filtered_page = [p for p in page if p.get("status") not in EXCLUDED_STATUSES]
            postings.extend(filtered_page)
            if len(postings) >= max_records:
                postings = postings[:max_records]
                break

            # If page is not full, there are no more records.
            if len(page) < batch_size:
                break

            offset += batch_size

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
                if existed:
                    updated += 1
                else:
                    created += 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        self.logger.info("Синхронизация заказов завершена: created=%s updated=%s", created, updated)
        return {"created": created, "updated": updated, "total": len(postings)}

    def _upsert_order(self, conn, order: Dict[str, Any]) -> Tuple[bool, int]:
        now = datetime.utcnow().isoformat()
        row = conn.execute(
            "SELECT id FROM orders WHERE marketplace = ? AND posting_number = ?",
            (order["marketplace"], order["posting_number"]),
        ).fetchone()

        if row:
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
                    COALESCE(SUM(oi.quantity), 0) as total_qty
                FROM orders o
                LEFT JOIN order_items oi ON oi.order_id = o.id
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
            status_list = [s["status"] for s in statuses if s["status"]]

            orders = [dict(r) for r in rows]
            for order in orders:
                items = conn.execute(
                    "SELECT * FROM order_items WHERE order_id = ?",
                    (order["id"],),
                ).fetchall()
                order["items"] = [dict(i) for i in items]
                order["status_label"] = STATUS_LABELS.get(order.get("status"), order.get("status") or "-")
                tinfo = parse_shipment_tariff_from_raw(order.get("raw_json"))
                order["tariff_label"] = tinfo["label"]
                order["tariff_hint"] = tinfo["hint"]
                order["tariff_segment_active"] = tinfo["segment_active"]
                order["tariff_segment_count"] = tinfo["segment_count"]

            return {
                "orders": orders,
                "statuses": status_list,
                "total": total,
                "page": page,
                "per_page": per_page,
                "pages": max(1, (total + per_page - 1) // per_page),
            }
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
