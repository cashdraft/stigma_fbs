import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests

from config import Config


class OzonApiError(Exception):
    pass


class OzonClient:
    def __init__(self) -> None:
        self.base_url = Config.OZON_BASE_URL.rstrip("/")
        self.client_id = Config.OZON_CLIENT_ID
        self.api_key = Config.OZON_API_KEY
        self.timeout = 30
        self._tree_timeout = 120
        self.logger = logging.getLogger(__name__)

    def _headers(self) -> Dict[str, str]:
        return {
            "Client-Id": self.client_id,
            "Api-Key": self.api_key,
            "Content-Type": "application/json",
        }

    def _post(self, path: str, payload: Dict[str, Any], timeout: Optional[int] = None) -> Dict[str, Any]:
        response = self._post_response(path, payload, timeout=timeout)
        try:
            return response.json()
        except ValueError as exc:
            self.logger.exception("Некорректный JSON от Ozon API")
            raise OzonApiError("Неожиданный формат ответа Ozon API") from exc

    def _post_response(
        self,
        path: str,
        payload: Dict[str, Any],
        timeout: Optional[int] = None,
    ) -> requests.Response:
        url = f"{self.base_url}{path}"
        wait = timeout if timeout is not None else self.timeout
        try:
            response = requests.post(
                url,
                headers=self._headers(),
                json=payload,
                timeout=wait,
            )
        except requests.RequestException as exc:
            self.logger.exception("Ошибка сети при запросе в Ozon API")
            raise OzonApiError("Не удалось подключиться к Ozon API") from exc

        if response.status_code in (401, 403):
            raise OzonApiError("Ошибка авторизации в API Ozon")

        if response.status_code >= 400:
            self.logger.error("Ошибка Ozon API: %s %s", response.status_code, response.text)
            raise OzonApiError(f"Ozon API вернул ошибку {response.status_code}")
        return response

    @staticmethod
    def _to_iso8601(value: Optional[str], default: datetime, end_of_day: bool = False) -> str:
        if not value:
            return default.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

        # Date input from HTML is "YYYY-MM-DD". Convert to RFC3339.
        if len(value) == 10:
            dt = datetime.fromisoformat(value)
            if end_of_day:
                dt = dt.replace(hour=23, minute=59, second=59)
        else:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def get_fbs_postings(
        self,
        status: Optional[str] = None,
        since: Optional[str] = None,
        to: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        if not self.client_id or not self.api_key:
            raise OzonApiError("Отсутствуют OZON_CLIENT_ID/OZON_API_KEY в .env")

        payload: Dict[str, Any] = {
            "dir": "DESC",
            "limit": limit,
            "offset": offset,
            "translit": True,
            "with": {
                "analytics_data": True,
                "financial_data": True,
            },
        }

        now = datetime.now(timezone.utc)
        since_default = now - timedelta(days=30)
        to_default = now + timedelta(days=1)

        filter_data: Dict[str, Any] = {
            # Ozon requires date bounds when filter object is sent.
            "since": self._to_iso8601(since, since_default),
            "to": self._to_iso8601(to, to_default, end_of_day=True),
        }
        if status and status != "all":
            filter_data["status"] = status
        payload["filter"] = filter_data

        data = self._post("/v3/posting/fbs/list", payload)

        result = data.get("result", {})
        postings = result.get("postings", [])
        if not isinstance(postings, list):
            raise OzonApiError("Неожиданный формат данных заказов")

        self.logger.info("Получено заказов из Ozon API: %s", len(postings))
        return postings

    def get_product_images_by_sku(self, skus: List[int]) -> Dict[str, str]:
        if not skus:
            return {}

        data = self._post("/v3/product/info/list", {"sku": skus})
        items = data.get("items", [])
        image_map: Dict[str, str] = {}
        for item in items:
            sku = item.get("sku")
            if sku is None:
                continue

            image_url = ""
            primary = item.get("primary_image") or []
            if isinstance(primary, list) and primary:
                image_url = primary[0]
            if not image_url:
                images = item.get("images") or []
                if isinstance(images, list) and images:
                    image_url = images[0]

            image_map[str(sku)] = image_url
        return image_map

    def get_type_id_to_leaf_name(self) -> Dict[int, str]:
        """Конечный тип товара (например «Футболка») по type_id из дерева description-category."""
        cache_path = os.path.join(Config.BASE_DIR, "instance", "ozon_type_map.json")
        max_age_sec = 86400 * 7
        if os.path.isfile(cache_path) and (time.time() - os.path.getmtime(cache_path)) < max_age_sec:
            with open(cache_path, encoding="utf-8") as fh:
                raw = json.load(fh)
            return {int(k): str(v) for k, v in raw.items()}

        data = self._post("/v1/description-category/tree", {"language": "DEFAULT"}, timeout=self._tree_timeout)
        acc: Dict[int, str] = {}

        def walk(nodes: Any) -> None:
            for node in nodes or []:
                tid = node.get("type_id")
                if tid is not None:
                    acc[int(tid)] = str(node.get("type_name") or "")
                walk(node.get("children"))

        walk(data.get("result", []))
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as fh:
            json.dump({str(k): v for k, v in acc.items()}, fh, ensure_ascii=False)
        self.logger.info("Сохранена карта type_id Ozon: %s типов", len(acc))
        return acc

    def get_description_category_attributes(
        self, description_category_id: int, type_id: int
    ) -> List[Dict[str, Any]]:
        data = self._post(
            "/v1/description-category/attribute",
            {
                "description_category_id": description_category_id,
                "type_id": type_id,
                "language": "DEFAULT",
            },
        )
        result = data.get("result", [])
        return result if isinstance(result, list) else []

    def get_product_attributes_by_sku(self, skus: List[int]) -> Dict[str, Dict[str, Any]]:
        if not skus:
            return {}
        payload = {"filter": {"sku": [str(s) for s in skus]}, "limit": min(len(skus), 100)}
        data = self._post("/v4/product/info/attributes", payload)
        out: Dict[str, Dict[str, Any]] = {}
        for item in data.get("result", []) or []:
            sku = item.get("sku")
            if sku is None:
                continue
            out[str(int(sku))] = item
        return out

    def ship_fbs_posting(
        self,
        posting_number: str,
        products: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        Перевод FBS posting в следующее состояние (отгрузка) через Seller API.

        В качестве endpoint используем /v4/posting/fbs/ship/package.
        """
        if not posting_number:
            raise OzonApiError("Отсутствует posting_number для отгрузки")

        payload: Dict[str, Any] = {"posting_number": posting_number}
        if products:
            payload["products"] = products

        # Ozon API response format is not strictly typed here; we pass through result.
        return self._post("/v4/posting/fbs/ship/package", payload)

    def get_fbs_posting_status(self, posting_number: str) -> str:
        """Актуальный статус FBS posting из /v3/posting/fbs/get; пустая строка при ошибке."""
        if not posting_number:
            return ""
        try:
            data = self._post("/v3/posting/fbs/get", {"posting_number": posting_number})
        except OzonApiError:
            return ""
        result = data.get("result") or {}
        return str(result.get("status") or "")

    def get_fbs_package_label_pdf(self, posting_number: str) -> bytes:
        """Скачать PDF этикетки отправления Ozon для FBS posting."""
        if not posting_number:
            raise OzonApiError("Отсутствует posting_number для этикетки Ozon")

        response = self._post_response(
            "/v2/posting/fbs/package-label",
            {"posting_number": [posting_number]},
            timeout=60,
        )
        content = response.content or b""
        ctype = (response.headers.get("Content-Type") or "").lower()
        if "application/pdf" in ctype or content.startswith(b"%PDF"):
            return content

        # Some API gateways may return JSON with a file URL or embedded payload.
        try:
            data = response.json()
        except ValueError as exc:
            raise OzonApiError("Ozon вернул неожиданный формат этикетки (не PDF)") from exc

        result = data.get("result")
        if isinstance(result, str) and result.startswith("http"):
            try:
                r2 = requests.get(result, timeout=60)
                r2.raise_for_status()
            except requests.RequestException as exc:
                raise OzonApiError("Не удалось скачать PDF этикетки Ozon по ссылке") from exc
            if (r2.content or b"").startswith(b"%PDF"):
                return r2.content

        raise OzonApiError("Ozon не вернул PDF этикетки отправления")
