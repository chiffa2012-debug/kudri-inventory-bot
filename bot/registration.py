from __future__ import annotations

from html import escape

from aiogram.fsm.state import State, StatesGroup


class Registration(StatesGroup):
    waiting_title = State()
    waiting_source = State()
    waiting_owner_name = State()
    waiting_confirm = State()


SOURCE_LABELS = {
    "internal": "Внутренняя база бота",
    "yclients": "Подключить YCLIENTS",
    "later": "Внутренняя база, YCLIENTS позже",
}


def registration_preview_text(title: str, owner_name: str, source_choice: str) -> str:
    source = SOURCE_LABELS.get(source_choice, "Не выбран")
    return (
        "<b>Проверьте новую организацию</b>\n\n"
        f"Название: <b>{escape(str(title))}</b>\n"
        f"Владелец: <b>{escape(str(owner_name))}</b>\n"
        f"Хранилище: <b>{escape(source)}</b>\n\n"
        "Категории, материалы и остатки будут пустыми."
    )
