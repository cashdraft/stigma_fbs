"""Кэш PDF этикеток Ozon и нормализация ориентации (общий код для app и сервиса)."""

from __future__ import annotations

import logging
import os
import re
from typing import Callable, Optional, Tuple

import pymupdf as fitz

from config import Config

logger = logging.getLogger(__name__)


def ozon_label_cache_path(posting_number: str) -> str:
    safe = re.sub(r"[^\w\-.]+", "_", posting_number or "", flags=re.UNICODE)[:180]
    return os.path.join(Config.BASE_DIR, "instance", "ozon_labels_cache", f"{safe}.pdf")


def normalize_ozon_label_pdf(pdf: bytes) -> bytes:
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
        logger.exception("Не удалось нормализовать ориентацию этикетки Ozon")
    return pdf


def load_cached_ozon_label(posting_number: str) -> Optional[bytes]:
    path = ozon_label_cache_path(posting_number)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as fh:
            data = fh.read()
        return data if data.startswith(b"%PDF") else None
    except OSError:
        return None


def save_cached_ozon_label(posting_number: str, pdf: bytes) -> None:
    path = ozon_label_cache_path(posting_number)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(pdf)


def first_page_size_pt(pdf_bytes: bytes) -> Tuple[float, float]:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        r = doc[0].rect
        return (float(r.width), float(r.height))
    finally:
        doc.close()


def ensure_ozon_label_pdf_for_posting(
    posting_number: str,
    fetch_pdf: Callable[[str], bytes],
) -> Optional[bytes]:
    """Вернуть нормализованный PDF из кэша или скачать, сохранить и вернуть."""
    if not posting_number:
        return None
    cached = load_cached_ozon_label(posting_number)
    if cached:
        return cached
    try:
        raw = fetch_pdf(posting_number)
        pdf = normalize_ozon_label_pdf(raw)
        save_cached_ozon_label(posting_number, pdf)
        return pdf
    except Exception:
        logger.warning("Не удалось получить этикетку Ozon для posting=%s", posting_number)
        return None
