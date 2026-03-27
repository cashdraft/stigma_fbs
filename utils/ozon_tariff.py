"""Парсинг тарифа отгрузки FBS из ответа Ozon (поля tariffication / tariffication_steps)."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple


def _charge_text(raw_charge: Any, currency: str = "RUB") -> str:
    if raw_charge is None or raw_charge == "":
        return ""
    if isinstance(raw_charge, dict):
        amount = raw_charge.get("amount", "")
        cur = raw_charge.get("currency") or currency
    else:
        amount = str(raw_charge).strip()
        cur = currency
    if not amount or amount == "0":
        return "0 ₽" if amount == "0" else ""
    sym = "₽" if cur == "RUB" else cur
    return f"{amount} {sym}".replace("  ", " ").strip()


def _label_for_type(tariff_type: str, charge_display: str) -> str:
    if tariff_type == "discount":
        return f"Скидка {charge_display}" if charge_display else "Скидка"
    if tariff_type == "no_discount":
        return "Без скидки"
    if tariff_type == "commission":
        if charge_display:
            return f"Штраф {charge_display}"
        return "Штраф"
    return tariff_type or "—"


def _parse_next_hint(t: Dict[str, Any]) -> str:
    ncharge = t.get("next_tariff_charge")
    ntype = t.get("next_tariff_type") or ""
    ncur = t.get("next_tariff_charge_currency_code") or "RUB"
    starts = t.get("next_tariff_starts_at")
    if not ncharge and ntype not in ("commission", "discount"):
        return ""
    ctext = _charge_text(ncharge, ncur)
    if ntype == "commission":
        lbl = f"далее: штраф {ctext}" if ctext else "далее: штраф"
    elif ntype == "discount":
        lbl = f"далее: скидка {ctext}" if ctext else "далее: скидка"
    else:
        lbl = ""
    if starts and len(str(starts)) > 10:
        try:
            dt = datetime.fromisoformat(str(starts).replace("Z", "+00:00"))
            lbl += f" с {dt.strftime('%d.%m %H:%M')}"
        except ValueError:
            pass
    return lbl.strip()


def parse_shipment_tariff_from_raw(raw_json: Optional[str]) -> Dict[str, Any]:
    """Возвращает label, hint, segments (индекс активного шага), steps_count для UI."""
    empty: Dict[str, Any] = {
        "label": "—",
        "hint": "",
        "segment_active": -1,
        "segment_count": 0,
    }
    if not raw_json:
        return empty
    try:
        data = json.loads(raw_json)
    except (TypeError, ValueError):
        return empty

    t = data.get("tariffication") or {}
    steps: List[Dict[str, Any]] = data.get("tariffication_steps") or []

    cur_type = t.get("current_tariff_type") or ""
    cur_charge = _charge_text(t.get("current_tariff_charge"), t.get("current_tariff_charge_currency_code") or "RUB")

    label = _label_for_type(cur_type, cur_charge)
    hint = _parse_next_hint(t)

    active = -1
    cur_rate = t.get("current_tariff_rate")
    if steps and cur_rate is not None:
        for i, step in enumerate(steps):
            if step.get("tariff_rate") == cur_rate:
                active = i
                break
    if active < 0 and steps:
        active = 0

    return {
        "label": label,
        "hint": hint,
        "segment_active": active,
        "segment_count": len(steps),
    }
