import logging
import os
import re
from typing import List

import fitz
from flask import Flask, flash, jsonify, redirect, render_template, request, send_file, url_for

from api_clients.ozon_client import OzonApiError
from config import Config
from database.db import init_db
from services.orders_service import OrdersService
from utils.helpers import parse_int


def setup_logging():
    os.makedirs(os.path.dirname(Config.LOG_PATH), exist_ok=True)
    logging.basicConfig(
        filename=Config.LOG_PATH,
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def create_app():
    setup_logging()
    init_db()

    app = Flask(__name__)
    app.secret_key = Config.FLASK_SECRET_KEY

    service = OrdersService()

    @app.get("/")
    def index():
        summary = service.get_summary()
        return render_template("index.html", summary=summary)

    @app.get("/orders")
    def orders():
        status = request.args.get("status", "all")
        date_from = request.args.get("date_from", "")
        date_to = request.args.get("date_to", "")
        query = request.args.get("q", "")

        per_page = Config.ORDERS_CHUNK_SIZE
        data = service.get_orders(
            status=status,
            date_from=date_from or None,
            date_to=date_to or None,
            query=query or None,
            page=1,
            per_page=per_page,
        )
        top_shipments = service.get_shipments_with_awaiting_deliver_orders()
        has_more = data["page"] < data["pages"]
        return render_template(
            "orders.html",
            data=data,
            top_shipments=top_shipments,
            filters={
                "status": status,
                "date_from": date_from,
                "date_to": date_to,
                "q": query,
            },
            has_more=has_more,
            next_page=2 if has_more else None,
        )

    @app.get("/orders/load-more")
    def orders_load_more():
        status = request.args.get("status", "all")
        date_from = request.args.get("date_from", "")
        date_to = request.args.get("date_to", "")
        query = request.args.get("q", "")
        page = parse_int(request.args.get("page"), 2, min_value=2)
        per_page = Config.ORDERS_CHUNK_SIZE

        data = service.get_orders(
            status=status,
            date_from=date_from or None,
            date_to=date_to or None,
            query=query or None,
            page=page,
            per_page=per_page,
        )
        html = render_template("partials/order_rows.html", orders=data["orders"])
        has_more = page < data["pages"]
        next_page = page + 1 if has_more else None
        return jsonify({"html": html, "has_more": has_more, "next_page": next_page})

    @app.post("/orders/update")
    def update_orders():
        status = request.form.get("status", "all")
        sync_scope = (request.form.get("sync_scope") or "active").strip()
        since = request.form.get("since", "")
        to = request.form.get("to", "")
        limit = parse_int(request.form.get("limit"), 100, min_value=1, max_value=200)
        sync_statuses = (
            ["awaiting_packaging", "awaiting_deliver", "delivering"]
            if sync_scope == "all"
            else ["awaiting_packaging", "awaiting_deliver"]
        )

        try:
            result = service.sync_from_ozon(
                status=status,
                statuses=sync_statuses,
                since=since or None,
                to=to or None,
                limit=limit,
                max_records=5000,
            )
            flash(
                f"Данные успешно обновлены. Создано: {result['created']}, обновлено: {result['updated']}.",
                "success",
            )
        except OzonApiError as exc:
            logging.exception("Ошибка Ozon API")
            flash(str(exc), "error")
        except Exception:
            logging.exception("Неожиданная ошибка при обновлении заказов")
            flash("Не удалось получить данные из Ozon API", "error")

        return redirect(url_for("orders"))

    @app.post("/orders/update-json")
    def update_orders_json():
        status = request.form.get("status", "all")
        sync_scope = (request.form.get("sync_scope") or "active").strip()
        since = request.form.get("since", "")
        to = request.form.get("to", "")
        limit = parse_int(request.form.get("limit"), 100, min_value=1, max_value=200)
        sync_statuses = (
            ["awaiting_packaging", "awaiting_deliver", "delivering"]
            if sync_scope == "all"
            else ["awaiting_packaging", "awaiting_deliver"]
        )

        try:
            result = service.sync_from_ozon(
                status=status,
                statuses=sync_statuses,
                since=since or None,
                to=to or None,
                limit=limit,
                max_records=5000,
            )
            return jsonify(
                {
                    "ok": True,
                    "scope": sync_scope,
                    "message": (
                        f"Готово. Создано: {result.get('created', 0)}, "
                        f"обновлено: {result.get('updated', 0)}, "
                        f"удалено: {result.get('deleted', 0)}."
                    ),
                    "result": result,
                }
            )
        except OzonApiError as exc:
            logging.exception("Ошибка Ozon API")
            return jsonify({"ok": False, "message": str(exc)}), 400
        except Exception:
            logging.exception("Неожиданная ошибка при обновлении заказов")
            return jsonify({"ok": False, "message": "Не удалось получить данные из Ozon API"}), 500

    @app.get("/orders/<int:order_id>/label.pdf")
    def order_label_pdf(order_id: int):
        rel = service.ensure_order_label_pdf_file(order_id)
        if not rel:
            flash("Нет этикетки: у позиций заказа нет штрихкода.", "error")
            return redirect(request.referrer or url_for("orders"))
        path = os.path.join(Config.BASE_DIR, rel.replace("/", os.sep))
        posting = service.get_order_posting_number(order_id) or str(order_id)
        safe = re.sub(r"[^\w\-.]+", "_", posting, flags=re.UNICODE)[:120]
        dl = f"etiketka_{safe}.pdf"
        return send_file(path, mimetype="application/pdf", as_attachment=True, download_name=dl)

    @app.get("/orders/<int:order_id>/ozon-label.pdf")
    def order_ozon_label_pdf(order_id: int):
        info = service.get_order_posting_and_status(order_id)
        if not info or not info.get("posting_number"):
            flash("Заказ не найден или нет posting_number.", "error")
            return redirect(request.referrer or url_for("orders"))
        if info.get("status") != "awaiting_deliver":
            flash("Этикетка Ozon доступна только для статуса «Ожидает отгрузки».", "error")
            return redirect(request.referrer or url_for("orders"))

        posting = info["posting_number"]
        try:
            pdf = service.client.get_fbs_package_label_pdf(posting)
        except OzonApiError as exc:
            logging.exception("Ошибка получения этикетки Ozon")
            flash(str(exc), "error")
            return redirect(request.referrer or url_for("orders"))

        # Some Ozon labels come in portrait orientation but are intended for landscape view.
        # Normalize to landscape for easier printing/scanning.
        try:
            doc = fitz.open(stream=pdf, filetype="pdf")
            changed = False
            for page in doc:
                rect = page.rect
                if rect.height > rect.width:
                    # Rotate counter-clockwise to keep text upright in landscape.
                    page.set_rotation((page.rotation + 270) % 360)
                    changed = True
            if changed:
                pdf = doc.tobytes()
            doc.close()
        except Exception:
            logging.exception("Не удалось нормализовать ориентацию этикетки Ozon")

        safe = re.sub(r"[^\w\-.]+", "_", posting, flags=re.UNICODE)[:120]
        dl = f"ozon_posting_label_{safe}.pdf"
        from io import BytesIO

        return send_file(
            BytesIO(pdf),
            mimetype="application/pdf",
            as_attachment=True,
            download_name=dl,
        )

    @app.get("/orders/shipment/next-name")
    def orders_shipment_next_name():
        name = service.suggest_next_shipment_name()
        return jsonify({"name": name})

    @app.get("/orders/shipment/available")
    def orders_shipment_available():
        shipments = service.get_shipments_available_for_awaiting_deliver()
        return jsonify({"shipments": shipments})

    @app.post("/orders/shipment/create")
    def orders_shipment_create():
        ids_raw = request.form.getlist("order_ids")
        next_url = request.form.get("next") or url_for("orders")
        if isinstance(next_url, str) and next_url.startswith("/") and not next_url.startswith("//"):
            pass
        else:
            next_url = url_for("orders")
        shipment_name = (request.form.get("shipment_name") or "").strip()
        order_ids = []
        for x in ids_raw:
            try:
                order_ids.append(int(x))
            except (TypeError, ValueError):
                continue
        try:
            result = service.create_shipment_with_orders(
                shipment_name,
                order_ids,
                ship_after=True,
            )
            if result.get("ok"):
                # Immediate reconciliation so split postings from Ozon appear locally right away.
                try:
                    service.sync_from_ozon(
                        status="all",
                        statuses=["awaiting_packaging", "awaiting_deliver"],
                        since=None,
                        to=None,
                        limit=100,
                        max_records=5000,
                    )
                except Exception:
                    logging.exception("Не удалось выполнить авто-синхронизацию после отгрузки")
                flash(
                    f"Поставка «{result['name']}» создана и отгружена, заказов: {result['count']}.",
                    "success",
                )
            else:
                flash(result.get("error") or "Не удалось создать поставку", "error")
        except OzonApiError as exc:
            logging.exception("Ошибка Ozon API при создании поставки/отгрузки")
            flash(str(exc), "error")
        return redirect(next_url)

    @app.post("/orders/shipment/add-existing")
    def orders_shipment_add_existing():
        ids_raw = request.form.getlist("order_ids")
        next_url = request.form.get("next") or url_for("orders")
        if isinstance(next_url, str) and next_url.startswith("/") and not next_url.startswith("//"):
            pass
        else:
            next_url = url_for("orders")

        shipment_id_raw = request.form.get("shipment_id")
        try:
            shipment_id = int(shipment_id_raw)
        except (TypeError, ValueError):
            flash("Не выбрана поставка", "error")
            return redirect(next_url)

        order_ids: List[int] = []
        for x in ids_raw:
            try:
                order_ids.append(int(x))
            except (TypeError, ValueError):
                continue

        try:
            result = service.add_orders_to_existing_shipment(
                shipment_id=shipment_id,
                order_ids=order_ids,
            )
            if result.get("ok"):
                # Immediate reconciliation so split postings from Ozon appear locally right away.
                try:
                    service.sync_from_ozon(
                        status="all",
                        statuses=["awaiting_packaging", "awaiting_deliver"],
                        since=None,
                        to=None,
                        limit=100,
                        max_records=5000,
                    )
                except Exception:
                    logging.exception("Не удалось выполнить авто-синхронизацию после добавления в поставку")
                flash(
                    f"Добавлено в поставку «{result.get('shipment_name') or shipment_id}», заказов: {result['count']}.",
                    "success",
                )
            else:
                flash(result.get("error") or "Не удалось добавить в поставку", "error")
        except OzonApiError as exc:
            logging.exception("Ошибка Ozon API при добавлении в существующую поставку")
            flash(str(exc), "error")

        return redirect(next_url)

    @app.get("/shipments")
    def shipments():
        return redirect(url_for("orders"))

    @app.get("/shipments/<int:shipment_id>")
    def shipment_detail(shipment_id: int):
        data = service.get_shipment_detail(shipment_id)
        shipment = data.get("shipment") or {}
        orders = data.get("orders") or []
        return render_template(
            "shipment_detail.html",
            shipment=shipment,
            orders=orders,
        )

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
