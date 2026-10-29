"""Короткий приватный журнал действий без содержимого сообщений."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message


_SAFE = re.compile(r"[^a-zA-Z0-9_.:-]+")
_BUTTONS = {
    "/start": "start",
    "🏢 Создать организацию": "registration_start",
    "🔑 Войти по коду": "invite_start",
    "🏢 Сменить организацию": "organization_switch",
    "⚙️ Настройки": "settings_open",
    "📦 Пополнить": "receive_start",
    "🔻 Закончилось": "deplete_start",
    "🧾 Фото чека": "receipt_start",
    "📋 Инвентаризация": "inventory_start",
}


def _safe(value: Any, fallback: str = "unknown", limit: int = 80) -> str:
    clean = _SAFE.sub("_", str(value or "").strip()).strip("_")[:limit]
    return clean or fallback


def classify_update(*, text: str = "", callback_data: str = "", has_photo: bool = False) -> str:
    if has_photo:
        return "photo_upload"
    clean_text = str(text or "").strip()
    if clean_text in _BUTTONS:
        return _BUTTONS[clean_text]
    callback = str(callback_data or "")
    callback_rules = (
        ("reg:source:", "registration_source"),
        ("reg:owner:", "registration_owner"),
        ("reg:confirm", "registration_confirm"),
        ("reg:cancel", "registration_cancel"),
        ("org:select:", "organization_select"),
        ("settings:user-add", "user_add"),
        ("settings:service-users", "service_users_open"),
        ("settings:users", "users_open"),
        ("settings:confirm-good-delete", "material_delete_confirm"),
        ("settings:good:", "material_card"),
        ("material:archive:", "material_delete_preview"),
        ("material:confirm", "material_confirm"),
        ("material:", "material_action"),
        ("unit:", "unit_select"),
        ("settings:cat-delete:", "category_delete"),
        ("settings:cat-rename:", "category_rename"),
        ("settings:cat-add:", "category_add"),
        ("receipt:confirm", "receipt_preview_confirm"),
        ("receipt:write", "receipt_write_confirm"),
        ("receive:confirm", "receive_confirm"),
        ("yes:receive", "receive_confirm"),
        ("yes:dep", "deplete_confirm"),
        ("cycle:", "inventory_action"),
    )
    for prefix, event in callback_rules:
        if callback.startswith(prefix):
            return event
    if callback:
        return "button_action"
    if clean_text.startswith("/"):
        return "command"
    return "text_input"


class ActionLog:
    def __init__(
        self,
        path: str | Path,
        *,
        retention: timedelta = timedelta(hours=24),
        max_entries: int = 500,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = Path(path)
        self.retention = retention
        self.max_entries = max(1, int(max_entries))
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()

    @staticmethod
    def actor(telegram_id: Any) -> str:
        digest = hashlib.sha256(f"inventory-action:{telegram_id}".encode()).hexdigest()[:12]
        return f"u_{digest}"

    def write(
        self,
        event: str,
        *,
        telegram_id: Any,
        state: str | None = None,
        status: str = "ok",
        error_code: str | None = None,
        organization_id: str | None = None,
        **_ignored: Any,
    ) -> None:
        now = self.clock().astimezone(timezone.utc)
        row = {
            "at": now.isoformat().replace("+00:00", "Z"),
            "event": _safe(event),
            "actor": self.actor(telegram_id),
            "state": _safe(state, "none"),
            "status": _safe(status),
        }
        if error_code:
            row["errorCode"] = _safe(error_code, limit=48)
        if organization_id:
            row["organizationId"] = _safe(organization_id, limit=64)

        cutoff = now - self.retention
        with self._lock:
            rows = []
            try:
                for line in self.path.read_text(encoding="utf-8").splitlines():
                    try:
                        saved = json.loads(line)
                        at = datetime.fromisoformat(str(saved.get("at", "")).replace("Z", "+00:00"))
                        if at >= cutoff:
                            rows.append(saved)
                    except (ValueError, TypeError, json.JSONDecodeError):
                        continue
            except FileNotFoundError:
                pass
            rows.append(row)
            rows = rows[-self.max_entries:]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
            temporary.write_text(
                "".join(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n" for item in rows),
                encoding="utf-8",
            )
            os.replace(temporary, self.path)


class ActionLogMiddleware(BaseMiddleware):
    def __init__(self, action_log: ActionLog) -> None:
        self.action_log = action_log

    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: Message | CallbackQuery,
        data: dict[str, Any],
    ) -> Any:
        message = event.message if isinstance(event, CallbackQuery) else event
        user = event.from_user
        state = data.get("state")
        state_name = await state.get_state() if state else None
        action = classify_update(
            text=getattr(message, "text", "") or "",
            callback_data=event.data if isinstance(event, CallbackQuery) else "",
            has_photo=bool(getattr(message, "photo", None)),
        )
        try:
            self.action_log.write(action, telegram_id=user.id, state=state_name, status="started")
        except OSError:
            pass
        try:
            result = await handler(event, data)
        except Exception as error:
            try:
                self.action_log.write(
                    action,
                    telegram_id=user.id,
                    state=state_name,
                    status="error",
                    error_code=getattr(error, "code", None) or type(error).__name__,
                )
            except OSError:
                pass
            raise
        try:
            self.action_log.write(action, telegram_id=user.id, state=state_name, status="ok")
        except OSError:
            pass
        return result
