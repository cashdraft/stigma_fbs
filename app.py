import logging
import os

from flask import Flask, flash, jsonify, redirect, render_template, request, url_for

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

    @app.post("/orders/assemble")
    def assemble_orders():
        ids = request.form.getlist("order_ids")
        next_url = request.form.get("next") or url_for("orders")
        if isinstance(next_url, str) and next_url.startswith("/") and not next_url.startswith("//"):
            pass
        else:
            next_url = url_for("orders")
        if not ids:
            flash("Не выбрано ни одного заказа", "error")
            return redirect(next_url)
        logging.info("Сборка заказов (заглушка): %s шт., id=%s", len(ids), ids[:20])
        flash(f"Выбрано для сборки: {len(ids)} заказов (действие будет подключено к Ozon API)", "success")
        return redirect(next_url)

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
