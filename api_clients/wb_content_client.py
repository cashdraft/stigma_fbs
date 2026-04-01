"""Клиент Content API Wildberries (карточки товара: название, фото, размеры по chrtID)."""

import json
import logging
import time
from typing import Any, Callable, Dict, Optional, Set

import requests

from config import Config


class WbContentError(Exception):
    pass


def _content_error_detail(response: requests.Response) -> str:
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


class WbContentClient:
    """
    POST /content/v2/get/cards/list — токен с категорией «Контент».

    filter.withPhoto=-1 обязателен (см. forum WB): иначе в выборку не попадают карточки с фото.

    Для многих nmId выгоднее пагинация всего каталога (по 100 шт.) и фильтр на клиенте —
    см. ответ сотрудника WB: https://dev.wildberries.ru/forum/1535
    (textSearch — только один артикул за запрос).
    """

    def __init__(self) -> None:
        self.base_url = Config.WB_CONTENT_BASE_URL.rstrip("/")
        self.token = (Config.WB_API_TOKEN or "").strip()
        self.timeout = 45
        self.logger = logging.getLogger(__name__)
        self._min_interval = max(0.05, float(getattr(Config, "WB_CONTENT_MIN_INTERVAL", 0.65)))
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

    def _request_cards_list(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        if not self.token:
            raise WbContentError(
                "Отсутствует WB_API_TOKEN в .env (для карточек нужен токен с категорией «Контент»)."
            )

        payload = {"settings": settings}
        self._throttle()
        url = f"{self.base_url}/content/v2/get/cards/list"
        try:
            response = requests.post(
                url,
                headers=self._headers(),
                json=payload,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            self.logger.exception("Ошибка сети при POST к WB Content API")
            raise WbContentError("Не удалось подключиться к Content API Wildberries") from exc

        if response.status_code in (401, 403):
            raise WbContentError(
                "Ошибка авторизации Content API (проверьте токен и категорию «Контент» в кабинете WB)."
            )

        if response.status_code == 429:
            self.logger.warning("WB Content API 429, пауза 3 с")
            time.sleep(3.0)
            self._throttle()
            response = requests.post(
                url,
                headers=self._headers(),
                json=payload,
                timeout=self.timeout,
            )

        if response.status_code >= 400:
            detail = _content_error_detail(response)
            self.logger.error(
                "WB Content API: %s %s %s",
                response.status_code,
                response.url,
                (response.text or "")[:800],
            )
            msg = f"Content API вернул ошибку {response.status_code}"
            if detail:
                msg = f"{msg}: {detail}"
            raise WbContentError(msg)

        try:
            return response.json()
        except ValueError as exc:
            self.logger.exception("Некорректный JSON от WB Content API")
            raise WbContentError("Неожиданный формат ответа Content API") from exc

    def get_card_by_nm_id(self, nm_id: int) -> Optional[Dict[str, Any]]:
        """Один nmID через textSearch + точное совпадение nmID в ответе."""
        settings = {
            "sort": {"ascending": False},
            "filter": {
                "textSearch": str(int(nm_id)),
                "withPhoto": -1,
            },
            "cursor": {"limit": 40},
        }
        data = self._request_cards_list(settings)
        cards = list(data.get("cards") or [])
        target = int(nm_id)
        for card in cards:
            if not isinstance(card, dict):
                continue
            cid = card.get("nmID")
            if cid is None:
                cid = card.get("nmId")
            if cid is None:
                continue
            try:
                if int(cid) == target:
                    return card
            except (TypeError, ValueError):
                continue
        return None

    def iter_catalog_pages(
        self,
        *,
        max_pages: int = 4000,
        cancel_check: Optional[Callable[[], bool]] = None,
    ):
        """
        Все страницы каталога продавца (по 100 карточек), без textSearch.
        Для полной ночной выгрузки в локальную БД.

        cancel_check — если возвращает True, следующий запрос к API не выполняется (кооперативная отмена).
        """
        cursor: Dict[str, Any] = {"limit": 100}
        for page_idx in range(max_pages):
            if cancel_check and cancel_check():
                break
            settings = {"cursor": cursor, "filter": {"withPhoto": -1}}
            data = self._request_cards_list(settings)
            cards = list(data.get("cards") or [])
            page_num = page_idx + 1
            cinfo = data.get("cursor") or {}
            yield page_num, cards, cinfo

            if not cards:
                break

            lim = int(cursor.get("limit") or 100)
            if len(cards) < lim:
                break

            nm_next = cinfo.get("nmID")
            ua_next = cinfo.get("updatedAt")
            if nm_next is None or ua_next is None:
                break
            cursor = {"limit": 100, "nmID": nm_next, "updatedAt": ua_next}

    def fetch_cards_for_nm_ids(
        self,
        nm_ids: Set[int],
        *,
        progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
        paginate_min_count: Optional[int] = None,
        max_pages: int = 4000,
        use_catalog_pagination: bool = True,
    ) -> Dict[int, Dict[str, Any]]:
        """
        Собирает карточки для набора nmId.

        API WB не умеет «дай карточки по списку nmId» одним запросом.

        - При use_catalog_pagination=True и |nm_ids| >= порога: листает весь каталог пачками по 100,
          отбирая нужные nmId (выгодно, если нужен почти весь каталог).
        - Иначе: только textSearch по одному nmId на запрос (выгодно для небольшой выборки в большом каталоге).

        Остаток после скана каталога всегда добирается через textSearch по недостающим nm.
        """
        needed: Set[int] = {int(x) for x in nm_ids}
        found: Dict[int, Dict[str, Any]] = {}
        if not needed:
            return found

        threshold = (
            paginate_min_count
            if paginate_min_count is not None
            else int(getattr(Config, "WB_CONTENT_PAGINATE_MIN_NM", 25))
        )

        def _p(extra: Dict[str, Any]) -> None:
            if progress_cb:
                progress_cb(extra)

        if use_catalog_pagination and len(needed) >= max(1, threshold):
            for page_num, cards, _cinfo in self.iter_catalog_pages(max_pages=max_pages):
                for card in cards:
                    if not isinstance(card, dict):
                        continue
                    cid = card.get("nmID") or card.get("nmId")
                    if cid is None:
                        continue
                    try:
                        ic = int(cid)
                    except (TypeError, ValueError):
                        continue
                    if ic in needed and ic not in found:
                        found[ic] = card

                _p(
                    {
                        "mode": "catalog",
                        "page": page_num,
                        "found": len(found),
                        "needed": len(needed),
                        "batch": len(cards),
                    }
                )

                if len(found) >= len(needed):
                    break

        missing = sorted(needed - set(found.keys()))
        for i, nmi in enumerate(missing, start=1):
            try:
                card = self.get_card_by_nm_id(nmi)
                if card:
                    found[nmi] = card
            except WbContentError as exc:
                low = str(exc).lower()
                if "авториз" in low or "401" in low or "403" in low:
                    raise
                self.logger.warning("WB Content API nm=%s: %s", nmi, exc)
            _p(
                {
                    "mode": "per_nm",
                    "done": i,
                    "total": len(missing),
                    "found": len(found),
                    "needed": len(needed),
                }
            )

        return found
