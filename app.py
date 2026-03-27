import logging
import os
import re

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
        has_more = data["page"] < data["pages"]
        return render_template(
            "orders.html",
            data=data,
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
        since = request.form.get("since", "")
        to = request.form.get("to", "")
        limit = parse_int(request.form.get("limit"), 100, min_value=1, max_value=200)

        try:
            result = service.sync_from_ozon(
                status=status,
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

    @app.get("/orders/shipment/next-name")
    def orders_shipment_next_name():
        name = service.suggest_next_shipment_name()
        return jsonify({"name": name})

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
        result = service.create_shipment_with_orders(shipment_name, order_ids)
        if result.get("ok"):
            flash(
                f"Поставка «{result['name']}» создана, заказов: {result['count']}.",
                "success",
            )
        else:
            flash(result.get("error") or "Не удалось создать поставку", "error")
        return redirect(next_url)

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
