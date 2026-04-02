import json
import logging
import time
import base64
from typing import Any, Dict, List, Optional

import requests

from config import Config


class WbApiError(Exception):
    pass


def _wb_error_detail(response: requests.Response) -> str:
    text = (response.text or "").strip()
    if not text:
        return ""
    try:
        data = response.json()
        if isinstance(data, dict):
            for key in ("detail", "message", "error", "errorText", "title"):
                v = data.get(key)
                if v is not None and str(v).strip():
                    return str(v).strip()
            return json.dumps(data, ensure_ascii=False)[:400]
    except (ValueError, TypeError):
        pass
    return text[:400]


class WbClient:
    """Клиент [FBS Assembly Orders API v3](https://dev.wildberries.ru/en/docs/openapi/orders-fbs)."""

    def __init__(self) -> None:
        self.base_url = Config.WB_MARKETPLACE_BASE_URL.rstrip("/")
        self.token = (Config.WB_API_TOKEN or "").strip()
        self.timeout = 45
        self.logger = logging.getLogger(__name__)
        self._min_interval = 0.22  # ~200 ms между запросами к лимиту WB
        self._last_request_at = 0.0

    def _throttle(self) -> None:
        now = time.monotonic()
        wait = self._min_interval - (now - self._last_request_at)
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    def _headers(self) -> Dict[str, str]:
        tok = self.token.strip()
        if tok.lower().startswith("bearer "):
            tok = tok[7:].strip()
        if Config.WB_USE_BEARER_PREFIX:
            auth = f"Bearer {tok}"
        else:
            auth = tok
        return {
            "Authorization": auth,
            "Content-Type": "application/json",
        }

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not self.token:
            raise WbApiError("Отсутствует WB_API_TOKEN в .env (токен категории «Маркетплейс»).")

        self._throttle()
        url = f"{self.base_url}{path}"
        try:
            response = requests.get(
                url,
                headers=self._headers(),
                params=params or {},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            self.logger.exception("Ошибка сети при запросе к WB API")
            raise WbApiError("Не удалось подключиться к API Wildberries") from exc

        if response.status_code in (401, 403):
            raise WbApiError("Ошибка авторизации в API Wildberries (проверьте токен и категорию «Маркетплейс»).")

        if response.status_code == 429:
            self.logger.warning("WB API 429, повтор через 2 с")
            time.sleep(2.0)
            self._throttle()
            response = requests.get(
                url,
                headers=self._headers(),
                params=params or {},
                timeout=self.timeout,
            )

        if response.status_code >= 400:
            detail = _wb_error_detail(response)
            self.logger.error(
                "Ошибка WB API: %s %s %s",
                response.status_code,
                response.url,
                (response.text or "")[:800],
            )
            msg = f"Wildberries API вернул ошибку {response.status_code}"
            if detail:
                msg = f"{msg}: {detail}"
            raise WbApiError(msg)

        try:
            return response.json()
        except ValueError as exc:
            self.logger.exception("Некорректный JSON от WB API")
            raise WbApiError("Неожиданный формат ответа WB API") from exc

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self.token:
            raise WbApiError("Отсутствует WB_API_TOKEN в .env (токен категории «Маркетплейс»).")

        self._throttle()
        url = f"{self.base_url}{path}"
        try:
            response = requests.post(
                url,
                headers=self._headers(),
                json=payload,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            self.logger.exception("Ошибка сети при POST к WB API")
            raise WbApiError("Не удалось подключиться к API Wildberries") from exc

        if response.status_code in (401, 403):
            raise WbApiError("Ошибка авторизации в API Wildberries (проверьте токен и категорию «Маркетплейс»).")

        if response.status_code >= 400:
            detail = _wb_error_detail(response)
            self.logger.error(
                "Ошибка WB API POST: %s %s %s",
                response.status_code,
                response.url,
                (response.text or "")[:800],
            )
            msg = f"Wildberries API вернул ошибку {response.status_code}"
            if detail:
                msg = f"{msg}: {detail}"
            raise WbApiError(msg)

        try:
            return response.json()
        except ValueError as exc:
            self.logger.exception("Некорректный JSON от WB API (POST)")
            raise WbApiError("Неожиданный формат ответа WB API") from exc

    def _patch(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self.token:
            raise WbApiError("Отсутствует WB_API_TOKEN в .env (токен категории «Маркетплейс»).")

        self._throttle()
        url = f"{self.base_url}{path}"
        try:
            response = requests.patch(
                url,
                headers=self._headers(),
                json=payload,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            self.logger.exception("Ошибка сети при PATCH к WB API")
            raise WbApiError("Не удалось подключиться к API Wildberries") from exc

        if response.status_code in (401, 403):
            raise WbApiError("Ошибка авторизации в API Wildberries (проверьте токен и категорию «Маркетплейс»).")

        if response.status_code >= 400:
            detail = _wb_error_detail(response)
            self.logger.error(
                "Ошибка WB API PATCH: %s %s %s",
                response.status_code,
                response.url,
                (response.text or "")[:800],
            )
            msg = f"Wildberries API вернул ошибку {response.status_code}"
            if detail:
                msg = f"{msg}: {detail}"
            raise WbApiError(msg)

        if not (response.text or "").strip():
            return {}
        try:
            return response.json()
        except ValueError:
            return {}

    def get_new_orders(self) -> List[Dict[str, Any]]:
        data = self._get("/api/v3/orders/new")
        return list(data.get("orders") or [])

    def get_orders_page(
        self,
        *,
        limit: int,
        next_cursor: int,
        date_from: int,
        date_to: int,
    ) -> Dict[str, Any]:
        """
        GET /api/v3/orders — период до 30 суток, пагинация через next.
        """
        params: Dict[str, Any] = {
            "limit": max(1, min(int(limit), 1000)),
            "next": int(next_cursor),
            "dateFrom": int(date_from),
            "dateTo": int(date_to),
        }
        return self._get("/api/v3/orders", params=params)

    def get_orders_statuses(self, order_ids: List[int]) -> List[Dict[str, Any]]:
        """POST /api/v3/orders/status — до 1000 id за запрос."""
        if not order_ids:
            return []
        data = self._post("/api/v3/orders/status", {"orders": order_ids})
        return list(data.get("orders") or [])

    def create_supply(self, name: str) -> str:
        data = self._post("/api/v3/supplies", {"name": (name or "").strip()})
        sid = str(data.get("id") or "").strip()
        if not sid:
            raise WbApiError("WB не вернул id созданной поставки")
        return sid

    def get_supplies_page(self, *, limit: int = 1000, next_cursor: int = 0) -> Dict[str, Any]:
        return self._get(
            "/api/v3/supplies",
            params={"limit": max(1, min(int(limit), 1000)), "next": int(next_cursor)},
        )

    def get_supply_details(self, supply_id: str) -> Dict[str, Any]:
        if not supply_id:
            raise WbApiError("Не указан WB supplyId")
        return self._get(f"/api/v3/supplies/{supply_id}")

    def add_orders_to_supply(self, supply_id: str, order_ids: List[int]) -> None:
        if not supply_id:
            raise WbApiError("Не указан WB supplyId")
        ids = [int(x) for x in order_ids if x is not None]
        if not ids:
            return
        self._patch(f"/api/marketplace/v3/supplies/{supply_id}/orders", {"orders": ids})

    def get_order_stickers_png(self, order_ids: List[int]) -> Dict[int, bytes]:
        """POST /api/v3/orders/stickers -> {orderId: png_bytes}."""
        ids = [int(x) for x in order_ids if x is not None]
        if not ids:
            return {}
        self._throttle()
        url = f"{self.base_url}/api/v3/orders/stickers"
        try:
            response = requests.post(
                url,
                headers=self._headers(),
                params={"type": "png", "width": 58, "height": 40},
                json={"orders": ids[:100]},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            self.logger.exception("Ошибка сети при POST stickers к WB API")
            raise WbApiError("Не удалось подключиться к API Wildberries") from exc
        if response.status_code in (401, 403):
            raise WbApiError("Ошибка авторизации в API Wildberries (проверьте токен и категорию «Маркетплейс»).")
        if response.status_code >= 400:
            detail = _wb_error_detail(response)
            msg = f"Wildberries API вернул ошибку {response.status_code}"
            if detail:
                msg = f"{msg}: {detail}"
            raise WbApiError(msg)
        try:
            data = response.json()
        except ValueError as exc:
            raise WbApiError("Неожиданный формат ответа WB API (stickers)") from exc
        out: Dict[int, bytes] = {}
        for s in list(data.get("stickers") or []):
            try:
                oid = int(s.get("orderId"))
            except (TypeError, ValueError):
                continue
            raw = str(s.get("file") or "").strip()
            if not raw:
                continue
            try:
                out[oid] = base64.b64decode(raw)
            except Exception:
                continue
        return out
