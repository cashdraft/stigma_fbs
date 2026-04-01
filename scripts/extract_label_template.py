#!/usr/bin/env python3
"""Вырезать страницу из PDF-ленты → instance/wb_label_template.pdf (опционально для WB).

Не копировать результат на место print_2026_03_25_21_41.pdf: первая страница ленты — уже
сгенерированная этикетка, координаты в utils/label_pdf.py рассчитаны под оригинальный шаблон Ozon.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymupdf as fitz  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("source_pdf", help="Например lenta_zakazov_OZON_….pdf")
    p.add_argument(
        "-o",
        "--out",
        default=os.path.join("instance", "wb_label_template.pdf"),
        help="Куда сохранить одностраничный шаблон",
    )
    p.add_argument("-n", "--page", type=int, default=0, help="Индекс страницы (0 = первая)")
    args = p.parse_args()
    if not os.path.isfile(args.source_pdf):
        print("Нет файла:", args.source_pdf, file=sys.stderr)
        return 1
    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    src = fitz.open(args.source_pdf)
    try:
        if args.page < 0 or args.page >= len(src):
            print("page вне диапазона 0…", len(src) - 1, file=sys.stderr)
            return 1
        dst = fitz.open()
        dst.insert_pdf(src, from_page=args.page, to_page=args.page)
        dst.save(args.out, garbage=4, deflate=True)
        dst.close()
    finally:
        src.close()
    print(args.out, os.path.getsize(args.out), "bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
