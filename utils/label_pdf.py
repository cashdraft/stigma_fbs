"""PDF этикеток FBS: 1:1 подложка из эталонного PDF + подстановка полей.

Берётся страница LABEL_TEMPLATE_PDF (по умолчанию print_2026_03_25_21_41.pdf):
сохраняются «Артикул:», «Размер:», строка из дефисов и подчёркивания.
Растр EAC берётся из того же шаблонного PDF (та же картинка), старый экземпляр замазывается и вставляется снова,
по вертикали по центру полос штрихкода. Заголовок: «категория / STIGMA».
"""

from __future__ import annotations

import io
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import pymupdf as fitz

from config import Config

# Маски PyMuPDF (y вниз), с небольшим запасом
_REDACT_TITLE = fitz.Rect(105, 6, 506, 42)
_REDACT_DASH = fitz.Rect(29, 43, 581, 64)
_REDACT_ARTICLE_VAL = fitz.Rect(118, 64, 356, 102)
_REDACT_SIZE_VAL = fitz.Rect(110, 116, 272, 154)
_REDACT_SELLER = fitz.Rect(386, 124, 567, 150)
_REDACT_COMP_COUNTRY = fitz.Rect(28, 172, 567, 200)
_REDACT_UNDERSCORES = fitz.Rect(31, 187, 579, 214)
_REDACT_BARCODE = fitz.Rect(28, 216, 456, 368)
# Растровый EAC шаблона + запас под новую позицию (центр по штрихкоду)
_REDACT_EAC = fitz.Rect(454, 212, 556, 328)

# Нижний край bbox текста в шаблоне (PyMuPDF) → insert_text baseline = y1 - |descender|*fs
_TITLE_Y1 = 40.515625
_TITLE_CENTER_X = 305.0
_DASH_Y1 = 62.390625
_DASH_X0 = 30.6015625
_ART_VAL_X = 120.0
_ART_VAL_Y1 = 99.90625
_SZ_VAL_X = 112.0
_SZ_VAL_Y1 = 151.765625
_SELLER_RIGHT = 565.0151977539062
_SELLER_Y1 = 148.453125
_COMP_X = 30.0
_COMP_Y1 = 198.453125
_COUNTRY_RIGHT = 565.0151977539062
_COUNTRY_Y1 = 196.796875
_UNDER_X = 32.4853515625
_UNDER_Y1 = 211.796875
# Шире, чем в образце Ozon (~до 408): до ~12 pt до растрового EAC (x=460).
_BARCODE_RECT = fitz.Rect(32, 220.796875, 452, 334.796875)
_DIGITS_Y1 = 365.0768737792969
# Было 30 pt bold; просили ×1.3 меньше и обычный вес.
_BARCODE_DIGITS_FS = 30.0 / 1.3
_EAC_IMG_W_PT = 90.0
_EAC_IMG_H_PT = 90.0
_EAC_IMG_LEFT_X = 460.0

_BC_RENDER_OPTS: dict[str, Any] = {
    "write_text": False,
    "module_width": 0.22,
    "module_height": 24.0,
    "quiet_zone": 2.0,
    "font_size": 0,
    "text": "",
    "human": "",
    "text_distance": 0,
    "background": "white",
    "foreground": "black",
}
_BARCODE_IMAGE_DPI = 300


def _template_path() -> str:
    p = (getattr(Config, "LABEL_TEMPLATE_PDF", None) or "").strip()
    if not p:
        return os.path.join(Config.BASE_DIR, "print_2026_03_25_21_41.pdf")
    if os.path.isfile(p):
        return p
    joined = os.path.join(Config.BASE_DIR, p)
    return joined if os.path.isfile(joined) else p


def _template_page_index() -> int:
    try:
        return int(getattr(Config, "LABEL_TEMPLATE_PAGE", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _font_paths() -> Tuple[str, str]:
    reg = getattr(Config, "LABEL_FONT_PATH", "") or ""
    bold = reg.replace("DejaVuSans.ttf", "DejaVuSans-Bold.ttf") if reg else ""
    if reg and os.path.isfile(reg):
        if not bold or not os.path.isfile(bold):
            bold = reg
        return reg, bold
    raise FileNotFoundError(
        "Нужен TTF для кириллицы (LABEL_FONT_PATH), например DejaVuSans.ttf"
    )


def _baseline_y1(face: fitz.Font, fs: float, y1_pm: float) -> float:
    return y1_pm - abs(face.descender) * fs


def _truncate_for_font(face: fitz.Font, text: str, fs: float, max_w: float) -> str:
    if max_w <= 8:
        return "…"
    if face.text_length(text, fontsize=fs) <= max_w:
        return text
    ell = "…"
    t = text
    while t and face.text_length(t + ell, fontsize=fs) > max_w:
        t = t[:-1]
    return (t + ell) if t else ell


def _fit_line(face: fitz.Font, text: str, fs0: float, max_w: float) -> Tuple[str, float]:
    """Подобрать размер шрифта (не ниже 14), чтобы строка помещалась без обрезки."""
    fs = fs0
    while fs >= 14 and face.text_length(text, fontsize=fs) > max_w:
        fs -= 0.5
    return text, fs


def _hyphen_rule(face: fitz.Font, fs: float, max_w: float) -> str:
    ch = "-"
    s = ch
    while face.text_length(s + ch, fontsize=fs) < max_w:
        s += ch
    return s


def _extract_eac_raster_from_template(doc: fitz.Document, page_index: int) -> Optional[bytes]:
    """PNG/JPEG поток картинки EAC справа на странице-шаблоне (как в Ozon print)."""
    page = doc.load_page(page_index)
    for info in page.get_images(full=True):
        xref = info[0]
        for r in page.get_image_rects(xref):
            if r.x0 >= 455 and r.width >= 70:
                blob = doc.extract_image(xref)
                return blob.get("image")
    return None


def _eac_rect_centered_on_barcode(barcode_rect: fitz.Rect) -> fitz.Rect:
    x0 = _EAC_IMG_LEFT_X
    w, h = _EAC_IMG_W_PT, _EAC_IMG_H_PT
    mid_y = (barcode_rect.y0 + barcode_rect.y1) / 2.0
    y0 = mid_y - h / 2.0
    return fitz.Rect(x0, y0, x0 + w, y0 + h)


def _bc_png(barcode_raw: str) -> Tuple[Optional[io.BytesIO], str]:
    from barcode import Code128
    from barcode.writer import ImageWriter

    digits = re.sub(r"\D", "", barcode_raw or "")
    display = (barcode_raw or "").strip() or digits
    writer = ImageWriter(format="PNG", mode="RGB", dpi=_BARCODE_IMAGE_DPI)
    payload = digits if digits else (barcode_raw or "").strip()
    if not payload:
        return None, display
    try:
        if len(digits) == 13:
            from barcode import EAN13

            code = EAN13(digits, writer=writer)
        elif len(digits) == 12:
            from barcode import EAN13

            code = EAN13(digits, writer=writer)
        elif len(digits) == 8:
            from barcode import EAN8

            code = EAN8(digits, writer=writer)
        else:
            code = Code128(payload, writer=writer)
        buf = io.BytesIO()
        code.write(buf, options=_BC_RENDER_OPTS)
        buf.seek(0)
        return buf, display
    except Exception:
        try:
            buf = io.BytesIO()
            Code128(payload, writer=writer).write(buf, options=_BC_RENDER_OPTS)
            buf.seek(0)
            return buf, display
        except Exception:
            return None, display


def write_order_label_pdf(
    order_id: int,
    items: List[Dict[str, Any]],
    *,
    target_page_pt: Optional[Tuple[float, float]] = None,
    target_fit_letterbox: bool = False,
) -> str:
    os.makedirs(Config.LABELS_DIR, exist_ok=True)
    rel = os.path.join("instance", "labels", f"order_{order_id}.pdf")
    out_path = os.path.join(Config.BASE_DIR, rel.replace("\\", "/"))

    tpl_path = _template_path()
    if not os.path.isfile(tpl_path):
        raise FileNotFoundError(
            f"Файл шаблона этикетки не найден: {tpl_path}. "
            "Задайте LABEL_TEMPLATE_PDF или положите print_2026_03_25_21_41.pdf в корень проекта."
        )

    reg_path, bold_path = _font_paths()
    face_reg = fitz.Font(fontfile=reg_path)
    face_bold = fitz.Font(fontfile=bold_path)

    src = fitz.open(tpl_path)
    pi = _template_page_index()
    if pi < 0 or pi >= len(src):
        src.close()
        raise ValueError(f"LABEL_TEMPLATE_PAGE={pi} вне диапазона файла (0…{len(src)-1})")

    eac_png = _extract_eac_raster_from_template(src, pi)

    redacts_base = (
        _REDACT_TITLE,
        _REDACT_DASH,
        _REDACT_ARTICLE_VAL,
        _REDACT_SIZE_VAL,
        _REDACT_SELLER,
        _REDACT_COMP_COUNTRY,
        _REDACT_UNDERSCORES,
        _REDACT_BARCODE,
    )

    out = fitz.open()
    try:
        for item in items:
            page = out.new_page(width=580, height=400)
            page.show_pdf_page(page.rect, src, pi)
            for r in redacts_base:
                page.add_redact_annot(r, fill=(1, 1, 1))
            if eac_png:
                page.add_redact_annot(_REDACT_EAC, fill=(1, 1, 1))
            page.apply_redactions()

            page.insert_font("lreg", fontfile=reg_path)
            page.insert_font("lbold", fontfile=bold_path)

            category = (item.get("category_leaf") or "").strip() or "Товар"
            brand = (Config.LABEL_BRAND_LINE or "").strip() or "STIGMA"
            title = f"{category} / {brand}"
            offer = (item.get("offer_id") or item.get("sku") or "").strip() or "—"
            size_txt = (item.get("manufacturer_size") or "").strip() or "—"
            seller = Config.LABEL_SELLER
            composition = Config.LABEL_COMPOSITION
            country = Config.LABEL_COUNTRY
            country_txt = f"Страна производства: {country}"
            comp_full = f"Состав: {composition}"

            fs_title = 30.0
            max_title_w = 503.8871154785156 - 106.1181640625
            while fs_title >= 14 and face_bold.text_length(title, fontsize=fs_title) > max_title_w:
                fs_title -= 0.75
            tw = face_bold.text_length(title, fontsize=fs_title)
            page.insert_text(
                (_TITLE_CENTER_X - tw / 2, _baseline_y1(face_bold, fs_title, _TITLE_Y1)),
                title,
                fontname="lbold",
                fontsize=fs_title,
                color=(0, 0, 0),
            )

            max_dash_w = 579.3858032226562 - _DASH_X0
            dash = _hyphen_rule(face_reg, 16.0, max_dash_w)
            page.insert_text(
                (_DASH_X0, _baseline_y1(face_reg, 16.0, _DASH_Y1)),
                dash,
                fontname="lreg",
                fontsize=16,
                color=(0, 0, 0),
            )

            offer_t = _truncate_for_font(
                face_bold, offer, 30.0, _SELLER_RIGHT - _ART_VAL_X - 10
            )
            page.insert_text(
                (_ART_VAL_X, _baseline_y1(face_bold, 30.0, _ART_VAL_Y1)),
                offer_t,
                fontname="lbold",
                fontsize=30,
                color=(0, 0, 0),
            )

            size_t = _truncate_for_font(
                face_bold, size_txt, 30.0, 388 - _SZ_VAL_X - 8
            )
            page.insert_text(
                (_SZ_VAL_X, _baseline_y1(face_bold, 30.0, _SZ_VAL_Y1)),
                size_t,
                fontname="lbold",
                fontsize=30,
                color=(0, 0, 0),
            )

            seller_max_w = _SELLER_RIGHT - 388.583984375 - 2
            seller_t, seller_fs = _fit_line(face_reg, seller, 20.0, seller_max_w)
            if face_reg.text_length(seller_t, fontsize=seller_fs) > seller_max_w:
                seller_t = _truncate_for_font(face_reg, seller_t, seller_fs, seller_max_w)
            sw = face_reg.text_length(seller_t, fontsize=seller_fs)
            page.insert_text(
                (_SELLER_RIGHT - sw, _baseline_y1(face_reg, seller_fs, _SELLER_Y1)),
                seller_t,
                fontname="lreg",
                fontsize=seller_fs,
                color=(0, 0, 0),
            )

            comp_max_w = 287.87109375 - _COMP_X - 6
            comp_t, comp_fs = _fit_line(face_reg, comp_full, 20.0, comp_max_w)
            if face_reg.text_length(comp_t, fontsize=comp_fs) > comp_max_w:
                comp_t = _truncate_for_font(face_reg, comp_t, comp_fs, comp_max_w)
            page.insert_text(
                (_COMP_X, _baseline_y1(face_reg, comp_fs, _COMP_Y1)),
                comp_t,
                fontname="lreg",
                fontsize=comp_fs,
                color=(0, 0, 0),
            )

            country_max_w = _COUNTRY_RIGHT - 287.87109375 - 4
            country_t, country_fs = _fit_line(face_reg, country_txt, 20.0, country_max_w)
            if face_reg.text_length(country_t, fontsize=country_fs) > country_max_w:
                country_t = _truncate_for_font(face_reg, country_t, country_fs, country_max_w)
            cw = face_reg.text_length(country_t, fontsize=country_fs)
            page.insert_text(
                (_COUNTRY_RIGHT - cw, _baseline_y1(face_reg, country_fs, _COUNTRY_Y1)),
                country_t,
                fontname="lreg",
                fontsize=country_fs,
                color=(0, 0, 0),
            )

            under_w = 577.3651733398438 - _UNDER_X
            under_fs = 20.0
            under = "_"
            while under_fs >= 14:
                u = "_"
                while face_reg.text_length(u + "_", fontsize=under_fs) < under_w:
                    u += "_"
                if face_reg.text_length(u, fontsize=under_fs) <= under_w:
                    under = u
                    break
                under_fs -= 0.5
            while under and face_reg.text_length(under, fontsize=under_fs) > under_w:
                under = under[:-1]
            page.insert_text(
                (_UNDER_X, _baseline_y1(face_reg, under_fs, _UNDER_Y1)),
                under,
                fontname="lreg",
                fontsize=under_fs,
                color=(0, 0, 0),
            )

            bc_buf, bc_text = _bc_png(item.get("barcode") or "")
            if bc_buf:
                # Заполняем весь прямоугольник (иначе при keep_proportion полосы узкие по ширине).
                page.insert_image(
                    _BARCODE_RECT,
                    stream=bc_buf.getvalue(),
                    keep_proportion=False,
                )
                dig_max = _BARCODE_RECT.x1 - _BARCODE_RECT.x0 - 16
                dig = _truncate_for_font(
                    face_reg, bc_text, _BARCODE_DIGITS_FS, dig_max
                )
                d_fs = _BARCODE_DIGITS_FS
                dw = face_reg.text_length(dig, fontsize=d_fs)
                cx = (_BARCODE_RECT.x0 + _BARCODE_RECT.x1) / 2
                page.insert_text(
                    (cx - dw / 2, _baseline_y1(face_reg, d_fs, _DIGITS_Y1)),
                    dig,
                    fontname="lreg",
                    fontsize=d_fs,
                    color=(0, 0, 0),
                )

            if eac_png:
                eac_rect = _eac_rect_centered_on_barcode(_BARCODE_RECT)
                page.insert_image(
                    eac_rect,
                    stream=eac_png,
                    keep_proportion=True,
                )

        base_w, base_h = 580.0, 400.0
        if target_page_pt and target_page_pt[0] > 0 and target_page_pt[1] > 0:
            target_w, target_h = float(target_page_pt[0]), float(target_page_pt[1])
            keep_prop = bool(target_fit_letterbox)
        else:
            # Нет размера от Ozon (нет posting / API недоступен) — страница как в шаблоне 580×400.
            target_w, target_h = base_w, base_h
            keep_prop = False
        need_resize = abs(target_w - base_w) > 0.01 or abs(target_h - base_h) > 0.01

        if need_resize:
            resized = fitz.open()
            try:
                for i in range(out.page_count):
                    dst = resized.new_page(width=target_w, height=target_h)
                    # Как в ленте заказов: при target_fit_letterbox вписываем с полями;
                    # иначе заполняем весь прямоугольник (малый формат из .env).
                    dst.show_pdf_page(dst.rect, out, i, keep_proportion=keep_prop)
                resized.subset_fonts()
                resized.save(
                    out_path,
                    garbage=4,
                    deflate=True,
                    deflate_images=True,
                    deflate_fonts=True,
                    use_objstms=1,
                    pretty=False,
                )
            finally:
                resized.close()
        else:
            # Keep print quality, but shrink PDFs by embedding only used font glyphs.
            out.subset_fonts()
            out.save(
                out_path,
                garbage=4,
                deflate=True,
                deflate_images=True,
                deflate_fonts=True,
                use_objstms=1,
                pretty=False,
            )
    finally:
        out.close()
        src.close()

    return rel.replace("\\", "/")


def build_label_pages_for_order_items(rows: List[Dict[str, Any]], max_pages: int = 80) -> List[Dict[str, Any]]:
    """Expand order_items rows into per-unit label dicts (only with barcode)."""
    pages: List[Dict[str, Any]] = []
    for row in rows:
        bc = (row.get("barcode") or "").strip()
        if not bc:
            continue
        try:
            qty = int(row.get("quantity") or 1)
        except (TypeError, ValueError):
            qty = 1
        qty = max(1, min(qty, 30))
        for _ in range(qty):
            pages.append(dict(row))
            if len(pages) >= max_pages:
                return pages
    return pages
