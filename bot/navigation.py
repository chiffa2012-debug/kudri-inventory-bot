"""Глобальная навигация reply-клавиатуры.

Главные кнопки являются переходами между разделами, поэтому незавершённый FSM
не должен воспринимать их как название товара, категории или количество.
"""

from __future__ import annotations

from aiogram import BaseMiddleware

from .keyboards import (
    BTN_CREATE_ORGANIZATION,
    BTN_DEPLETE,
    BTN_INVENTORY,
    BTN_JOIN_ORGANIZATION,
    BTN_RECEIPT,
    BTN_RECEIVE,
    BTN_SETTINGS,
    BTN_SWITCH_ORGANIZATION,
)


MAIN_MENU_ACTIONS = frozenset({
    BTN_RECEIVE,
    BTN_DEPLETE,
    BTN_RECEIPT,
    BTN_INVENTORY,
    BTN_SETTINGS,
    BTN_CREATE_ORGANIZATION,
    BTN_JOIN_ORGANIZATION,
    BTN_SWITCH_ORGANIZATION,
})


class MainMenuResetMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        if str(getattr(event, "text", "") or "").strip() in MAIN_MENU_ACTIONS:
            state = data.get("state")
            if state is not None:
                await state.clear()
        return await handler(event, data)
