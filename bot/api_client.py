"""Связь с панелью «Кудри».

Своей базы у бота нет и быть не должно: склад живёт в YCLIENTS, а мастера — в
панели. Бот только показывает кнопки и пересказывает ответы. Так у данных
остаётся один хозяин, и две программы не пишут в одно место одновременно.
"""

from __future__ import annotations

from typing import Any

import httpx

from .config import Settings

API_PREFIX = "/api/inventory-bot"


class ApiError(Exception):
    """Ошибка, текст которой можно показать мастеру как есть."""

    def __init__(self, message: str, code: str = "api_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class PanelClient:
    def __init__(self, settings: Settings) -> None:
        self._base = settings.api_base_url + API_PREFIX
        self._client = httpx.AsyncClient(
            timeout=settings.request_timeout_s,
            headers={
                "x-inventory-key": settings.api_key,
                "Content-Type": "application/json",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, path: str, **kwargs: Any) -> dict:
        try:
            response = await self._client.request(method, f"{self._base}{path}", **kwargs)
        except httpx.RequestError:
            # Молчать нельзя: мастер решит, что расход записан, и второй раз
            # вносить не станет.
            raise ApiError(
                "Панель не ответила. Результат операции пока неизвестен. Проверьте карточку; для повторного подтверждения используйте ту же кнопку, не создавайте новый приход.",
                "unavailable",
            )

        if response.status_code == 401:
            raise ApiError(
                "Бот не опознан панелью. Сообщите владельцу — нужен общий ключ.",
                "unauthorized",
            )

        try:
            body = response.json()
        except ValueError:
            raise ApiError("Панель ответила непонятно. Попробуйте ещё раз.", "bad_response")

        if response.status_code >= 400 or not body.get("ok", False):
            raise ApiError(
                body.get("error") or "Не получилось выполнить операцию.",
                body.get("code") or "api_error",
            )
        return body

    # --- доступ ---------------------------------------------------------

    async def whoami(self, telegram_id: int) -> dict | None:
        body = await self._call("GET", "/me", params={"telegramId": telegram_id})
        return body.get("master")

    async def memberships(self, telegram_id: int) -> dict:
        return await self._call("GET", "/memberships", params={"telegramId": telegram_id})

    async def preview_registration(self, **payload: Any) -> dict:
        return (await self._call("POST", "/registration/preview", json=payload))["preview"]

    async def confirm_registration(self, telegram_id: int, token: str) -> dict:
        return await self._call("POST", "/registration/confirm", json={
            "telegramId": telegram_id, "token": token, "confirmed": True
        })

    async def select_organization(self, telegram_id: int, organization_id: str) -> str:
        body = await self._call("POST", "/memberships/select", json={
            "telegramId": telegram_id, "organizationId": organization_id
        })
        return str(body["activeOrganizationId"])

    async def activate_with_reason(self, code: str, telegram_id: int) -> tuple[bool, str]:
        """Активация, где причина отказа нужна словами, а не как ошибка."""
        body = await self._call(
            "POST", "/activate", json={"code": code, "telegramId": telegram_id}
        )
        if body.get("ok") and body.get("name"):
            return True, body["name"]
        return False, body.get("reason") or "unknown"

    # --- позиции --------------------------------------------------------

    async def suggestions(self, telegram_id: int) -> list[dict]:
        return (await self._call("GET", "/suggest", params={"telegramId": telegram_id})).get("positions", [])

    async def search(self, term: str, telegram_id: int) -> list[dict]:
        return (await self._call(
            "GET", "/search", params={"term": term, "telegramId": telegram_id}
        )).get("positions", [])

    async def find_good(self, *, telegram_id: int, title: str = "", barcode: str = "") -> dict:
        data = await self._call("GET", "/find-good", params={
            "telegramId": telegram_id, "title": title, "barcode": barcode
        })
        return {
            "exact": data.get("exact"),
            "matches": data.get("matches", []),
        }

    async def open_positions(self, telegram_id: int) -> list[dict]:
        return (await self._call("GET", "/open", params={"telegramId": telegram_id})).get("positions", [])

    async def categories(self, telegram_id: int) -> list[dict]:
        return (await self._call(
            "GET", "/categories", params={"telegramId": telegram_id}
        )).get("categories", [])

    async def catalog(self, telegram_id: int, category_id: int | None = None) -> dict:
        params: dict[str, int] = {"telegramId": telegram_id}
        if category_id is not None:
            params["categoryId"] = category_id
        return await self._call("GET", "/catalog", params=params)

    async def preview_category(self, telegram_id: int, parent_id: int, title: str) -> dict:
        return await self._call("POST", "/categories/preview", json={
            "telegramId": telegram_id, "parentId": parent_id, "title": title
        })

    async def create_category(self, telegram_id: int, parent_id: int, title: str) -> dict:
        return await self._call("POST", "/categories/confirm", json={
            "telegramId": telegram_id, "parentId": parent_id, "title": title, "confirmed": True
        })

    async def preview_category_rename(self, telegram_id: int, category_id: int, title: str) -> dict:
        return await self._call("POST", "/categories/rename/preview", json={
            "telegramId": telegram_id, "categoryId": category_id, "title": title
        })

    async def rename_category(self, telegram_id: int, category_id: int, title: str) -> dict:
        return await self._call("POST", "/categories/rename/confirm", json={
            "telegramId": telegram_id, "categoryId": category_id,
            "title": title, "confirmed": True
        })

    async def preview_category_delete(self, telegram_id: int, category_id: int) -> dict:
        return await self._call("POST", "/categories/delete/preview", json={
            "telegramId": telegram_id, "categoryId": category_id
        })

    async def delete_category(self, telegram_id: int, category_id: int) -> dict:
        return await self._call("POST", "/categories/delete/confirm", json={
            "telegramId": telegram_id, "categoryId": category_id, "confirmed": True
        })

    async def preview_good_delete(self, telegram_id: int, good_id: int) -> dict:
        return await self._call("POST", "/goods/delete/preview", json={
            "telegramId": telegram_id, "goodId": good_id
        })

    async def delete_good(self, telegram_id: int, good_id: int) -> dict:
        return await self._call("POST", "/goods/delete/confirm", json={
            "telegramId": telegram_id, "goodId": good_id, "confirmed": True
        })

    async def settings_users(self, telegram_id: int) -> list[dict]:
        return (await self._call("GET", "/settings/users", params={"telegramId": telegram_id})).get("users", [])

    async def add_settings_user(self, telegram_id: int, name: str) -> dict:
        return (await self._call("POST", "/settings/users", json={
            "telegramId": telegram_id, "name": name
        }))["member"]

    async def service_users(self, telegram_id: int, page: int = 1) -> dict:
        return await self._call("GET", "/service/users", params={
            "telegramId": telegram_id, "page": page, "pageSize": 8
        })

    # --- операции -------------------------------------------------------

    async def inventory_cycle(self, telegram_id: int) -> dict:
        return (await self._call("GET", "/cycle", params={"telegramId": telegram_id}))["cycle"]

    async def cycle_action(self, telegram_id: int, campaign_id: str, category_id: str,
                           action: str, *, revision: int = 0, **payload: Any) -> dict:
        from urllib.parse import quote
        if action not in {"start", "continue", "recheck", "complete"}:
            raise ValueError("Unknown campaign action")
        body = await self._call(
            "POST", f"/cycle/categories/{quote(str(category_id), safe='')}/{action}",
            json={**payload, "telegramId": telegram_id, "campaignId": campaign_id, "revision": revision},
        )
        return body["cycle"]

    async def cycle_preview(self, *, telegram_id: int, campaign_id: str,
                            category_id: str, good_id: int, counted: float, revision: int = 0) -> dict:
        return (await self._call("POST", "/cycle/preview", json={
            "telegramId": telegram_id, "campaignId": campaign_id,
            "categoryId": category_id, "goodId": good_id, "counted": counted, "revision": revision,
        }))["preview"]

    async def receive(self, **payload: Any) -> dict:
        return await self._call("POST", "/receive", json=payload)

    async def deplete(self, telegram_id: int, good_id: int) -> dict:
        return await self._call(
            "POST", "/deplete", json={"telegramId": telegram_id, "goodId": good_id}
        )

    # --- запись чека ----------------------------------------------------

    async def receipt_preview(self, *, telegram_id: int, items: list[dict],
                              merchant: str = "", purchased_at: str = "",
                              total: float | None = None, excluded_items: list[dict] | None = None) -> dict:
        """Готовит предпросмотр прихода. В YCLIENTS не пишет, выдаёт токен."""
        body = await self._call("POST", "/receipt/preview", json={
            "telegramId": telegram_id,
            "merchant": merchant,
            "purchasedAt": purchased_at,
            "total": total,
            "items": items,
            "excludedItems": list(excluded_items or []),
        })
        return body["preview"]

    async def receipt_confirm(self, *, telegram_id: int, token: str,
                              fingerprint: str | None = None, confirmed: bool = True) -> dict:
        """Проводит приход по строкам. Повторный вызов безопасен и продолжает запись."""
        body = await self._call("POST", "/receipt/confirm", json={
            "telegramId": telegram_id,
            "token": token,
            "fingerprint": fingerprint,
            "confirmed": confirmed,
        })
        return body["result"]


    async def material(self, telegram_id: int, good_id: int) -> dict:
        return (await self._call("GET", f"/materials/{good_id}", params={"telegramId": telegram_id}))["material"]

    async def material_preview(self, telegram_id: int, good_id: int, kind: str, **payload: Any) -> dict:
        if kind not in {"edit", "writeoff", "count", "undo"}:
            raise ValueError("Unknown material action")
        return (await self._call("POST", f"/materials/{good_id}/{kind}/preview", json={"telegramId": telegram_id, **payload}))["preview"]

    async def material_confirm(self, telegram_id: int, good_id: int, kind: str, token: str) -> dict:
        if kind not in {"edit", "writeoff", "count", "undo"}:
            raise ValueError("Unknown material action")
        return (await self._call("POST", f"/materials/{good_id}/{kind}/confirm", json={"telegramId": telegram_id, "token": token, "confirmed": True}))["result"]

    async def shopping(self, telegram_id: int) -> list[dict]:
        return (await self._call("GET", "/shopping", params={"telegramId": telegram_id}))["items"]

    async def material_order(self, telegram_id: int, good_id: int, ordered: bool) -> dict:
        return (await self._call("POST", f"/materials/{good_id}/order", json={"telegramId": telegram_id, "ordered": ordered, "confirmed": True}))["material"]


    async def inventory_count_preview(self, telegram_id: int, category_id: int, counts: list[dict]) -> dict:
        return (await self._call("POST", "/inventory-count/preview", json={"telegramId": telegram_id, "categoryId": category_id, "counts": counts}))["preview"]

    async def inventory_count_confirm(self, telegram_id: int, token: str) -> dict:
        return (await self._call("POST", "/inventory-count/confirm", json={"telegramId": telegram_id, "token": token, "confirmed": True}))["result"]
