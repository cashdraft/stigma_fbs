import logging
import os
import re
import time
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

    def _sync_active_orders_with_retry() -> None:
        """
        Ozon может отдавать старый статус сразу после ship-вызова.
        Делаем несколько коротких попыток синхронизации 2 активных статусов.
        """
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                service.sync_from_ozon(
                    statuses=["awaiting_packaging", "awaiting_deliver"],
                    since=None,
                    to=None,
                    limit=100,
                    max_records=5000,
                )
                return
            except Exception as exc:
                last_exc = exc
                logging.exception(
                    "Авто-синхронизация после ship не удалась (попытка %s/3)",
                    attempt + 1,
                )
                if attempt < 2:
                    time.sleep(1.2)
        if last_exc:
            raise last_exc

    def _normalize_ozon_label_orientation(pdf: bytes) -> bytes:
        try:
            doc = fitz.open(stream=pdf, filetype="pdf")
            changed = False
            for page in doc:
                rect = page.rect
                if rect.height > rect.width:
                    page.set_rotation((page.rotation + 270) % 360)
                    changed = True
            if changed:
                pdf = doc.tobytes()
            doc.close()
        except Exception:
            logging.exception("Не удалось нормализовать ориентацию этикетки Ozon")
        return pdf

    def _normalize_pick_article(raw_offer_id: str) -> str:
        """
        Примеры:
        BASE_3_1_697_B_M  -> 3_1_697_B
        BASE_3_1_697_BB_M -> 3_1_697_B
        """
        s = str(raw_offer_id or "").strip()
        if not s:
            return "—"
        def _latinize_color(c: str) -> str:
            t = str(c or "").upper()[:1]
            return (
                t.replace("А", "A")
                .replace("В", "B")
                .replace("Е", "E")
                .replace("К", "K")
                .replace("М", "M")
                .replace("Н", "H")
                .replace("О", "O")
                .replace("Р", "P")
                .replace("С", "C")
                .replace("Т", "T")
                .replace("У", "Y")
                .replace("Х", "X")
            )

        m = re.search(r"^[^_]+_(\d+)_(\d+)_(\d+)_([A-Za-zА-Яа-я])", s)
        if m:
            return f"{m.group(1)}_{m.group(2)}_{m.group(3)}_{_latinize_color(m.group(4))}"
        parts = s.split("_")
        if len(parts) >= 5:
            tail = _latinize_color(parts[4] if parts[4] else "X")
            return f"{parts[1]}_{parts[2]}_{parts[3]}_{tail}"
        return s

    @app.get("/")
    def index():
        summary = service.get_summary()
        return render_template("index.html", summary=summary)

    @app.get("/logo.png")
    def logo_png():
        return send_file(os.path.join(app.root_path, "logo.png"), mimetype="image/png")

    @app.get("/icon-ozon.png")
    def icon_ozon_png():
        return send_file(os.path.join(app.root_path, "icon-ozon.png"), mimetype="image/png")

    @app.get("/orders")
    @app.get("/orders_ozon")
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

    @app.get("/orders_wb")
    def orders_wb():
        return render_template("orders_wb.html")

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

        pdf = _normalize_ozon_label_orientation(pdf)

        safe = re.sub(r"[^\w\-.]+", "_", posting, flags=re.UNICODE)[:120]
        dl = f"ozon_posting_label_{safe}.pdf"
        from io import BytesIO

        return send_file(
            BytesIO(pdf),
            mimetype="application/pdf",
            as_attachment=True,
            download_name=dl,
        )

    @app.get("/shipments/<int:shipment_id>/orders-tape.pdf")
    def shipment_orders_tape_pdf(shipment_id: int):
        data = service.get_shipment_detail(shipment_id)
        shipment = data.get("shipment") or {}
        orders = data.get("orders") or []
        if not shipment or not orders:
            flash("Поставка не найдена или в ней нет заказов.", "error")
            return redirect(url_for("orders"))

        out_doc = fitz.open()
        try:
            prepared: List[dict] = []
            issues: List[str] = []

            # Preflight: verify that BOTH labels are available for EACH order.
            for order in orders:
                order_id = int(order.get("id") or 0)
                posting = str(order.get("posting_number") or "")
                if not order_id or not posting:
                    issues.append(f"Некорректный заказ в поставке (id={order.get('id')}).")
                    continue

                rel = service.ensure_order_label_pdf_file(order_id)
                if not rel:
                    issues.append(f"{posting}: нет локальной этикетки (ШК).")
                    continue
                local_path = os.path.join(Config.BASE_DIR, rel.replace("/", os.sep))
                if not os.path.isfile(local_path):
                    issues.append(f"{posting}: файл локальной этикетки не найден.")
                    continue

                try:
                    ozon_pdf = service.client.get_fbs_package_label_pdf(posting)
                    ozon_pdf = _normalize_ozon_label_orientation(ozon_pdf)
                except Exception:
                    issues.append(f"{posting}: не удалось получить этикетку Ozon.")
                    continue

                prepared.append(
                    {
                        "posting": posting,
                        "local_path": local_path,
                        "ozon_pdf": ozon_pdf,
                    }
                )

            if issues:
                preview = "; ".join(issues[:5])
                tail = f" (и еще {len(issues) - 5})" if len(issues) > 5 else ""
                flash(
                    "Лента заказов не сформирована: не все этикетки доступны. "
                    f"Проблемы: {preview}{tail}",
                    "error",
                )
                return redirect(url_for("shipment_detail", shipment_id=shipment_id))

            # Build final tape only after all labels are pre-validated.
            for entry in prepared:
                local_doc = fitz.open(entry["local_path"])
                out_doc.insert_pdf(local_doc)
                local_doc.close()

                ozon_doc = fitz.open(stream=entry["ozon_pdf"], filetype="pdf")
                out_doc.insert_pdf(ozon_doc)
                ozon_doc.close()

            if out_doc.page_count == 0:
                flash("Не удалось собрать ленту заказов: нет подходящих этикеток.", "error")
                return redirect(url_for("shipment_detail", shipment_id=shipment_id))

            from io import BytesIO

            pdf_bytes = out_doc.tobytes()
            safe_name = re.sub(r"[^\w\-.]+", "_", str(shipment.get("name") or shipment_id), flags=re.UNICODE)[:120]
            filename = f"lenta_zakazov_{safe_name}.pdf"
            return send_file(
                BytesIO(pdf_bytes),
                mimetype="application/pdf",
                as_attachment=True,
                download_name=filename,
            )
        except OzonApiError as exc:
            logging.exception("Ошибка Ozon API при сборке ленты заказов")
            flash(str(exc), "error")
            return redirect(url_for("shipment_detail", shipment_id=shipment_id))
        except Exception as exc:
            logging.exception("Ошибка при сборке ленты заказов")
            flash(str(exc) or "Не удалось сформировать ленту заказов.", "error")
            return redirect(url_for("shipment_detail", shipment_id=shipment_id))
        finally:
            out_doc.close()

    @app.get("/shipments/<int:shipment_id>/picklist.xlsx")
    def shipment_picklist_xlsx(shipment_id: int):
        data = service.get_shipment_detail(shipment_id)
        shipment = data.get("shipment") or {}
        orders = data.get("orders") or []
        if not shipment or not orders:
            flash("Поставка не найдена или в ней нет заказов.", "error")
            return redirect(url_for("orders"))

        try:
            from openpyxl import Workbook
            from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
            from io import BytesIO

            agg: dict[str, int] = {}
            for order in orders:
                for item in (order.get("items") or []):
                    offer_id = str(item.get("offer_id") or item.get("sku") or "").strip()
                    norm = _normalize_pick_article(offer_id)
                    qty = int(item.get("quantity") or 0)
                    if qty <= 0:
                        qty = 1
                    agg[norm] = agg.get(norm, 0) + qty

            wb = Workbook()
            ws_print = wb.active
            ws_print.title = "Печать"
            ws_print.append(["Артикул", "Количество"])
            total_qty = 0
            for article, qty in sorted(agg.items(), key=lambda x: x[0]):
                ws_print.append([article, qty])
                total_qty += int(qty or 0)
            ws_print.append(["Итого", total_qty])

            ws_print.column_dimensions["A"].width = 28
            ws_print.column_dimensions["B"].width = 14

            thin = Side(style="thin", color="000000")
            all_border = Border(left=thin, right=thin, top=thin, bottom=thin)
            base_font = Font(name="Calibri", size=18)
            bold_font = Font(name="Calibri", size=18, bold=True)

            max_row = ws_print.max_row
            for row in ws_print.iter_rows(min_row=1, max_row=max_row, min_col=1, max_col=2):
                for c in row:
                    c.font = base_font
                    c.border = all_border
                    c.alignment = Alignment(vertical="center")

            for c in ws_print[1]:
                c.font = bold_font
                c.alignment = Alignment(horizontal="center", vertical="center")

            for r in range(2, max_row + 1):
                ws_print.cell(row=r, column=1).alignment = Alignment(horizontal="left", vertical="center")
                ws_print.cell(row=r, column=2).alignment = Alignment(horizontal="center", vertical="center")

            ws_print.cell(row=max_row, column=1).font = bold_font
            ws_print.cell(row=max_row, column=2).font = bold_font

            # Sheet 2: "Сборка" (pivot-like by brand-prefix -> color-letter -> size)
            ws_pick = wb.create_sheet("Сборка")
            shipment_title = f"Поставка: {shipment.get('name') or shipment_id}"
            ws_pick.append([shipment_title, ""])
            ws_pick.append(["Названия строк", "Количество"])
            ws_pick.column_dimensions["A"].width = 34
            ws_pick.column_dimensions["B"].width = 16

            def _parse_pick_keys(offer_id: str, size_raw: str) -> tuple[str, str, str]:
                s = str(offer_id or "").strip()
                prefix = (s.split("_", 1)[0] if "_" in s else s) or "—"
                parts = s.split("_")
                color = "X"
                size = str(size_raw or "").strip()

                def _latinize_color(c: str) -> str:
                    t = str(c or "").upper()[:1]
                    return (
                        t.replace("А", "A")
                        .replace("В", "B")
                        .replace("Е", "E")
                        .replace("К", "K")
                        .replace("М", "M")
                        .replace("Н", "H")
                        .replace("О", "O")
                        .replace("Р", "P")
                        .replace("С", "C")
                        .replace("Т", "T")
                        .replace("У", "Y")
                        .replace("Х", "X")
                    )

                # Expected forms:
                # BASE_6_2_104_B_S
                # L_3_1_44_BS
                # L_3_1_7_WM
                # L_3_1_58_ВM
                # BASE_3_1_697_BB_M
                if len(parts) >= 5:
                    tail = parts[4] or ""
                    color = _latinize_color(tail[:1] if tail else "X")
                    if len(parts) >= 6 and parts[5]:
                        size = parts[5]
                    elif len(tail) > 1:
                        size = tail[1:]

                if not size:
                    size = parts[-1] if parts else "—"
                return prefix, color, size.upper()

            tree: dict[str, dict[str, dict[str, int]]] = {}
            for order in orders:
                for item in (order.get("items") or []):
                    offer_id = str(item.get("offer_id") or item.get("sku") or "").strip()
                    qty = int(item.get("quantity") or 0)
                    if qty <= 0:
                        qty = 1
                    prefix, color, size = _parse_pick_keys(offer_id, str(item.get("manufacturer_size") or ""))
                    tree.setdefault(prefix, {}).setdefault(color, {})
                    tree[prefix][color][size] = tree[prefix][color].get(size, 0) + qty

            ws_pick["A1"].font = bold_font
            ws_pick["A1"].alignment = Alignment(horizontal="left", vertical="center")
            ws_pick.merge_cells("A1:B1")
            ws_pick["A2"].font = bold_font
            ws_pick["B2"].font = bold_font
            ws_pick["A2"].alignment = Alignment(horizontal="left", vertical="center")
            ws_pick["B2"].alignment = Alignment(horizontal="center", vertical="center")
            fill_header = PatternFill(fill_type="solid", fgColor="D9E1F2")
            fill_level_1 = PatternFill(fill_type="solid", fgColor="E2F0D9")
            fill_level_2 = PatternFill(fill_type="solid", fgColor="FCE4D6")
            for c in ws_pick[2]:
                c.fill = fill_header

            def _size_sort_key(sz: str) -> tuple[int, str]:
                order_map = {
                    "XS": 1,
                    "S": 2,
                    "M": 3,
                    "L": 4,
                    "XL": 5,
                    "XXL": 6,
                    "XXXL": 7,
                    "4XL": 8,
                    "5XL": 9,
                    "6XL": 10,
                }
                return (order_map.get(sz.upper(), 100), sz)

            grand_total = 0
            for prefix in sorted(tree.keys()):
                prefix_total = sum(
                    qty
                    for c in tree[prefix].values()
                    for qty in c.values()
                )
                grand_total += prefix_total
                ws_pick.append([prefix, prefix_total])
                pr = ws_pick.max_row
                ws_pick[f"A{pr}"].font = bold_font
                ws_pick[f"B{pr}"].font = bold_font
                ws_pick[f"A{pr}"].fill = fill_level_1
                ws_pick[f"B{pr}"].fill = fill_level_1

                for color in sorted(tree[prefix].keys()):
                    color_total = sum(tree[prefix][color].values())
                    ws_pick.append([f"  Цвет-{color}", color_total])
                    cr = ws_pick.max_row
                    ws_pick[f"A{cr}"].font = bold_font
                    ws_pick[f"B{cr}"].font = bold_font
                    ws_pick[f"A{cr}"].fill = fill_level_2
                    ws_pick[f"B{cr}"].fill = fill_level_2

                    for size in sorted(tree[prefix][color].keys(), key=_size_sort_key):
                        ws_pick.append([f"    {size}", tree[prefix][color][size]])

            ws_pick.append(["Итого", grand_total])
            tr = ws_pick.max_row
            ws_pick[f"A{tr}"].font = bold_font
            ws_pick[f"B{tr}"].font = bold_font
            ws_pick[f"A{tr}"].fill = fill_header
            ws_pick[f"B{tr}"].fill = fill_header

            for row in ws_pick.iter_rows(min_row=2, max_row=ws_pick.max_row, min_col=1, max_col=2):
                for c in row:
                    if c.font != bold_font:
                        c.font = base_font
                    c.border = all_border
                    c.alignment = Alignment(vertical="center")
            for r in range(2, ws_pick.max_row + 1):
                ws_pick.cell(row=r, column=2).alignment = Alignment(horizontal="right", vertical="center")

            bio = BytesIO()
            wb.save(bio)
            bio.seek(0)
            safe_name = re.sub(r"[^\w\-.]+", "_", str(shipment.get("name") or shipment_id), flags=re.UNICODE)[:120]
            filename = f"fayl_podbora_{safe_name}.xlsx"
            return send_file(
                bio,
                mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                as_attachment=True,
                download_name=filename,
            )
        except Exception:
            logging.exception("Не удалось сформировать файл подбора")
            flash("Не удалось сформировать файл подбора.", "error")
            return redirect(url_for("shipment_detail", shipment_id=shipment_id))

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
        is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
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
                # Always refresh active Ozon statuses right after create/ship:
                # awaiting_packaging + awaiting_deliver (independent of split/non-split flow).
                try:
                    _sync_active_orders_with_retry()
                    service.attach_split_children_to_shipment(
                        shipment_id=int(result.get("shipment_id") or 0),
                        source_postings=result.get("source_postings") or [],
                    )
                except Exception:
                    logging.exception("Не удалось выполнить авто-синхронизацию после отгрузки")
                    msg = "Поставка создана, но авто-обновление заказов не удалось. Нажмите «Обновить заказы»."
                    if is_ajax:
                        return jsonify({"ok": False, "message": msg, "next_url": next_url}), 500
                    flash(msg, "error")
                success_msg = f"Поставка «{result['name']}» создана и отгружена, заказов: {result['count']}."
                if is_ajax:
                    return jsonify({"ok": True, "message": success_msg, "next_url": next_url})
                flash(success_msg, "success")
            else:
                err = result.get("error") or "Не удалось создать поставку"
                if is_ajax:
                    return jsonify({"ok": False, "message": err, "next_url": next_url}), 400
                flash(err, "error")
        except OzonApiError as exc:
            logging.exception("Ошибка Ozon API при создании поставки/отгрузки")
            if is_ajax:
                return jsonify({"ok": False, "message": str(exc), "next_url": next_url}), 500
            flash(str(exc), "error")
        return redirect(next_url)

    @app.post("/orders/shipment/add-existing")
    def orders_shipment_add_existing():
        is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
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
            if is_ajax:
                return jsonify({"ok": False, "message": "Не выбрана поставка", "next_url": next_url}), 400
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
                # Always refresh active Ozon statuses right after add/ship:
                # awaiting_packaging + awaiting_deliver (independent of split/non-split flow).
                try:
                    _sync_active_orders_with_retry()
                    service.attach_split_children_to_shipment(
                        shipment_id=int(result.get("shipment_id") or shipment_id),
                        source_postings=result.get("source_postings") or [],
                    )
                except Exception:
                    logging.exception("Не удалось выполнить авто-синхронизацию после добавления в поставку")
                    msg = "Заказы добавлены в поставку, но авто-обновление не удалось. Нажмите «Обновить заказы»."
                    if is_ajax:
                        return jsonify({"ok": False, "message": msg, "next_url": next_url}), 500
                    flash(msg, "error")
                success_msg = f"Добавлено в поставку «{result.get('shipment_name') or shipment_id}», заказов: {result['count']}."
                if is_ajax:
                    return jsonify({"ok": True, "message": success_msg, "next_url": next_url})
                flash(success_msg, "success")
            else:
                err = result.get("error") or "Не удалось добавить в поставку"
                if is_ajax:
                    return jsonify({"ok": False, "message": err, "next_url": next_url}), 400
                flash(err, "error")
        except OzonApiError as exc:
            logging.exception("Ошибка Ozon API при добавлении в существующую поставку")
            if is_ajax:
                return jsonify({"ok": False, "message": str(exc), "next_url": next_url}), 500
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
