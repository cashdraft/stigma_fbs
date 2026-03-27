"""Извлечение цвета и размера производителя из метаданных атрибутов Ozon."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


def resolve_color_and_mfr_size_attr_ids(category_attributes: List[Dict[str, Any]]) -> Tuple[Optional[int], Optional[int]]:
    """По списку /v1/description-category/attribute находит id атрибутов «Цвет товара» и «Размер производителя»."""
    color_id: Optional[int] = None
    size_id: Optional[int] = None
    for a in category_attributes or []:
        aid = a.get("id")
        if aid is None:
            continue
        name = (a.get("name") or "").lower()
        if "размер производителя" in name:
            size_id = int(aid)
        if name == "цвет товара":
            color_id = int(aid)
        elif color_id is None and name == "название цвета":
            color_id = int(aid)
        elif color_id is None and "цвет" in name and "товар" in name and "название" not in name:
            color_id = int(aid)
    return color_id, size_id


def first_attribute_value(attributes: List[Dict[str, Any]], attr_id: Optional[int]) -> str:
    if attr_id is None:
        return ""
    for block in attributes or []:
        if block.get("id") != attr_id:
            continue
        values = block.get("values") or []
        if not values:
            return ""
        raw = values[0].get("value")
        if raw is None:
            return ""
        text = str(raw).strip()
        if text.startswith("{"):
            return ""
        return text
    return ""
