"""Сценарии бота: пополнить и «закончилось».

Добавлен поток сканирования этикетки по фото и сверка SKU.
Перед тем как принять новое SKU, мы сначала проверяем, нет ли такого товара
в каталоге, и просим явное подтверждение для избежания дублей.
Для чистоты чата используем редактирование клавиатур и удаление предыдущего
служебного сообщения в одном сценарии.
"""

from __future__ import annotations

import asyncio
import logging
from html import escape
from uuid import uuid4

from aiogram import F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .api_client import ApiError, PanelClient
from .config import Settings
from .keyboards import (
    BTN_DEPLETE,
    BTN_RECEIPT,
    BTN_RECEIVE,
    BTN_SETTINGS,
    BTN_CREATE_ORGANIZATION,
    BTN_JOIN_ORGANIZATION,
    BTN_SWITCH_ORGANIZATION,
    category_admin_keyboard,
    category_goods_page,
    category_change_confirm_keyboard,
    good_delete_confirm_keyboard,
    catalog_keyboard,
    categories_keyboard,
    category_create_confirm_keyboard,
    confirm_keyboard,
    main_menu,
    organization_switch_keyboard,
    owner_name_keyboard,
    positions_keyboard,
    receipt_lines_keyboard,
    receipt_item_keyboard,
    receipt_category_keyboard,
    receipt_preview_keyboard,
    receipt_result_keyboard,
    receipt_write_keyboard,
    receive_confirm_keyboard,
    quantity_keyboard,
    price_keyboard,

    scan_duplicates_keyboard,
    scan_match_keyboard,
    scan_photo_collection_keyboard,
    recognition_retry_keyboard,
    photo_kind_keyboard,
    settings_keyboard,
    registration_confirm_keyboard,
    registration_menu,
    source_menu,
    users_keyboard,
    service_users_keyboard,
    unit_keyboard,
    receive_result_keyboard,
    material_keyboard,
    material_edit_keyboard,
    material_confirm_keyboard,
    shopping_keyboard,
)
from .parsing import (
    format_amount,
    looks_like_invite_code,
    parse_barcode_like,
    parse_price,
    parse_quantity,
)
from .scan_history import scan_history
from .receipt import (
    ReceiptReadResult,
    build_receipt_payload,
    classify_receipt_line,
    scanned_receipt,
    parse_manual_line,
    read_receipt_from_bytes,
    validate_receipt,
)
from .vision import read_label_from_bytes
from .photo_intake import merge_label_reads, read_auto_from_bytes
from .vision import LabelReadResult
from .registration import Registration, registration_preview_text

router = Router()
log = logging.getLogger("kudri-inventory-bot.handlers")

PHOTO_UNAVAILABLE = "Распознавание фото временно недоступно."
MANUAL_LABEL_PROMPT = (
    PHOTO_UNAVAILABLE + "\nВведите название расходника вручную, указав объём или размер. "
    "Я проверю совпадения в каталоге. Для выхода — /cancel."
)
MANUAL_RECEIPT_PROMPT = (
    PHOTO_UNAVAILABLE + "\nВведите строки чека вручную, каждую с новой строки:\n"
    "<code>Название | количество | цена за единицу | сумма</code>\n"
    "Неизвестное значение обозначьте знаком <code>?</code>. Для выхода — /cancel."
)

ACTIVATION_ERRORS = {
    "not_a_code": "Код состоит из шести цифр. Проверьте и пришлите ещё раз.",
    "unknown_code": "Такого кода нет. Возможно, его уже использовали — попросите новый.",
    "expired_code": "Код устарел. Попросите у владельца новый.",
    "telegram_already_linked": "Этот Telegram уже привязан к другому мастеру.",
    "no_telegram_id": "Не вижу, кто вы. Напишите боту из личного чата.",
}

class Receive(StatesGroup):
    waiting_code = State()
    waiting_search = State()
    waiting_title = State()
    waiting_category = State()
    waiting_unit = State()
    waiting_quantity = State()
    waiting_package_count = State()
    receiving = State()
    waiting_price = State()
    waiting_receive_confirm = State()
    waiting_scan_photo = State()
    waiting_scan_confirm = State()
    waiting_category_name = State()
    waiting_category_confirm = State()


class Deplete(StatesGroup):
    waiting_confirm = State()


class Receipt(StatesGroup):
    waiting_photo = State()
    waiting_preview = State()
    waiting_write_confirm = State()

    waiting_edit_line = State()
    waiting_manual_lines = State()


class PhotoIntake(StatesGroup):
    waiting_kind = State()


class AdminSettings(StatesGroup):
    waiting_add_name = State()
    waiting_add_confirm = State()
    waiting_rename_name = State()
    waiting_rename_confirm = State()
    waiting_delete_confirm = State()
    waiting_good_delete_confirm = State()
    waiting_user_name = State()


async def _clean_prev_prompt(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    prev_id = data.get("prompt_message_id")
    if not prev_id:
        return
    if prev_id == message.message_id:
        return
    try:
        await message.bot.delete_message(message.chat.id, prev_id)
    except Exception:
        pass


async def _send_prompt(
    message: Message,
    state: FSMContext,
    text: str,
    reply_markup=None,
    parse_mode: str | None = None,
    clear_state: bool = False,
) -> Message:
    await _clean_prev_prompt(message, state)
    if clear_state:
        await state.clear()
    sent = await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
    await state.update_data(prompt_message_id=sent.message_id)
    return sent


async def require_master(message: Message, panel: PanelClient, state: FSMContext) -> dict | None:
    """Кто пишет. Незнакомого сразу отправляем вводить код, а не в меню."""
    master = await panel.whoami(message.from_user.id)
    if master:
        return master
    await state.set_state(Receive.waiting_code)
    await message.answer(
        "Здравствуйте! Я веду учёт расходных материалов салона.\n\n"
        "Пришлите шестизначный код доступа — его выдаёт владелец."
    )
    return None


async def _start_confirm_with_existing(
    message: Message | CallbackQuery,
    state: FSMContext,
    title: str,
    barcode: str,
    good_id: int,
    matches: list[dict],
    duplicate: bool = False,
) -> None:
    payload = [
        f"Найдено совпадение:\n<b>{_label_of_position(matches[0])}</b>"
        if matches
        else "Найденная позиция не определилась точно."
    ]
    message_text = payload[0] + "\n\n" + "Это именно она?"
    if duplicate:
        message_text += "\n⚠️ Такой штрихкод уже был сканирован недавно. Проверьте, не дубликат ли это."

    if isinstance(message, CallbackQuery):
        await state.update_data(
            scan_candidate_good_id=good_id,
            candidate_title=matches[0].get("title", title) if matches else title,
            candidate_barcode=barcode,
            candidate_unit=(matches[0].get("unit") if matches else None) or "шт",
        )
        await state.set_state(Receive.waiting_scan_confirm)
        await message.message.edit_text(
            message_text,
            reply_markup=scan_match_keyboard(good_id),
            parse_mode="HTML",
        )
    else:
        await state.update_data(
            scan_candidate_good_id=good_id,
            candidate_title=matches[0].get("title", title) if matches else title,
            candidate_barcode=barcode,
            candidate_unit=(matches[0].get("unit") if matches else None) or "шт",
        )
        await state.set_state(Receive.waiting_scan_confirm)
        await _send_prompt(message, state, message_text, reply_markup=scan_match_keyboard(good_id), parse_mode="HTML")

def _label_of_position(position: dict) -> str:
    return position.get("title", "позицию")


def _quantity_question(unit: str, prefix: str = "Какая фасовка?") -> str:
    clean = "мл" if unit == "мл" else "шт"
    example = "100" if clean == "мл" else "3"
    return f"{prefix}\nСколько {clean} в одной упаковке или тюбике? Например 100. Для поштучного товара — 1."


async def _ask_new_good_unit(target: Message | CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Receive.waiting_unit)
    text = (
        "Как учитывать этот материал?\n"
        "• <b>шт</b> — палочки, перчатки, упаковки\n"
        "• <b>мл</b> — краска, шампунь, жидкости"
    )
    if hasattr(target, "message") and hasattr(target, "data"):
        await target.message.edit_text(text, parse_mode="HTML", reply_markup=unit_keyboard())
        await target.answer()
    else:
        await _send_prompt(target, state, text, parse_mode="HTML", reply_markup=unit_keyboard())


def _receipt_number(value: float | None) -> str:
    if value is None:
        return "?"
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def _receipt_preview(
    receipt: ReceiptReadResult,
    sku_matches: list[dict | None],
    decisions: list[dict] | None = None,
) -> str:
    rows = ["<b>Проверьте чек перед подготовкой данных</b>"]
    decisions = list(decisions or [])
    if receipt.merchant:
        rows.append(f"Магазин: {escape(receipt.merchant)}")
    if receipt.purchased_at:
        rows.append(f"Дата: {escape(receipt.purchased_at)}")

    for index, line in enumerate(receipt.lines):
        line = classify_receipt_line(line)
        match = sku_matches[index] if index < len(sku_matches) else None
        decision = decisions[index] if index < len(decisions) else {}
        if decision.get("skip"):
            sku_status = "не добавлять"
        elif match:
            sku_status = "найдена в базе"
        elif decision.get("categoryId"):
            sku_status = "новая · категория выбрана"
        else:
            sku_status = "новая · выберите категорию"
        rows.append(
            f"\n{index + 1}. <b>{escape(line.title or 'Без названия')}</b>"
            f"\n{_receipt_number(line.quantity)} × {_receipt_number(line.unit_price)} ₽"
            f" = {_receipt_number(line.line_total)} ₽ · {sku_status}"
        )
        if line.issues:
            rows.append("⚠️ " + escape("; ".join(line.issues)))
        if line.requires_review and not decision.get("skip"):
            if decision.get("reviewDecision") == "include":
                rows.append("✅ Вы подтвердили: добавить как расходник.")
            else:
                rows.append("⚠️ " + escape(line.review_reason) + " Откройте строку и выберите: на склад или исключить.")

    rows.append(f"\nИтог чека: {_receipt_number(receipt.total)} ₽")
    if receipt.calculated_total is not None:
        rows.append(f"Сумма строк: {_receipt_number(receipt.calculated_total)} ₽")
    excluded = [line for index, line in enumerate(receipt.lines) if index < len(decisions) and decisions[index].get("skip")]
    if excluded:
        excluded_total = None if any(line.line_total is None for line in excluded) else sum(line.line_total for line in excluded)
        rows.append(f"Исключено: {len(excluded)} строк · {_receipt_number(excluded_total)} ₽")
    if receipt.issues:
        rows.append("⚠️ " + escape("; ".join(receipt.issues)))
    rows.append("\nПроверьте строки, затем одной кнопкой подтвердите добавление на склад.")
    return "\n".join(rows)


async def _receipt_matches(panel: PanelClient, receipt: ReceiptReadResult, telegram_id: int) -> list[dict | None]:
    matches: list[dict | None] = []
    for line in receipt.lines:
        found = await panel.find_good(telegram_id=telegram_id, title=line.title, barcode="")
        matches.append(found.get("exact"))
    return matches

def _receipt_write_prompt(lines: list[dict], excluded_lines: list[dict] | None = None, excluded_total: float | None = 0) -> str:
    """Отдельное подтверждение: что именно уйдёт в YCLIENTS построчно."""
    rows = ["<b>Подтвердите добавление на склад</b>"]
    for index, line in enumerate(lines):
        rows.append(
            f"{index + 1}. <b>{escape(str(line.get('title') or 'Без названия'))}</b> — "
            f"{_receipt_number(line.get('amount'))} × "
            f"{_receipt_number(line.get('pricePerUnit'))} ₽"
        )
    if excluded_lines:
        rows.append(f"\nНе добавляем: {len(excluded_lines)} строк · {_receipt_number(excluded_total)} ₽")
    rows.append("\nСтроки запишутся по очереди. Отмена остановит сценарий.")
    return "\n".join(rows)


def _receipt_result_text(result: dict) -> str:
    """Построчный итог записи: что прошло, что упало и что осталось."""
    marks = {"done": "✅", "failed": "❌", "pending": "⏳"}
    rows = ["<b>Добавление на склад</b>"]
    for line in result.get("lines", []):
        mark = marks.get(line.get("status"), "•")
        rows.append(
            f"{mark} {escape(str(line.get('title') or 'Без названия'))} — "
            f"{_receipt_number(line.get('amount'))}"
        )
        if line.get("error"):
            rows.append(f"   ⚠️ {escape(str(line['error']))}")
    if result.get("status") == "partial":
        rows.append(
            "\n⚠️ Часть строк не записана. Нажмите «Продолжить запись» — "
            "уже записанные строки не повторятся."
        )
    elif result.get("duplicate"):
        rows.append("\n✅ Этот чек уже был записан ранее — повторно ничего не ушло.")
    else:
        rows.append("\n✅ Все строки записаны.")
    return "\n".join(rows)



# --- вход -------------------------------------------------------------------

@router.message(CommandStart())
async def on_start(message: Message, state: FSMContext, panel: PanelClient) -> None:
    await state.clear()
    memberships = await panel.memberships(message.from_user.id)
    organizations = memberships.get("organizations", [])
    if not organizations:
        await _send_prompt(
            message,
            state,
            "Здравствуйте! Создайте отдельную организацию или войдите по коду приглашения.",
            reply_markup=registration_menu(),
        )
        return
    master = await panel.whoami(message.from_user.id)
    if master:
        await _send_prompt(
            message,
            state,
            f"С возвращением, {master['name']}. Что делаем?",
            reply_markup=main_menu(can_manage=True, can_switch=len(organizations) > 1),
        )
        return

    await state.set_state(Receive.waiting_code)
    await message.answer(
        "Здравствуйте! Я веду учёт расходных материалов.\n\n"
        "Чтобы начать, пришлите шестизначный код доступа — его выдаёт владелец."
    )


@router.message(Command("cancel"))
async def on_cancel_command(message: Message, state: FSMContext) -> None:
    await state.clear()
    await _send_prompt(message, state, "Отменил.", reply_markup=main_menu())


@router.message(Receive.waiting_code, F.text)
async def on_invite_code(message: Message, state: FSMContext, panel: PanelClient) -> None:
    code = (message.text or "").strip()
    if not looks_like_invite_code(code):
        await message.answer(ACTIVATION_ERRORS["not_a_code"])
        return

    ok, payload = await panel.activate_with_reason(code, message.from_user.id)
    if not ok:
        await message.answer(ACTIVATION_ERRORS.get(payload, "Не получилось активировать код."))
        return

    await state.clear()
    await _send_prompt(message, state, f"Готово, {payload}. Теперь можно вести учёт.", reply_markup=main_menu())


# --- настройки владельца ----------------------------------------------------

async def _show_category_settings(
    call: CallbackQuery,
    panel: PanelClient,
    category_id: int | None = None,
    goods_page: int = 0,
) -> None:
    payload = await panel.catalog(call.from_user.id, category_id)
    if not payload.get("canManageCategories"):
        raise ApiError("Настройки доступны только владельцу", "owner_required")
    page = payload["page"]
    _, current_page, total_pages, count = category_goods_page(page, goods_page)
    page_label = f" · страница {current_page + 1}/{total_pages}" if total_pages > 1 else ""
    await call.message.edit_text(
        f"📁 <b>{escape(page['current']['title'])}</b>\n"
        f"Подкатегорий: {len(page.get('children', []))} · материалов: {count}{page_label}\n\n"
        "Выберите папку или действие:",
        parse_mode="HTML",
        reply_markup=category_admin_keyboard(page, goods_page=current_page),
    )


async def _show_registration_preview(
    message: Message,
    state: FSMContext,
    panel: PanelClient,
    owner_name: str,
    telegram_id: int,
) -> None:
    data = await state.get_data()
    preview = await panel.preview_registration(
        telegramId=telegram_id,
        title=data["registration_title"],
        ownerName=owner_name,
        sourceChoice=data["registration_source"],
        requestId=data["registration_request_id"],
    )
    await state.update_data(registration_owner_name=owner_name, registration_token=preview["token"])
    await state.set_state(Registration.waiting_confirm)
    await _send_prompt(
        message,
        state,
        registration_preview_text(preview["title"], preview["ownerName"], data["registration_source"]),
        parse_mode="HTML",
        reply_markup=registration_confirm_keyboard(),
    )


@router.message(F.text == BTN_CREATE_ORGANIZATION)
async def on_registration_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(Registration.waiting_title)
    await state.update_data(registration_request_id=f"tg-{message.from_user.id}-{uuid4().hex}")
    await _send_prompt(message, state, "Как называется организация?")


@router.message(F.text == BTN_JOIN_ORGANIZATION)
async def on_registration_join(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(Receive.waiting_code)
    await _send_prompt(message, state, "Пришлите шестизначный код приглашения.")


@router.message(Registration.waiting_title, F.text)
async def on_registration_title(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()
    if len(title) < 3:
        await message.answer("Название слишком короткое. Напишите полное название организации.")
        return
    await state.update_data(registration_title=title)
    await state.set_state(Registration.waiting_source)
    await _send_prompt(message, state, "У организации уже есть YCLIENTS?", reply_markup=source_menu())


@router.callback_query(F.data.startswith("reg:source:"), Registration.waiting_source)
async def on_registration_source(call: CallbackQuery, state: FSMContext) -> None:
    source = call.data.rsplit(":", 1)[1]
    if source not in {"internal", "yclients", "later"}:
        await call.answer("Неизвестный вариант", show_alert=True)
        return
    telegram_name = (call.from_user.full_name or "").strip()
    await state.update_data(registration_source=source, registration_telegram_name=telegram_name)
    await state.set_state(Registration.waiting_owner_name)
    await call.message.edit_text(
        "Как подписать владельца организации?",
        reply_markup=owner_name_keyboard(telegram_name),
    )
    await call.answer()


@router.callback_query(F.data == "reg:owner:telegram", Registration.waiting_owner_name)
async def on_registration_owner_telegram(call: CallbackQuery, state: FSMContext, panel: PanelClient) -> None:
    data = await state.get_data()
    name = (data.get("registration_telegram_name") or "").strip()
    if not name:
        await call.answer("Введите имя вручную", show_alert=True)
        return
    await _show_registration_preview(call.message, state, panel, name, call.from_user.id)
    await call.answer()


@router.callback_query(F.data == "reg:owner:type", Registration.waiting_owner_name)
async def on_registration_owner_type(call: CallbackQuery) -> None:
    await call.message.edit_text("Введите имя владельца.")
    await call.answer()


@router.message(Registration.waiting_owner_name, F.text)
async def on_registration_owner_name(message: Message, state: FSMContext, panel: PanelClient) -> None:
    name = (message.text or "").strip()
    if len(name) < 2:
        await message.answer("Введите имя полностью.")
        return
    await _show_registration_preview(message, state, panel, name, message.from_user.id)


@router.callback_query(F.data == "reg:confirm", Registration.waiting_confirm)
async def on_registration_confirm(call: CallbackQuery, state: FSMContext, panel: PanelClient) -> None:
    data = await state.get_data()
    result = await panel.confirm_registration(call.from_user.id, data["registration_token"])
    memberships = await panel.memberships(call.from_user.id)
    await state.clear()
    source = result["organization"].get("source")
    suffix = "\nПодключите YCLIENTS в настройках панели." if source == "yclients_pending" else ""
    await call.message.edit_text(f"✅ Организация <b>{escape(result['organization']['title'])}</b> создана.{suffix}", parse_mode="HTML")
    await call.message.answer(
        "Каталог пока пуст. Начните с настроек категорий.",
        reply_markup=main_menu(can_manage=True, can_switch=len(memberships.get("organizations", [])) > 1),
    )
    await call.answer()


@router.callback_query(F.data == "reg:cancel")
async def on_registration_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text("Регистрация отменена.")
    await call.message.answer("Выберите действие:", reply_markup=registration_menu())
    await call.answer()


@router.message(F.text == BTN_SWITCH_ORGANIZATION)
async def on_organization_switch(message: Message, state: FSMContext, panel: PanelClient) -> None:
    memberships = await panel.memberships(message.from_user.id)
    active = memberships.get("activeOrganizationId")
    organizations = [{**item, "active": item.get("id") == active} for item in memberships.get("organizations", [])]
    await _send_prompt(message, state, "Выберите организацию:", reply_markup=organization_switch_keyboard(organizations))


@router.callback_query(F.data.startswith("org:select:"))
async def on_organization_selected(call: CallbackQuery, state: FSMContext, panel: PanelClient) -> None:
    organization_id = call.data.split(":", 2)[2]
    await panel.select_organization(call.from_user.id, organization_id)
    memberships = await panel.memberships(call.from_user.id)
    selected = next(item for item in memberships["organizations"] if item["id"] == organization_id)
    await state.clear()
    await call.message.edit_text(f"✅ Выбрана организация: <b>{escape(selected['title'])}</b>", parse_mode="HTML")
    await call.message.answer(
        "Выберите действие:",
        reply_markup=main_menu(can_manage=True, can_switch=len(memberships["organizations"]) > 1),
    )
    await call.answer()


@router.callback_query(F.data == "org:cancel")
async def on_organization_switch_cancel(call: CallbackQuery) -> None:
    await call.message.edit_text("Выбор организации закрыт.")
    await call.answer()


@router.message(F.text == BTN_SETTINGS)
async def on_settings_start(message: Message, state: FSMContext, panel: PanelClient) -> None:
    master = await require_master(message, panel, state)
    if not master:
        return
    await state.clear()
    can_manage_users = master.get("role") == "owner"
    can_view_service_users = master.get("serviceOwner") is True
    await state.update_data(settings_can_manage_users=can_manage_users, settings_can_view_service_users=can_view_service_users)
    await _send_prompt(
        message, state,
        "⚙️ <b>Настройки учёта</b>\nЗдесь можно изменить структуру каталога.",
        parse_mode="HTML", reply_markup=settings_keyboard(can_manage_users=can_manage_users, can_view_service_users=can_view_service_users),
    )


@router.callback_query(F.data == "settings:home")
async def on_settings_home(call: CallbackQuery, state: FSMContext, panel: PanelClient) -> None:
    master = await panel.whoami(call.from_user.id)
    can_manage_users = bool(master and master.get("role") == "owner")
    can_view_service_users = bool(master and master.get("serviceOwner") is True)
    await state.clear()
    await state.update_data(settings_can_manage_users=can_manage_users, settings_can_view_service_users=can_view_service_users)
    await call.message.edit_text("⚙️ <b>Настройки учёта</b>", parse_mode="HTML", reply_markup=settings_keyboard(can_manage_users=can_manage_users, can_view_service_users=can_view_service_users))
    await call.answer()


@router.callback_query(F.data == "settings:close")
async def on_settings_close(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text("Настройки закрыты.")
    await call.message.answer("Выберите действие:", reply_markup=main_menu(can_manage=True))
    await call.answer()


@router.callback_query(F.data == "settings:categories")
async def on_settings_categories(call: CallbackQuery, state: FSMContext, panel: PanelClient) -> None:
    await state.clear()
    await _show_category_settings(call, panel)
    await call.answer()


@router.callback_query(F.data.startswith("settings:cat-page:"))
async def on_settings_category_page(call: CallbackQuery, state: FSMContext, panel: PanelClient) -> None:
    _, _, category_id, page_index = call.data.split(":", 3)
    await state.clear()
    await _show_category_settings(call, panel, int(category_id), int(page_index))
    await call.answer()


@router.callback_query(F.data == "settings:page-current")
async def on_settings_page_current(call: CallbackQuery) -> None:
    await call.answer()


@router.callback_query(F.data.startswith("settings:cat:"))
async def on_settings_category(call: CallbackQuery, state: FSMContext, panel: PanelClient) -> None:
    await state.clear()
    await _show_category_settings(call, panel, int(call.data.rsplit(":", 1)[1]))
    await call.answer()


@router.callback_query(F.data.startswith("settings:cat-add:"))
async def on_settings_category_add(call: CallbackQuery, state: FSMContext) -> None:
    category_id = int(call.data.rsplit(":", 1)[1])
    await state.set_state(AdminSettings.waiting_add_name)
    await state.update_data(settings_category_id=category_id, prompt_message_id=call.message.message_id)
    await call.message.edit_text("Введите название новой подкатегории.")
    await call.answer()


@router.message(AdminSettings.waiting_add_name, F.text)
async def on_settings_category_add_name(message: Message, state: FSMContext, panel: PanelClient) -> None:
    data = await state.get_data()
    preview = (await panel.preview_category(
        message.from_user.id, data["settings_category_id"], message.text
    ))["preview"]
    await state.update_data(settings_title=preview["title"], settings_category_id=preview["parentId"])
    await state.set_state(AdminSettings.waiting_add_confirm)
    await _send_prompt(
        message, state,
        f"Создать <b>{escape(preview['title'])}</b> внутри <b>{escape(preview['parentTitle'])}</b>?",
        parse_mode="HTML", reply_markup=category_change_confirm_keyboard("add"),
    )


@router.callback_query(F.data == "settings:confirm-add", AdminSettings.waiting_add_confirm)
async def on_settings_category_add_confirm(call: CallbackQuery, state: FSMContext, panel: PanelClient) -> None:
    data = await state.get_data()
    await panel.create_category(call.from_user.id, data["settings_category_id"], data["settings_title"])
    parent_id = data["settings_category_id"]
    await state.clear()
    await _show_category_settings(call, panel, parent_id)
    await call.answer("Категория создана")


@router.callback_query(F.data.startswith("settings:cat-rename:"))
async def on_settings_category_rename(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminSettings.waiting_rename_name)
    await state.update_data(settings_category_id=int(call.data.rsplit(":", 1)[1]), prompt_message_id=call.message.message_id)
    await call.message.edit_text("Введите новое название категории.")
    await call.answer()


@router.message(AdminSettings.waiting_rename_name, F.text)
async def on_settings_category_rename_name(message: Message, state: FSMContext, panel: PanelClient) -> None:
    data = await state.get_data()
    preview = (await panel.preview_category_rename(
        message.from_user.id, data["settings_category_id"], message.text
    ))["preview"]
    await state.update_data(settings_title=preview["title"], settings_parent_id=preview["parentId"])
    await state.set_state(AdminSettings.waiting_rename_confirm)
    await _send_prompt(
        message, state,
        f"Переименовать <b>{escape(preview['oldTitle'])}</b> в <b>{escape(preview['title'])}</b>?",
        parse_mode="HTML", reply_markup=category_change_confirm_keyboard("rename"),
    )


@router.callback_query(F.data == "settings:confirm-rename", AdminSettings.waiting_rename_confirm)
async def on_settings_category_rename_confirm(call: CallbackQuery, state: FSMContext, panel: PanelClient) -> None:
    data = await state.get_data()
    await panel.rename_category(call.from_user.id, data["settings_category_id"], data["settings_title"])
    parent_id = data["settings_parent_id"]
    await state.clear()
    await _show_category_settings(call, panel, parent_id)
    await call.answer("Название изменено")


@router.callback_query(F.data.startswith("settings:cat-delete:"))
async def on_settings_category_delete(call: CallbackQuery, state: FSMContext, panel: PanelClient) -> None:
    category_id = int(call.data.rsplit(":", 1)[1])
    preview = (await panel.preview_category_delete(call.from_user.id, category_id))["preview"]
    await state.set_state(AdminSettings.waiting_delete_confirm)
    await state.update_data(settings_category_id=category_id, settings_parent_id=preview["parentId"])
    await call.message.edit_text(
        f"Удалить пустую категорию <b>{escape(preview['title'])}</b>?\n"
        "Это действие нельзя отменить.",
        parse_mode="HTML", reply_markup=category_change_confirm_keyboard("delete"),
    )
    await call.answer()


@router.callback_query(F.data == "settings:confirm-delete", AdminSettings.waiting_delete_confirm)
async def on_settings_category_delete_confirm(call: CallbackQuery, state: FSMContext, panel: PanelClient) -> None:
    data = await state.get_data()
    await panel.delete_category(call.from_user.id, data["settings_category_id"])
    parent_id = data["settings_parent_id"]
    await state.clear()
    await _show_category_settings(call, panel, parent_id)
    await call.answer("Категория удалена")


@router.callback_query(F.data == "settings:users")
async def on_settings_users(call: CallbackQuery, panel: PanelClient) -> None:
    users = await panel.settings_users(call.from_user.id)
    lines = ["👥 <b>Пользователи</b>"]
    for user in users:
        role = "владелец" if user.get("role") == "owner" else "оператор"
        status = "подключён" if user.get("active") else "ожидает код"
        lines.append(f"• {escape(user.get('name', 'Без имени'))} — {role}, {status}")
    await call.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=users_keyboard())
    await call.answer()


def service_users_text(payload: dict) -> str:
    summary = payload.get("summary") or {}
    page = int(payload.get("page") or 1)
    pages = int(payload.get("pages") or 1)
    lines = [
        "👥 <b>Все пользователи сервиса</b>",
        f"Организаций: {int(summary.get('organizations') or 0)}",
        f"Пользователей: {int(summary.get('users') or 0)} "
        f"(Telegram: {int(summary.get('telegramUsers') or 0)}, web: {int(summary.get('webUsers') or 0)})",
        f"Активны: {int(summary.get('activeUsers') or 0)} · ожидают: {int(summary.get('pendingUsers') or 0)} · заблокированы: {int(summary.get('blockedUsers') or 0)}",
        f"Страница {page}/{pages}",
        "",
    ]
    roles = {"owner_product": "владелец сервиса", "owner": "владелец", "salon_owner": "владелец", "operator": "оператор", "salon_admin": "администратор"}
    statuses = {"active": "🟢", "pending": "🟡", "blocked": "🔴"}
    for user in payload.get("users") or []:
        identity = user.get("login") if user.get("type") == "web" else user.get("name")
        source = "web" if user.get("type") == "web" else "Telegram"
        organizations = ", ".join(str(item.get("title") or item.get("id") or "—") for item in user.get("organizations") or [])
        details = f"{source} · {roles.get(user.get('role'), user.get('role') or 'пользователь')} · {organizations or 'без организации'}"
        if user.get("telegramId"):
            details += f" · ID {user['telegramId']}"
        lines.append(f"{statuses.get(user.get('status'), '⚪')} <b>{escape(str(identity or 'Без имени'))}</b>\n   {escape(details)}")
    if not payload.get("users"):
        lines.append("Пользователей пока нет.")
    return "\n".join(lines)


@router.callback_query(F.data.startswith("settings:service-users"))
async def on_service_users(call: CallbackQuery, panel: PanelClient) -> None:
    parts = call.data.split(":")
    page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
    payload = await panel.service_users(call.from_user.id, page)
    await call.message.edit_text(
        service_users_text(payload), parse_mode="HTML",
        reply_markup=service_users_keyboard(payload.get("page", 1), payload.get("pages", 1)),
    )
    await call.answer()


@router.callback_query(F.data == "settings:user-add")
async def on_settings_user_add(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminSettings.waiting_user_name)
    await call.message.edit_text("Введите имя нового пользователя.")
    await call.answer()


@router.message(AdminSettings.waiting_user_name, F.text)
async def on_settings_user_add_name(message: Message, state: FSMContext, panel: PanelClient) -> None:
    name = (message.text or "").strip()
    if len(name) < 2:
        await message.answer("Введите имя полностью.")
        return
    member = await panel.add_settings_user(message.from_user.id, name)
    await state.clear()
    await _send_prompt(
        message,
        state,
        f"Пользователь <b>{escape(member['name'])}</b> создан.\n"
        f"Код входа: <code>{escape(str(member['inviteCode']))}</code>\n"
        "Код действует сутки и используется один раз.",
        parse_mode="HTML",
        reply_markup=users_keyboard(),
    )


@router.callback_query(F.data.startswith("material:archive:"))
async def on_settings_good_archive(call: CallbackQuery, state: FSMContext, panel: PanelClient) -> None:
    good_id = int(call.data.rsplit(":", 1)[1])
    preview = (await panel.preview_good_delete(call.from_user.id, good_id))["preview"]
    await state.set_state(AdminSettings.waiting_good_delete_confirm)
    await state.update_data(settings_good_id=good_id, settings_category_id=preview["categoryId"])
    stock = format_amount(preview.get("stock", 0))
    unit = preview.get("unit", "шт")
    writeoff = (
        f"\nОстаток {stock} {unit} будет сначала списан до нуля."
        if float(preview.get("stock", 0)) > 0 else ""
    )
    await call.message.edit_text(
        f"Убрать материал <b>{escape(preview['title'])}</b> из активного склада?"
        f"{writeoff}\nКарточка перейдёт в архив YCLIENTS и сможет быть восстановлена.",
        parse_mode="HTML",
        reply_markup=good_delete_confirm_keyboard(),
    )
    await call.answer()


@router.callback_query(F.data == "settings:confirm-good-delete", AdminSettings.waiting_good_delete_confirm)
async def on_settings_good_delete_confirm(call: CallbackQuery, state: FSMContext, panel: PanelClient) -> None:
    data = await state.get_data()
    await panel.delete_good(call.from_user.id, data["settings_good_id"])
    category_id = data["settings_category_id"]
    await state.clear()
    await _show_category_settings(call, panel, category_id)
    await call.answer("Материал перенесён в архив")


# --- пополнение -------------------------------------------------------------

@router.message(F.text == BTN_RECEIVE)
async def on_receive_start(message: Message, state: FSMContext, panel: PanelClient) -> None:
    if not await require_master(message, panel, state):
        return

    await state.clear()
    payload = await panel.catalog(message.from_user.id)
    page = payload["page"]
    await state.update_data(catalog_positions=page.get("goods", []))
    await _send_prompt(
        message,
        state,
        "Выберите категорию расходников:",
        reply_markup=catalog_keyboard(page, payload.get("canManageCategories", False)),
    )


@router.callback_query(F.data.startswith("catalog:"))
async def on_catalog_action(call: CallbackQuery, state: FSMContext, panel: PanelClient) -> None:
    parts = call.data.split(":")
    if len(parts) == 2 and parts[1] == "confirm-category":
        data = await state.get_data()
        await panel.create_category(call.from_user.id, data["category_parent_id"], data["category_title"])
        payload = await panel.catalog(call.from_user.id, data["category_parent_id"])
        await state.clear()
        await call.message.edit_text(
            "✅ Категория создана.",
            reply_markup=catalog_keyboard(payload["page"], payload.get("canManageCategories", False)),
        )
        await call.answer()
        return
    if len(parts) == 2:
        category_id = int(parts[1])
        payload = await panel.catalog(call.from_user.id, category_id)
        page = payload["page"]
        await state.update_data(catalog_positions=page.get("goods", []))
        await call.message.edit_text(
            f"📁 <b>{page['current']['title']}</b>\nВыберите подкатегорию или материал:",
            parse_mode="HTML",
            reply_markup=catalog_keyboard(page, payload.get("canManageCategories", False)),
        )
        await call.answer()
        return
    if len(parts) == 3 and parts[1] == "new":
        await state.set_state(Receive.waiting_title)
        await state.update_data(category_id=int(parts[2]), skip_category=True)
        await call.message.edit_text(
            "Напишите название расходника. Укажите объём или размер.\n"
            "Например: <i>Гель-лак Kira 15 мл</i>",
            parse_mode="HTML",
        )
        await call.answer()
        return
    if len(parts) == 3 and parts[1] == "scan":
        await state.set_state(Receive.waiting_scan_photo)
        await state.update_data(
            category_id=int(parts[2]),
            prompt_message_id=call.message.message_id,
            scan_photo_ids=[],
        )
        await call.message.edit_text("Отправьте фото этикетки или штрихкода.")
        await call.answer()
        return
    if len(parts) == 3 and parts[1] == "add-category":
        await state.set_state(Receive.waiting_category_name)
        await state.update_data(category_parent_id=int(parts[2]))
        await call.message.edit_text("Введите название новой подкатегории.")
        await call.answer()
        return


@router.message(Receive.waiting_category_name, F.text)
async def on_category_name(message: Message, state: FSMContext, panel: PanelClient) -> None:
    data = await state.get_data()
    preview = await panel.preview_category(message.from_user.id, data["category_parent_id"], message.text)
    item = preview["preview"]
    await state.update_data(category_title=item["title"], category_parent_id=item["parentId"])
    await state.set_state(Receive.waiting_category_confirm)
    await _send_prompt(
        message,
        state,
        f"Создать <b>{item['title']}</b> внутри <b>{item['parentTitle']}</b>?",
        parse_mode="HTML",
        reply_markup=category_create_confirm_keyboard(),
    )
@router.callback_query(F.data == "scan")
async def on_scan_start(call: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    await state.clear()
    if not settings.vision_available:
        await state.set_state(Receive.waiting_title)
        await state.update_data(
            prompt_message_id=call.message.message_id,
            scan_editing=True,
            skip_category=False,
            scan_photo_ids=[],
        )
        await call.message.edit_text(MANUAL_LABEL_PROMPT)
        await call.answer()
        return
    await state.set_state(Receive.waiting_scan_photo)
    await state.update_data(prompt_message_id=call.message.message_id, scan_photo_ids=[])
    await call.message.edit_text(
        "Отправьте фото этикетки или штрихкода.\n"
        "Я попробую распознать товар и проверить дубли в каталоге.",
    )
    await call.answer()


@router.callback_query(F.data == "new")
async def on_new_position(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Receive.waiting_title)
    await state.update_data(skip_category=False)
    await call.message.edit_text(
        "Напишите название. Укажите объём или размер — по ним считается расход.\n"
        "Например: <i>Гель-лак Kira 15 мл</i>",
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data == "find")
async def on_find_position(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Receive.waiting_search)
    await call.message.edit_text("Напишите часть названия — поищу в каталоге.")
    await call.answer()


@router.message(Receive.waiting_search, F.text)
async def on_search_typed(message: Message, state: FSMContext, panel: PanelClient) -> None:
    found = await panel.search(message.text.strip(), message.from_user.id)
    if not found:
        await message.answer(
            "Ничего не нашёл. Если это новая позиция — пришлите полное название."
        )
        await state.set_state(Receive.waiting_title)
        await state.update_data(skip_category=False)
        return

    await state.clear()
    await state.update_data(catalog_positions=found)
    await _send_prompt(message, state, "Нашёл вот что:", reply_markup=positions_keyboard(found, "recv"))


@router.message(Receive.waiting_scan_photo, F.photo)
async def on_scan_photo(
    message: Message,
    state: FSMContext,
    panel: PanelClient,
    settings: Settings,
) -> None:
    if not await require_master(message, panel, state):
        return

    if not settings.vision_available:
        await _send_prompt(message, state, MANUAL_LABEL_PROMPT, clear_state=True)
        await state.update_data(skip_category=False, scan_editing=True)
        await state.set_state(Receive.waiting_title)
        return

    data = await state.get_data()
    photo_ids = list(data.get("scan_photo_ids", []))
    if len(photo_ids) >= 3:
        await _send_prompt(
            message, state,
            "Уже получено 3 фото — этого достаточно. Нажмите «Распознать».",
            reply_markup=scan_photo_collection_keyboard(3),
        )
        return
    photo_ids.append(message.photo[-1].file_id)
    await state.set_state(Receive.waiting_scan_photo)
    await state.update_data(scan_photo_ids=photo_ids)
    await _send_prompt(
        message,
        state,
        f"Фото {len(photo_ids)} получено. Пришлите ещё фото этого же товара "
        "или нажмите «Распознать».",
        reply_markup=scan_photo_collection_keyboard(len(photo_ids)),
    )


async def _finish_label_read(
    target: Message | CallbackQuery,
    state: FSMContext,
    panel: PanelClient,
    read,
    actor_id: int,
) -> None:
    is_callback = hasattr(target, "message") and hasattr(target, "data")
    message = target.message if is_callback else target
    if not read.title and not read.barcode:
        await state.update_data(skip_category=False, scan_editing=True)
        await state.set_state(Receive.waiting_title)
        text = (
            "Не удалось распознать штрихкод и название. Можно повторить "
            "распознавание или ввести название вручную."
        )
        if is_callback:
            await message.edit_text(text, reply_markup=recognition_retry_keyboard("label"))
            await target.answer()
        else:
            await _send_prompt(message, state, text, reply_markup=recognition_retry_keyboard("label"))
        return

    title = read.title.strip() or "(без названия)"
    barcode = parse_barcode_like(f"{read.barcode} {read.raw}") or ""
    data = await state.get_data()

    payload = await panel.find_good(telegram_id=actor_id, title=title, barcode=barcode)
    exact = payload.get("exact")
    matches = payload.get("matches", [])
    duplicate = scan_history.was_recent(actor_id, barcode)
    scan_history.remember(actor_id, barcode, title)

    data["candidate_title"] = title
    data["candidate_barcode"] = barcode
    data["scan_candidate_good_id"] = int(exact["goodId"]) if exact else None
    await state.update_data(**data)

    if exact:
        await _start_confirm_with_existing(
            target,
            state,
            title,
            barcode,
            int(exact["goodId"]),
            [exact] + matches,
            duplicate=duplicate,
        )
        return

    if duplicate:
        text = (
                "⚠️ Этот штрихкод уже сканировали недавно.\n"
                f"Код: <code>{barcode}</code>\n\n"
                f"Добавить <b>{title}</b> ещё раз?"
            )
        if is_callback:
            await message.edit_text(text, reply_markup=scan_duplicates_keyboard(), parse_mode="HTML")
            await target.answer()
        else:
            await _send_prompt(message, state, text, reply_markup=scan_duplicates_keyboard(), parse_mode="HTML")
        await state.set_state(Receive.waiting_scan_confirm)
        return

    await state.update_data(
        good_id=None,
        title=title,
        barcode=barcode,
        category_id=data.get("category_id"),
        skip_category=False,
    )
    if not data.get("category_id"):
        categories = await panel.categories(actor_id)
        await state.update_data(category_options=categories)
        await state.set_state(Receive.waiting_category)
        text = f"Распознал: <b>{escape(title)}</b>\nК какой категории отнести?"
        if is_callback:
            await message.edit_text(text, reply_markup=categories_keyboard(categories), parse_mode="HTML")
            await target.answer()
        else:
            await _send_prompt(message, state, text, reply_markup=categories_keyboard(categories), parse_mode="HTML")
        return
    await _ask_new_good_unit(target, state)


@router.callback_query(F.data == "scan:recognize", Receive.waiting_scan_photo)
async def on_scan_recognize(call: CallbackQuery, state: FSMContext, panel: PanelClient, settings: Settings) -> None:
    if not await panel.whoami(call.from_user.id):
        await state.clear()
        await call.message.edit_text("Аккаунт не подключён к учёту расходников.")
        await call.answer()
        return
    data = await state.get_data()
    photo_ids = list(data.get("scan_photo_ids", []))[:3]
    if not photo_ids:
        await call.answer("Сначала пришлите фото", show_alert=True)
        return
    await call.message.edit_text("Фото получены. Распознаю название и штрихкод…")
    reads = []
    try:
        async with asyncio.timeout(50):
            for file_id in photo_ids:
                telegram_file = await call.bot.get_file(file_id)
                file_obj = await call.bot.download_file(telegram_file.file_path)
                reads.append(await read_label_from_bytes(
                    api_key=settings.openai_api_key,
                    model=settings.openai_vision_model,
                    file_bytes=file_obj.read(),
                    **settings.vision_call_options(),
                ))
    except Exception:
        reads = []
    await _finish_label_read(call, state, panel, merge_label_reads(reads), call.from_user.id)


@router.callback_query(F.data == "scan:again", Receive.waiting_scan_confirm)
async def on_scan_again(call: CallbackQuery, state: FSMContext, panel: PanelClient) -> None:
    data = await state.get_data()
    good_id = data.get("scan_candidate_good_id")
    await state.update_data(
        good_id=good_id,
        title=data.get("candidate_title", ""),
        barcode=data.get("candidate_barcode", ""),
        category_id=data.get("category_id"),
        skip_category=True,
    )
    if not good_id:
        if not data.get("category_id"):
            categories = await panel.categories(call.from_user.id)
            await state.update_data(category_options=categories)
            await state.set_state(Receive.waiting_category)
            await call.message.edit_text(
                "К какой категории отнести этот новый материал?",
                reply_markup=categories_keyboard(categories),
            )
            await call.answer()
            return
        await _ask_new_good_unit(call, state)
        return
    unit = data.get("candidate_unit") or "шт"
    await state.update_data(unit=unit)
    await state.set_state(Receive.waiting_quantity)
    await call.message.edit_text(
        f"Добавляем ещё раз: <b>{data.get('candidate_title', 'товар')}</b>\n"
        f"{_quantity_question(unit)}",
        reply_markup=quantity_keyboard(),
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data == "scan:skip", Receive.waiting_scan_confirm)
async def on_scan_skip(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text("Ок, не добавлял. Выберите действие кнопкой ниже.")
    await call.answer()


@router.callback_query(F.data.startswith("scan:use:"), Receive.waiting_scan_confirm)
async def on_scan_use_existing(call: CallbackQuery, state: FSMContext) -> None:
    good_id = int(call.data.split(":", 2)[2])
    data = await state.get_data()

    await state.update_data(
        good_id=good_id,
        title=data.get("candidate_title", ""),
        barcode=data.get("candidate_barcode", ""),
        category_id=None,
        skip_category=True,
        prompt_message_id=call.message.message_id,
        unit=data.get("candidate_unit") or "шт",
    )
    await state.set_state(Receive.waiting_quantity)

    await call.message.edit_text(
        f"<b>{escape(data.get('candidate_title', 'позиции'))}</b>\n"
        f"{_quantity_question(data.get('candidate_unit') or 'шт')}",
        parse_mode="HTML", reply_markup=quantity_keyboard(),
    )
    await call.answer()


@router.callback_query(F.data == "scan:edit", Receive.waiting_scan_confirm)
async def on_scan_edit_title(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.set_state(Receive.waiting_title)
    await state.update_data(
        candidate_title=data.get("candidate_title", ""),
        candidate_barcode="",
        scan_editing=True,
        skip_category=True,
    )
    await call.message.edit_text("Введите другое название или уточните оттенок/объём. Штрихкод прежнего товара не будет скопирован.")
    await call.answer()


@router.callback_query(F.data.startswith("recv:"))
async def on_pick_existing(call: CallbackQuery, state: FSMContext) -> None:
    good_id = int(call.data.split(":", 1)[1])
    data = await state.get_data()
    position = next(
        (item for item in data.get("catalog_positions", []) if int(item.get("goodId", item.get("id", 0))) == good_id),
        {},
    )
    unit = position.get("unit") or "шт"
    await state.update_data(
        good_id=good_id,
        title=position.get("title") or _label_of(call, good_id),
        barcode="",
        category_id=None,
        skip_category=False,
        unit=unit,
    )
    await state.set_state(Receive.waiting_quantity)
    await call.message.edit_text(_quantity_question(unit, "Сколько привезли?"), reply_markup=quantity_keyboard())
    await call.answer()


@router.message(Receive.waiting_title, F.photo)
async def on_manual_label_photo(message: Message, state: FSMContext, settings: Settings) -> None:
    text = MANUAL_LABEL_PROMPT if not settings.vision_available else "Введите название расходника текстом. Для выхода — /cancel."
    await _send_prompt(message, state, text)


@router.message(Receive.waiting_title, F.text)
async def on_title_typed(message: Message, state: FSMContext, panel: PanelClient) -> None:
    title = (message.text or "").strip()
    if len(title) < 3:
        await message.answer("Слишком короткое название. Напишите подробнее.")
        return

    data = await state.get_data()
    skip_category = bool(data.get("skip_category"))

    payload = await panel.find_good(telegram_id=message.from_user.id, title=title, barcode="")
    exact = payload.get("exact")
    if exact:
        await state.update_data(
            candidate_title=title,
            candidate_barcode="",
            scan_editing=False,
        )
        await _start_confirm_with_existing(
            message,
            state,
            title,
            "",
            int(exact["goodId"]),
            [exact] + payload.get("matches", []),
        )
        return


    await state.update_data(
        good_id=None,
        title=title,
        barcode=data.get("candidate_barcode", ""),
        scan_editing=False,
    )
    if skip_category:
        if not data.get("category_id"):
            await state.set_state(Receive.waiting_category)
            categories = await panel.categories(message.from_user.id)
            await state.update_data(category_options=categories)
            await _send_prompt(message, state, "К какой категории отнести?", reply_markup=categories_keyboard(categories))
            return
        await _ask_new_good_unit(message, state)
        return

    categories = await panel.categories(message.from_user.id)
    if not categories:
        # Справочник недоступен — не держим мастера, категорию проставит панель.
        await _ask_new_good_unit(message, state)
        return

    await state.set_state(Receive.waiting_category)
    await state.update_data(category_options=categories)
    await _send_prompt(message, state, "К какой категории отнести?", reply_markup=categories_keyboard(categories))


@router.callback_query(F.data.startswith("cat:"), Receive.waiting_category)
async def on_category_picked(call: CallbackQuery, state: FSMContext) -> None:
    category_id = int(call.data.split(":", 1)[1])
    data = await state.get_data()
    selected = next(
        (item for item in data.get("category_options", []) if int(item.get("id", 0)) == category_id),
        None,
    )
    if data.get("category_options") and not selected:
        await call.answer("Категория больше недоступна", show_alert=True)
        return
    await state.update_data(
        category_id=category_id,
        category_title=(selected or {}).get("title", ""),
    )
    await _ask_new_good_unit(call, state)


@router.callback_query(F.data.startswith("unit:"), Receive.waiting_unit)
async def on_unit_picked(call: CallbackQuery, state: FSMContext) -> None:
    unit = "мл" if call.data.split(":", 1)[1] == "ml" else "шт"
    await state.update_data(unit=unit)
    await state.set_state(Receive.waiting_quantity)
    await call.message.edit_text(_quantity_question(unit), reply_markup=quantity_keyboard())
    await call.answer()


@router.message(Receive.waiting_quantity, F.text)
async def on_quantity_typed(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    unit = data.get("unit") or "шт"
    quantity = parse_quantity(message.text or "")
    if quantity is None or (unit == "шт" and not float(quantity).is_integer()):
        await message.answer(_quantity_question(unit, "Нужно положительное количество; штуки — целые."))
        return

    await state.update_data(package_size=quantity)
    await state.set_state(Receive.waiting_package_count)
    await _send_prompt(
        message, state, "Сколько упаковок или тюбиков добавляем? Введите число, например 3.",
        reply_markup=None,
    )


@router.callback_query(F.data.startswith("qty:"), Receive.waiting_quantity)
async def on_quantity_button(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    unit = data.get("unit") or "шт"
    value = call.data.split(":", 1)[1]
    if value == "manual":
        await call.message.edit_text(_quantity_question(unit))
        await call.answer()
        return
    quantity = parse_quantity(value)
    if quantity is None:
        await call.answer("Не понял количество", show_alert=True)
        return
    await state.update_data(package_size=quantity)
    await state.set_state(Receive.waiting_package_count)
    await call.message.edit_text(
        "Сколько упаковок или тюбиков добавляем? Введите число, например 3.",
        reply_markup=None,
    )
    await call.answer()


@router.message(Receive.waiting_package_count, F.text)
async def on_package_count(message: Message, state: FSMContext) -> None:
    count = parse_quantity(message.text or "")
    if count is None or not float(count).is_integer():
        await message.answer("Введите целое положительное количество упаковок, например 3.")
        return
    data = await state.get_data()
    await state.update_data(package_count=count, quantity=count * data["package_size"])
    await state.set_state(Receive.waiting_price)
    await _send_prompt(message, state, "Сколько стоит одна упаковка или тюбик? Например 450 ₽. Если неизвестно — 0 ₽.", reply_markup=price_keyboard())


def receive_preview_text(data: dict) -> str:
    size = data.get("package_size", 1)
    count = data.get("package_count", data.get("quantity", 1))
    price = data.get("price", 0)
    unit = escape(data.get("unit") or "шт")
    return (
        "<b>Подтвердите добавление на склад</b>"
        f"\n{escape(data.get('title') or 'Выбранная позиция')}"
        f"\nКатегория: {escape(data.get('category_title') or 'выбранная')}"
        f"\nФасовка: {format_amount(size)} {unit} × {format_amount(count)} уп."
        f"\nНа склад: {format_amount(size * count)} {unit}"
        f"\nЦена упаковки: {format_amount(price)} ₽"
        f"\nИтого: {format_amount(price * count)} ₽"
    )


@router.callback_query(F.data == "price:0", Receive.waiting_price)
async def on_zero_price(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(price=0.0, receive_request_id=str(uuid4()))
    await state.set_state(Receive.waiting_receive_confirm)
    await call.message.edit_text(receive_preview_text(await state.get_data()), parse_mode="HTML", reply_markup=receive_confirm_keyboard())
    await call.answer()


@router.message(Receive.waiting_price, F.text)
async def on_price_typed(message: Message, state: FSMContext, panel: PanelClient) -> None:
    price = parse_price(message.text or "")
    if price is None:
        await message.answer("Не понял цену. Напишите числом, например 450.")
        return
    await state.update_data(price=price, receive_request_id=str(uuid4()))
    await state.set_state(Receive.waiting_receive_confirm)
    await _send_prompt(message, state, receive_preview_text(await state.get_data()), parse_mode="HTML", reply_markup=receive_confirm_keyboard())


@router.callback_query(F.data == "receive:confirm", Receive.waiting_receive_confirm)
async def on_receive_confirm(call: CallbackQuery, state: FSMContext, panel: PanelClient) -> None:
    data = await state.get_data()
    if await state.get_state() != Receive.waiting_receive_confirm.state:
        await call.answer("Операция уже обрабатывается")
        return
    await state.set_state(Receive.receiving)
    try:
        result = await panel.receive(
            telegramId=call.from_user.id,
            goodId=data.get("good_id"),
            title=data.get("title", ""),
            packageSize=data.get("package_size", 1),
            packageCount=data.get("package_count", data["quantity"]),
            packagePrice=data.get("price", 0),
            idempotencyKey=data.get("receive_request_id"),
            categoryId=data.get("category_id"),
            barcode=data.get("barcode", ""),
            unit=data.get("unit") or "шт",
            **({"photoFileId": data["scan_photo_ids"][0]} if data.get("scan_photo_ids") else {}),
        )
    except ApiError as error:
        if error.code not in {"category_required", "leaf_category_required"}:
            await state.set_state(Receive.waiting_receive_confirm)
            raise
        categories = await panel.categories(call.from_user.id)
        await state.update_data(
            category_id=None,
            category_title="",
            category_options=categories,
        )
        await state.set_state(Receive.waiting_category)
        await call.message.edit_text(
            "Категория изменилась или недоступна. Выберите конечную категорию заново.",
            reply_markup=categories_keyboard(categories),
        )
        await call.answer()
        return
    except Exception:
        await state.set_state(Receive.waiting_receive_confirm)
        raise
    position = result["position"]
    created = " · новая позиция" if result.get("created") else ""
    await state.clear()
    await state.update_data(category_id=position.get("categoryId") or data.get("category_id"), category_title=data.get("category_title", ""), last_operation_id=result.get("operationId"), last_good_id=position.get("goodId"))
    await call.message.edit_text(
        "✅ Записал"
        f"\n<b>{escape(position['title'])}</b>{created}"
        f"\nПришло: {format_amount(result['added'])} {position.get('unit', 'шт')}"
        f"\nСумма: {format_amount(data.get('price', 0) * data.get('package_count', data.get('quantity', 1)))} ₽"
        f"\nТеперь на складе: {format_amount(position['stock'])} {position.get('unit', 'шт')}",
        parse_mode="HTML",
        reply_markup=receive_result_keyboard(bool(position.get("categoryId") or data.get("category_id")), bool(result.get("operationId"))),
    )
    await call.answer()


@router.callback_query(F.data == "receive:edit", Receive.waiting_receive_confirm)
async def on_receive_edit(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("good_id"):
        await _ask_new_good_unit(call, state)
        return
    unit = data.get("unit") or "шт"
    await state.set_state(Receive.waiting_quantity)
    await call.message.edit_text(_quantity_question(unit))
    await call.answer()


class MaterialFlow(StatesGroup):
    waiting_value = State()
    waiting_confirm = State()
    submitting = State()


def material_text(material: dict) -> str:
    unit = escape(material.get("unit", "шт"))
    stats = material.get("analytics") or {}
    text = (
        f"<b>{escape(material['title'])}</b>"
        f"\nКатегория: {escape(str(material.get('categoryTitle') or material.get('categoryId') or 'не указана'))}"
        f"\nШтрихкод: {escape(str(material.get('barcode') or 'не указан'))}"
        f"\nОстаток: {format_amount(material.get('stock', 0))} {unit}"
        f"\nФасовка: {format_amount(material.get('packageSize') or 1)} {unit}"
        f"\nМинимальный остаток: {format_amount(material.get('minStock') or 0)} {unit}"
        f"\nСписано через бота за 30 дней: {format_amount(stats.get('consumed30d') or 0)} {unit}"
        f"\nСтоимость списаний: {format_amount(stats.get('consumptionCost30d') or 0)} ₽"
        f"\nЗакупки через бота за 30 дней: {format_amount(stats.get('receiptCost30d') or 0)} ₽"
    )
    if stats.get("daysOfStock") is not None:
        text += f"\nЗапаса примерно на {format_amount(stats['daysOfStock'])} дней (оценка по операциям бота)"
    elif stats.get("forecastNote"):
        text += f"\n{escape(str(stats['forecastNote']))}"
    history = material.get("history") or []
    if history:
        names = {"receipt": "Приход", "receive": "Приход", "writeoff": "Списание", "count": "Пересчёт", "undo": "Отмена", "edit": "Изменение"}
        text += "\n\nПоследние операции:"
        for item in history[:5]:
            text += f"\n{escape(str(item.get('createdAt', ''))[:16])} · {names.get(item.get('kind'), 'Операция')} · {format_amount(item.get('amount') or 0)} {unit} · {escape(str(item.get('actorName') or item.get('actorId') or '—'))}"
    return text


@router.callback_query(F.data.startswith("settings:good:"))
async def on_settings_good_info(call: CallbackQuery, state: FSMContext, panel: PanelClient) -> None:
    material = await panel.material(call.from_user.id, int(call.data.rsplit(":", 1)[1]))
    await state.clear()
    await state.update_data(material=material)
    await call.message.edit_text(material_text(material), parse_mode="HTML", reply_markup=material_keyboard(material))
    await call.answer()


@router.callback_query(F.data.startswith("material:photo:"))
async def on_material_photo(call: CallbackQuery, state: FSMContext, panel: PanelClient) -> None:
    # Re-read under active organisation: never trust a file_id from callback/FSM.
    material = await panel.material(call.from_user.id, int(call.data.rsplit(":", 1)[1]))
    if not material.get("photoFileId"):
        await call.answer("Фото материала пока нет", show_alert=True)
        return
    await call.message.answer_photo(material["photoFileId"], caption=material["title"][:1000])
    await call.answer()


@router.callback_query(F.data.startswith("material:receive:"))
async def on_material_receive(call: CallbackQuery, state: FSMContext, panel: PanelClient) -> None:
    material = await panel.material(call.from_user.id, int(call.data.rsplit(":", 1)[1]))
    await state.clear()
    await state.update_data(good_id=material["goodId"], title=material["title"], unit=material["unit"], category_id=material.get("categoryId"))
    await state.set_state(Receive.waiting_quantity)
    await call.message.edit_text(_quantity_question(material["unit"]), reply_markup=quantity_keyboard(material.get("packageSize") or 1, material["unit"]))
    await call.answer()


@router.callback_query(F.data == "receive:next")
async def on_receive_next(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("category_id"):
        await call.answer("Откройте нужную категорию заново", show_alert=True)
        return
    category_id, title = data["category_id"], data.get("category_title", "")
    await state.clear()
    await state.update_data(category_id=category_id, category_title=title, skip_category=True)
    await state.set_state(Receive.waiting_scan_photo)
    await call.message.edit_text("Отправьте фото следующего материала для этой категории или введите название.")
    await call.answer()


@router.message(Receive.waiting_scan_photo, F.text)
async def on_next_title(message: Message, state: FSMContext, panel: PanelClient) -> None:
    await state.update_data(scan_editing=True, candidate_barcode="")
    await on_title_typed(message, state, panel)


@router.callback_query(F.data == "material:undo-last")
async def on_material_undo_last(call: CallbackQuery, state: FSMContext, panel: PanelClient) -> None:
    data = await state.get_data()
    if not data.get("last_good_id") or not data.get("last_operation_id"):
        await call.answer("Откройте актуальную карточку операции", show_alert=True)
        return
    material = await panel.material(call.from_user.id, data["last_good_id"])
    preview = await panel.material_preview(call.from_user.id, data["last_good_id"], "undo", operationId=data["last_operation_id"])
    await state.update_data(material=material, material_kind="undo", material_preview=preview)
    await state.set_state(MaterialFlow.waiting_confirm)
    await call.message.edit_text(f"Отменить последний приход?\nОстаток: {format_amount(preview['stockBefore'])} → {format_amount(preview['stockAfter'])} {escape(material['unit'])}.", parse_mode="HTML", reply_markup=material_confirm_keyboard())
    await call.answer()


@router.callback_query(F.data.startswith("material:edit:"))
async def on_material_edit(call: CallbackQuery, state: FSMContext, panel: PanelClient) -> None:
    material = await panel.material(call.from_user.id, int(call.data.rsplit(":", 1)[1]))
    await state.clear()
    await state.update_data(material=material)
    await call.message.edit_text("Что изменить?", reply_markup=material_edit_keyboard(material))
    await call.answer()


@router.callback_query(F.data.startswith("material:field:"))
async def on_material_field(call: CallbackQuery, state: FSMContext, panel: PanelClient) -> None:
    field = call.data.rsplit(":", 1)[1]
    data = await state.get_data()
    material = data.get("material") or {}
    if field not in material.get("editableFields", []):
        await call.answer("Поле недоступно", show_alert=True)
        return
    await state.update_data(material_kind="edit", material_field=field)
    if field == "categoryId":
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        categories = await panel.categories(call.from_user.id)
        await state.update_data(material_categories=categories)
        rows = [[InlineKeyboardButton(text=item["title"][:64], callback_data=f"material:category:{item['id']}")] for item in categories]
        rows.append([InlineKeyboardButton(text="Отмена", callback_data="cancel")])
        await call.message.edit_text("Выберите конечную категорию:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        await call.answer()
        return
    await state.set_state(MaterialFlow.waiting_value)
    await call.message.edit_text(f"Введите новое значение. Единица учёта: {escape(material.get('unit', 'шт'))}.", parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("material:category:"))
async def on_material_category(call: CallbackQuery, state: FSMContext, panel: PanelClient) -> None:
    data = await state.get_data()
    category_id = int(call.data.rsplit(":", 1)[1])
    category = next((c for c in data.get("material_categories", []) if int(c["id"]) == category_id), None)
    if not category or "categoryId" not in data.get("material", {}).get("editableFields", []):
        await call.answer("Категория недоступна", show_alert=True)
        return
    preview = await panel.material_preview(call.from_user.id, data["material"]["goodId"], "edit", patch={"categoryId": category_id})
    await state.update_data(material_preview=preview, material_kind="edit")
    await state.set_state(MaterialFlow.waiting_confirm)
    await call.message.edit_text(f"Перенести {escape(data['material']['title'])} в категорию {escape(category['title'])}?", parse_mode="HTML", reply_markup=material_confirm_keyboard())
    await call.answer()


@router.callback_query(F.data.startswith("material:writeoff:") | F.data.startswith("material:count:"))
async def on_material_amount(call: CallbackQuery, state: FSMContext, panel: PanelClient) -> None:
    _, kind, gid = call.data.split(":")
    material = await panel.material(call.from_user.id, int(gid))
    await state.clear()
    await state.update_data(material=material, material_kind=kind)
    await state.set_state(MaterialFlow.waiting_value)
    question = "Сколько списать" if kind == "writeoff" else "Какой остаток фактически"
    await call.message.edit_text(f"{question}, в {escape(material['unit'])}? Сейчас: {format_amount(material['stock'])}.", parse_mode="HTML")
    await call.answer()


@router.message(MaterialFlow.waiting_value, F.text)
async def on_material_value(message: Message, state: FSMContext, panel: PanelClient) -> None:
    data = await state.get_data()
    material, kind = data["material"], data["material_kind"]
    field = data.get("material_field")
    raw = (message.text or "").strip()
    if kind == "edit" and field in {"title", "barcode"}:
        if not raw:
            await message.answer("Введите непустое значение.")
            return
        payload = {"patch": {field: raw}}
    else:
        value = parse_price(raw) if kind == "count" or field == "minStock" else parse_quantity(raw)
        if value is None or (material["unit"] == "шт" and not float(value).is_integer()):
            await message.answer("Введите корректное количество. Штуки учитываются целыми числами.")
            return
        payload = {"patch": {field: value}} if kind == "edit" else {"amount" if kind == "writeoff" else "stock": value}
    preview = await panel.material_preview(message.from_user.id, material["goodId"], kind, **payload)
    await state.update_data(material_preview=preview)
    await state.set_state(MaterialFlow.waiting_confirm)
    field_names = {"title": "Название", "barcode": "Штрихкод", "packageSize": "Фасовка", "minStock": "Минимальный остаток"}
    description = "\n".join(f"{field_names.get(k, escape(str(k)))}: {escape(str(v))}" for k, v in (preview.get("patch") or {}).items()) if kind == "edit" else f"Остаток: {format_amount(preview['stockBefore'])} → {format_amount(preview['stockAfter'])} {escape(material['unit'])}"
    await _send_prompt(message, state, f"<b>Подтвердите изменение</b>\n{escape(material['title'])}\n{description}", parse_mode="HTML", reply_markup=material_confirm_keyboard())


@router.callback_query(F.data == "material:confirm", MaterialFlow.waiting_confirm)
async def on_material_confirm(call: CallbackQuery, state: FSMContext, panel: PanelClient) -> None:
    data = await state.get_data()
    if await state.get_state() != MaterialFlow.waiting_confirm.state:
        await call.answer("Уже обрабатывается")
        return
    await state.set_state(MaterialFlow.submitting)
    try:
        result = await panel.material_confirm(call.from_user.id, data["material"]["goodId"], data["material_kind"], data["material_preview"]["token"])
    except Exception:
        await state.set_state(MaterialFlow.waiting_confirm)
        raise
    await state.clear()
    await state.update_data(material=result["material"])
    await call.message.edit_text("✅ Сохранено\n" + material_text(result["material"]), parse_mode="HTML", reply_markup=material_keyboard(result["material"]))
    await call.answer()


@router.callback_query(F.data == "material:shopping")
async def on_shopping(call: CallbackQuery, state: FSMContext, panel: PanelClient) -> None:
    items = await panel.shopping(call.from_user.id)
    await state.clear()
    # Keep Telegram keyboard and message within limits; full list pages.
    await state.update_data(shopping_items=items, shopping_page=0)
    await _show_shopping(call, items, 0)


async def _show_shopping(call: CallbackQuery, items: list[dict], page: int) -> None:
    pages = max(1, (len(items) + 9) // 10)
    page = max(0, min(page, pages - 1))
    selected = items[page * 10:(page + 1) * 10]
    text = f"🛒 Список закупки · {len(items)} материалов · {page + 1}/{pages}"
    if not items:
        text += "\nМатериалов ниже минимального остатка нет. Минимум можно задать в карточке."
    for item in selected:
        text += f"\n{escape(item['title'])}: {format_amount(item['stock'])} / минимум {format_amount(item.get('minStock') or 0)} {escape(item['unit'])}"
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=shopping_keyboard(selected, page, pages))
    await call.answer()


@router.callback_query(F.data.startswith("material:shopping-page:"))
async def on_shopping_page(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    page = int(call.data.rsplit(":", 1)[1])
    await state.update_data(shopping_page=page)
    await _show_shopping(call, data.get("shopping_items", []), page)


@router.callback_query(F.data.startswith("material:order:"))
async def on_shopping_order(call: CallbackQuery, state: FSMContext, panel: PanelClient) -> None:
    _, _, gid, ordered = call.data.split(":")
    await panel.material_order(call.from_user.id, int(gid), ordered == "1")
    items = await panel.shopping(call.from_user.id)
    data = await state.get_data()
    await state.update_data(shopping_items=items)
    await _show_shopping(call, items, data.get("shopping_page", 0))


class StockCount(StatesGroup):
    waiting_value = State()
    waiting_confirm = State()
    submitting = State()


def _count_question(data: dict) -> str:
    index = len(data.get("count_values", []))
    item = data["count_goods"][index]
    return f"{index + 1}/{len(data['count_goods'])} · {escape(item['title'])}\nСейчас в базе: {format_amount(item.get('stock', 0))} {escape(item.get('unit', 'шт'))}.\nВведите фактический остаток, включая 0. Для отмены — /cancel."


@router.callback_query(F.data.startswith("stock-count:"))
async def on_stock_count_start(call: CallbackQuery, state: FSMContext, panel: PanelClient) -> None:
    category_id = int(call.data.split(":")[1])
    page = (await panel.catalog(call.from_user.id, category_id))["page"]
    goods = page.get("goods", [])
    if not goods:
        await call.answer("В категории нет материалов", show_alert=True)
        return
    await state.clear()
    await state.update_data(count_category_id=category_id, count_goods=goods, count_values=[])
    await state.set_state(StockCount.waiting_value)
    await call.message.edit_text(_count_question(await state.get_data()), parse_mode="HTML")
    await call.answer()


@router.message(StockCount.waiting_value, F.text)
async def on_stock_count_value(message: Message, state: FSMContext, panel: PanelClient) -> None:
    data = await state.get_data()
    values = list(data["count_values"])
    item = data["count_goods"][len(values)]
    value = parse_price(message.text or "")
    if value is None or (item.get("unit", "шт") == "шт" and not float(value).is_integer()):
        await message.answer("Введите неотрицательный остаток. Штуки — целым числом.")
        return
    values.append({"goodId": item.get("goodId", item.get("id")), "stock": value})
    await state.update_data(count_values=values)
    if len(values) < len(data["count_goods"]):
        await _send_prompt(message, state, _count_question(await state.get_data()), parse_mode="HTML")
        return
    preview = await panel.inventory_count_preview(message.from_user.id, data["count_category_id"], values)
    await state.update_data(count_preview=preview)
    await state.set_state(StockCount.waiting_confirm)
    changed = [i for i in preview["items"] if i.get("delta", 0) != 0]
    # Each chunk remains readable and below Telegram text limits.
    summary = f"<b>Сверка завершена</b>\nПроверено: {len(values)}. Расхождений: {preview['changedCount']}."
    for item in changed:
        line = f"\n{escape(item['title'])}: {format_amount(item['stockBefore'])} → {format_amount(item['stockAfter'])} {escape(item['unit'])}"
        if len(summary) + len(line) > 3400:
            await message.answer(summary, parse_mode="HTML")
            summary = "<b>Продолжение сверки</b>"
        summary += line
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить все корректировки", callback_data="stock-count-confirm")],
        [InlineKeyboardButton(text="Отмена", callback_data="cancel")],
    ])
    await _send_prompt(message, state, summary, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data == "stock-count-confirm", StockCount.waiting_confirm)
async def on_stock_count_confirm(call: CallbackQuery, state: FSMContext, panel: PanelClient) -> None:
    data = await state.get_data()
    if await state.get_state() != StockCount.waiting_confirm.state:
        await call.answer("Уже обрабатывается")
        return
    await state.set_state(StockCount.submitting)
    try:
        result = await panel.inventory_count_confirm(call.from_user.id, data["count_preview"]["token"])
    except Exception:
        await state.set_state(StockCount.waiting_confirm)
        raise
    if result.get("status") == "partial":
        errors = {item.get("errorCode") for item in result.get("items", []) if item.get("errorCode")}
        blocked = bool(errors & {"uncertain_result", "stale_stock", "stale_catalog", "expired"})
        if blocked:
            await state.clear()
            await call.message.edit_text("Часть корректировок выполнена. Остальные требуют проверки: остаток изменился или результат записи неизвестен. Проверьте карточки материалов и начните новую сверку; повторная запись автоматически не выполняется.")
        else:
            await state.set_state(StockCount.waiting_confirm)
            await call.message.edit_text("Часть корректировок выполнена. Повторите то же подтверждение для оставшихся строк.", reply_markup=call.message.reply_markup)
    else:
        await state.clear()
        await call.message.edit_text("✅ Остатки категории актуализированы.")
    await call.answer()


# --- чек по фото ------------------------------------------------------------

@router.message(F.text == BTN_RECEIPT)
async def on_receipt_start(message: Message, state: FSMContext, panel: PanelClient, settings: Settings) -> None:
    if not await require_master(message, panel, state):
        return

    if not settings.vision_available:
        await _send_prompt(message, state, MANUAL_RECEIPT_PROMPT, parse_mode="HTML", clear_state=True)
        await state.set_state(Receipt.waiting_manual_lines)
        return

    await _send_prompt(
        message,
        state,
        "Отправьте одно чёткое фото чека целиком. Оригинал фото не сохраняется.",
        clear_state=True,
    )
    await state.set_state(Receipt.waiting_photo)


@router.message(Receipt.waiting_photo, F.photo)
async def on_receipt_photo(
    message: Message,
    state: FSMContext,
    panel: PanelClient,
    settings: Settings,
) -> None:
    if not await require_master(message, panel, state):
        return

    if not settings.vision_available:
        await _send_prompt(message, state, MANUAL_RECEIPT_PROMPT, parse_mode="HTML", clear_state=True)
        await state.set_state(Receipt.waiting_manual_lines)
        return

    await state.update_data(receipt_photo_id=message.photo[-1].file_id)
    try:
        telegram_file = await message.bot.get_file(message.photo[-1].file_id)
        file_obj = await message.bot.download_file(telegram_file.file_path)
        file_bytes = file_obj.read()
    except Exception:
        file_bytes = b""

    await _send_prompt(message, state, "Фото получено. Распознаю позиции и цены…")
    try:
        async with asyncio.timeout(50):
            receipt = await read_receipt_from_bytes(
                api_key=settings.openai_api_key,
                model=settings.openai_vision_model,
                file_bytes=file_bytes,
                **settings.vision_call_options(),
            )
    except Exception:
        receipt = ReceiptReadResult(issues=("распознавание не завершилось",))
    await _finish_receipt_result(message, state, panel, receipt)


async def _finish_receipt_result(
    message: Message,
    state: FSMContext,
    panel: PanelClient,
    receipt: ReceiptReadResult,
) -> None:
    receipt = validate_receipt(receipt)
    if not receipt.lines:
        await state.update_data(receipt_ocr_issues=list(receipt.issues))
        await state.set_state(Receipt.waiting_manual_lines)
        await _send_prompt(
            message,
            state,
            "Не удалось уверенно разобрать товарные строки. Введите их вручную, "
            "каждую с новой строки:\n"
            "<code>Название | количество | цена за единицу | сумма</code>\n"
            "Неизвестное значение обозначьте знаком <code>?</code>.",
            parse_mode="HTML",
            reply_markup=recognition_retry_keyboard("receipt"),
        )
        return

    matches = await _receipt_matches(panel, receipt, message.from_user.id)
    await state.update_data(
        receipt=receipt.to_dict(),
        receipt_sku_matches=matches,
        receipt_decisions=[{} for _ in receipt.lines],
    )
    await state.set_state(Receipt.waiting_preview)
    await _send_prompt(
        message,
        state,
        _receipt_preview(receipt, matches),
        reply_markup=receipt_preview_keyboard(),
        parse_mode="HTML",
    )


@router.message(StateFilter(None), F.photo)
async def on_unclassified_photo(
    message: Message,
    state: FSMContext,
    panel: PanelClient,
    settings: Settings,
) -> None:
    if not await require_master(message, panel, state):
        return
    file_id = message.photo[-1].file_id
    if not settings.vision_available or settings.vision_provider != "hermes-local":
        await state.set_state(PhotoIntake.waiting_kind)
        await state.update_data(unclassified_photo_id=file_id)
        await _send_prompt(
            message, state, "Что изображено на фото?",
            reply_markup=photo_kind_keyboard(),
        )
        return
    await _send_prompt(message, state, "Фото получено. Определяю: товар или чек…")
    try:
        telegram_file = await message.bot.get_file(file_id)
        file_obj = await message.bot.download_file(telegram_file.file_path)
        async with asyncio.timeout(50):
            result = await read_auto_from_bytes(
                file_bytes=file_obj.read(),
                local_url=settings.inventory_vision_url,
                local_key=settings.inventory_vision_key,
            )
    except Exception:
        result = None

    if result and result.kind == "label":
        raw = result.label or {}
        read = LabelReadResult(
            str(raw.get("title") or ""),
            str(raw.get("barcode") or ""),
            "",
            str(raw.get("score") or "low"),
        )
        await _finish_label_read(message, state, panel, read, message.from_user.id)
        return
    if result and result.kind == "receipt":
        await _finish_receipt_result(
            message, state, panel, scanned_receipt(result.receipt or {})
        )
        return

    await state.set_state(PhotoIntake.waiting_kind)
    await state.update_data(unclassified_photo_id=file_id)
    await _send_prompt(
        message, state,
        "Не удалось уверенно определить тип фото. Что на нём?",
        reply_markup=photo_kind_keyboard(),
    )


@router.callback_query(F.data == "photo:kind:label", PhotoIntake.waiting_kind)
async def on_photo_kind_label(call: CallbackQuery, state: FSMContext, panel: PanelClient, settings: Settings) -> None:
    data = await state.get_data()
    file_id = data.get("unclassified_photo_id")
    await state.update_data(scan_photo_ids=[file_id] if file_id else [])
    await state.set_state(Receive.waiting_scan_photo)
    await on_scan_recognize(call, state, panel, settings)


@router.callback_query(F.data == "photo:kind:receipt", PhotoIntake.waiting_kind)
async def on_photo_kind_receipt(call: CallbackQuery, state: FSMContext, panel: PanelClient, settings: Settings) -> None:
    data = await state.get_data()
    file_id = data.get("unclassified_photo_id")
    if not file_id:
        await call.answer("Фото уже недоступно", show_alert=True)
        return
    await call.message.edit_text("Распознаю позиции и цены…")
    try:
        telegram_file = await call.bot.get_file(file_id)
        file_obj = await call.bot.download_file(telegram_file.file_path)
        async with asyncio.timeout(50):
            receipt = await read_receipt_from_bytes(
                api_key=settings.openai_api_key,
                model=settings.openai_vision_model,
                file_bytes=file_obj.read(),
                **settings.vision_call_options(),
            )
    except Exception:
        receipt = ReceiptReadResult(issues=("распознавание не завершилось",))
    await state.update_data(receipt_photo_id=file_id)
    await _finish_receipt_result(call.message, state, panel, receipt)
    await call.answer()


@router.callback_query(F.data == "label:manual")
async def on_label_manual(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Receive.waiting_title)
    await state.update_data(scan_editing=True, skip_category=False)
    await call.message.edit_text("Введите название расходника текстом. Для выхода — /cancel.")
    await call.answer()


@router.callback_query(F.data == "receipt:manual")
async def on_receipt_manual(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Receipt.waiting_manual_lines)
    await call.message.edit_text(MANUAL_RECEIPT_PROMPT, parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "scan:retry")
async def on_scan_retry(call: CallbackQuery, state: FSMContext, panel: PanelClient, settings: Settings) -> None:
    data = await state.get_data()
    if not data.get("scan_photo_ids"):
        await call.answer("Пришлите фото заново", show_alert=True)
        return
    await state.set_state(Receive.waiting_scan_photo)
    await on_scan_recognize(call, state, panel, settings)


@router.callback_query(F.data == "receipt:retry")
async def on_receipt_retry(call: CallbackQuery, state: FSMContext, panel: PanelClient, settings: Settings) -> None:
    data = await state.get_data()
    file_id = data.get("receipt_photo_id")
    if not file_id:
        await call.answer("Пришлите фото заново", show_alert=True)
        return
    await state.update_data(unclassified_photo_id=file_id)
    await state.set_state(PhotoIntake.waiting_kind)
    await on_photo_kind_receipt(call, state, panel, settings)


@router.message(Receipt.waiting_manual_lines, F.photo)
async def on_manual_receipt_photo(message: Message, state: FSMContext, settings: Settings) -> None:
    text = MANUAL_RECEIPT_PROMPT if not settings.vision_available else (
        "Введите строки чека текстом: <code>Название | количество | цена за единицу | сумма</code>. Для выхода — /cancel."
    )
    await _send_prompt(message, state, text, parse_mode="HTML")


@router.message(Receipt.waiting_manual_lines, F.text)
async def on_receipt_manual_lines(
    message: Message,
    state: FSMContext,
    panel: PanelClient,
) -> None:
    try:
        lines = tuple(
            parse_manual_line(row)
            for row in (message.text or "").splitlines()
            if row.strip()
        )
        if not lines:
            raise ValueError("Добавьте хотя бы одну строку")
    except ValueError as error:
        await message.answer(f"Не получилось разобрать строки: {escape(str(error))}")
        return

    receipt = validate_receipt(ReceiptReadResult(lines=lines))
    matches = await _receipt_matches(panel, receipt, message.from_user.id)
    await state.update_data(
        receipt=receipt.to_dict(),
        receipt_sku_matches=matches,
        receipt_decisions=[{} for _ in receipt.lines],
    )
    await state.set_state(Receipt.waiting_preview)
    await _send_prompt(
        message,
        state,
        _receipt_preview(receipt, matches),
        reply_markup=receipt_preview_keyboard(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "receipt:edit", Receipt.waiting_preview)
async def on_receipt_edit(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    receipt = ReceiptReadResult.from_dict(data.get("receipt", {}))
    await call.message.edit_text(
        "Какую строку исправить?",
        reply_markup=receipt_lines_keyboard([line.to_dict() for line in receipt.lines]),
    )
    await call.answer()


@router.callback_query(F.data.startswith("receipt:line:"), Receipt.waiting_preview)
async def on_receipt_pick_line(call: CallbackQuery, state: FSMContext) -> None:
    index = int(call.data.rsplit(":", 1)[1])
    data = await state.get_data()
    receipt = ReceiptReadResult.from_dict(data.get("receipt", {}))
    if index < 0 or index >= len(receipt.lines):
        await call.answer()
        return
    data = await state.get_data()
    matches = list(data.get("receipt_sku_matches", []))
    decisions = list(data.get("receipt_decisions", []))
    while len(decisions) < len(receipt.lines):
        decisions.append({})
    line = classify_receipt_line(receipt.lines[index])
    await call.message.edit_text(
        f"<b>{escape(line.title)}</b>\n"
        f"{_receipt_number(line.quantity)} × {_receipt_number(line.unit_price)} ₽ "
        f"= {_receipt_number(line.line_total)} ₽"
        + (f"\n⚠️ {escape(line.review_reason)}" if line.requires_review else ""),
        parse_mode="HTML",
        reply_markup=receipt_item_keyboard(
            index,
            matched=bool(index < len(matches) and matches[index]),
            skipped=bool(decisions[index].get("skip")),
            needs_review=line.requires_review and decisions[index].get("reviewDecision") != "include",
        ),
    )
    await call.answer()


@router.callback_query(F.data.startswith("receipt:item:category:"), Receipt.waiting_preview)
async def on_receipt_category_menu(call: CallbackQuery, state: FSMContext, panel: PanelClient) -> None:
    index = int(call.data.rsplit(":", 1)[1])
    categories = await panel.categories(call.from_user.id)
    await call.message.edit_text(
        "Выберите категорию новой позиции:",
        reply_markup=receipt_category_keyboard(index, categories),
    )
    await call.answer()


@router.callback_query(F.data.startswith("receipt:item:cat:"), Receipt.waiting_preview)
async def on_receipt_category_picked(call: CallbackQuery, state: FSMContext) -> None:
    _, _, _, raw_index, raw_category = call.data.split(":", 4)
    index, category_id = int(raw_index), int(raw_category)
    data = await state.get_data()
    receipt = ReceiptReadResult.from_dict(data.get("receipt", {}))
    decisions = list(data.get("receipt_decisions", []))
    while len(decisions) < len(receipt.lines):
        decisions.append({})
    decisions[index] = {**decisions[index], "categoryId": category_id, "skip": False}
    if decisions[index].get("reviewDecision") == "exclude":
        decisions[index].pop("reviewDecision", None)
    await state.update_data(receipt_decisions=decisions)
    await call.message.edit_text(
        _receipt_preview(receipt, list(data.get("receipt_sku_matches", [])), decisions),
        reply_markup=receipt_preview_keyboard(), parse_mode="HTML",
    )
    await call.answer("Категория выбрана")


@router.callback_query(F.data.startswith("receipt:item:skip:"), Receipt.waiting_preview)
@router.callback_query(F.data.startswith("receipt:item:include:"), Receipt.waiting_preview)
async def on_receipt_toggle_line(call: CallbackQuery, state: FSMContext) -> None:
    parts = call.data.split(":")
    skip, index = parts[2] == "skip", int(parts[3])
    data = await state.get_data()
    receipt = ReceiptReadResult.from_dict(data.get("receipt", {}))
    decisions = list(data.get("receipt_decisions", []))
    while len(decisions) < len(receipt.lines):
        decisions.append({})
    if not 0 <= index < len(receipt.lines):
        await call.answer("Строка уже недоступна", show_alert=True)
        return
    decisions[index] = {**decisions[index], "skip": skip, "reviewDecision": "exclude" if skip else "include"}
    await state.update_data(receipt_decisions=decisions)
    await call.message.edit_text(
        _receipt_preview(receipt, list(data.get("receipt_sku_matches", [])), decisions),
        reply_markup=receipt_preview_keyboard(), parse_mode="HTML",
    )
    await call.answer("Строка исключена" if skip else "Строка возвращена")


@router.callback_query(F.data.startswith("receipt:item:edit:"), Receipt.waiting_preview)
async def on_receipt_edit_item(call: CallbackQuery, state: FSMContext) -> None:
    index = int(call.data.rsplit(":", 1)[1])
    data = await state.get_data()
    receipt = ReceiptReadResult.from_dict(data.get("receipt", {}))
    line = receipt.lines[index]
    await state.update_data(receipt_edit_index=index)
    await state.set_state(Receipt.waiting_edit_line)
    await call.message.edit_text(
        "Пришлите исправленную строку в формате:\n"
        "<code>Название | количество | цена за единицу | сумма</code>\n\n"
        f"Сейчас: <code>{escape(line.title)} | {_receipt_number(line.quantity)} | "
        f"{_receipt_number(line.unit_price)} | {_receipt_number(line.line_total)}</code>",
        parse_mode="HTML",
    )
    await call.answer()


@router.message(Receipt.waiting_edit_line, F.text)
async def on_receipt_edit_line_text(
    message: Message,
    state: FSMContext,
    panel: PanelClient,
) -> None:
    try:
        edited = parse_manual_line(message.text or "")
    except ValueError as error:
        await message.answer(f"Не получилось разобрать строку: {escape(str(error))}")
        return

    data = await state.get_data()
    receipt = ReceiptReadResult.from_dict(data.get("receipt", {}))
    index = int(data.get("receipt_edit_index", -1))
    if index < 0 or index >= len(receipt.lines):
        await state.clear()
        await _send_prompt(message, state, "Строка уже недоступна. Начните с фото чека заново.", reply_markup=main_menu())
        return

    lines = list(receipt.lines)
    lines[index] = edited
    receipt = validate_receipt(
        ReceiptReadResult(
            lines=tuple(lines),
            total=receipt.total,
            purchased_at=receipt.purchased_at,
            merchant=receipt.merchant,
        )
    )
    matches = list(data.get("receipt_sku_matches", []))
    while len(matches) < len(lines):
        matches.append(None)
    found = await panel.find_good(
        telegram_id=message.from_user.id, title=edited.title, barcode=""
    )
    matches[index] = found.get("exact")
    decisions = list(data.get("receipt_decisions", []))
    while len(decisions) < len(lines):
        decisions.append({})
    # A decision about a previous title cannot approve a different edited item.
    decisions[index] = {}
    await state.update_data(receipt=receipt.to_dict(), receipt_sku_matches=matches, receipt_decisions=decisions)
    await state.set_state(Receipt.waiting_preview)
    await _send_prompt(
        message,
        state,
        _receipt_preview(receipt, matches),
        reply_markup=receipt_preview_keyboard(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "receipt:back", Receipt.waiting_preview)
async def on_receipt_back(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    receipt = ReceiptReadResult.from_dict(data.get("receipt", {}))
    matches = list(data.get("receipt_sku_matches", []))
    decisions = list(data.get("receipt_decisions", []))
    await call.message.edit_text(
        _receipt_preview(receipt, matches, decisions),
        reply_markup=receipt_preview_keyboard(),
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data == "receipt:confirm", Receipt.waiting_preview)
async def on_receipt_confirm(
    call: CallbackQuery,
    state: FSMContext,
    panel: PanelClient,
) -> None:
    data = await state.get_data()
    receipt = ReceiptReadResult.from_dict(data.get("receipt", {}))
    matches = list(data.get("receipt_sku_matches", []))
    decisions = list(data.get("receipt_decisions", []))
    payload = build_receipt_payload(receipt, sku_matches=matches, decisions=decisions)
    needs_review = [item["sourceIndex"] + 1 for item in payload["items"] if item.get("requiresReview") and item.get("reviewDecision") != "include"]
    if needs_review:
        await call.answer("Решите, какие строки добавить на склад: " + ", ".join(map(str, needs_review)), show_alert=True)
        return
    if not payload["items"]:
        await call.answer("Все строки исключены — на склад ничего не добавлено", show_alert=True)
        return
    unresolved = [
        index + 1
        for index, item in enumerate(payload["items"])
        if not item.get("goodId") and not item.get("categoryId")
    ]
    if unresolved:
        await call.answer(
            "Выберите категорию для новых строк: " + ", ".join(map(str, unresolved)),
            show_alert=True,
        )
        return
    preview = await panel.receipt_preview(
        telegram_id=call.from_user.id,
        items=payload["items"],
        merchant=payload.get("merchant") or "",
        purchased_at=payload.get("purchasedAt") or "",
        total=payload.get("total"),
        **({"excluded_items": payload["excludedItems"]} if payload["excludedItems"] else {}),
    )
    await state.update_data(
        receipt_write_token=preview.get("token"),
        receipt_write_fingerprint=preview.get("fingerprint"),
        receipt_write_lines=preview.get("lines", []),
    )
    await state.set_state(Receipt.waiting_write_confirm)
    await call.message.edit_text(
        _receipt_write_prompt(preview.get("lines", []), preview.get("excludedLines", []), preview.get("excludedTotal", 0)),
        reply_markup=receipt_write_keyboard(),
        parse_mode="HTML",
    )
    await call.answer()


async def _apply_receipt_write(call: CallbackQuery, state: FSMContext, panel: PanelClient) -> None:
    data = await state.get_data()
    result = await panel.receipt_confirm(
        telegram_id=call.from_user.id,
        token=data.get("receipt_write_token"),
        fingerprint=data.get("receipt_write_fingerprint"),
        confirmed=True,
    )
    partial = result.get("status") == "partial"
    await state.set_state(Receipt.waiting_write_confirm if partial else None)
    await call.message.edit_text(
        _receipt_result_text(result),
        reply_markup=receipt_result_keyboard(partial=partial),
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data == "receipt:write", Receipt.waiting_write_confirm)
async def on_receipt_write(call: CallbackQuery, state: FSMContext, panel: PanelClient) -> None:
    await _apply_receipt_write(call, state, panel)


@router.callback_query(F.data == "receipt:resume", Receipt.waiting_write_confirm)
async def on_receipt_resume(call: CallbackQuery, state: FSMContext, panel: PanelClient) -> None:
    await _apply_receipt_write(call, state, panel)


@router.callback_query(F.data == "receipt:done")
async def on_receipt_done(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text("✅ Приход записан. Выберите действие кнопкой ниже.")
    await call.answer()



# --- списание ---------------------------------------------------------------

@router.message(F.text == BTN_DEPLETE)
async def on_deplete_start(message: Message, state: FSMContext, panel: PanelClient) -> None:
    if not await require_master(message, panel, state):
        return

    await state.clear()
    positions = await panel.open_positions(message.from_user.id)
    if not positions:
        await _send_prompt(
            message,
            state,
            "Списывать нечего — на складе пусто. Сначала пополните.",
            reply_markup=main_menu(),
        )
        return

    await _send_prompt(
        message,
        state,
        "Что закончилось?",
        reply_markup=positions_keyboard(positions, "dep", with_stock=True),
    )


@router.callback_query(F.data.startswith("dep:"))
async def on_pick_depleted(call: CallbackQuery, state: FSMContext) -> None:
    good_id = int(call.data.split(":", 1)[1])
    await state.update_data(good_id=good_id, title=_label_of(call, good_id))
    await state.set_state(Deplete.waiting_confirm)
    await call.message.edit_text(
        "Списать весь остаток этой позиции?", reply_markup=confirm_keyboard("dep")
    )
    await call.answer()


@router.callback_query(F.data == "yes:dep", Deplete.waiting_confirm)
async def on_confirm_deplete(call: CallbackQuery, state: FSMContext, panel: PanelClient) -> None:
    data = await state.get_data()
    await state.clear()

    result = await panel.deplete(call.from_user.id, data["good_id"])
    position = result["position"]
    await call.message.edit_text(
        "✅ Списал"
        f"\n<b>{position['title']}</b>"
        f"\nУшло: {format_amount(result['writtenOff'])} {position.get('unit', 'шт')}",
        parse_mode="HTML",
    )
    await call.answer()


# --- общее ------------------------------------------------------------------

@router.callback_query(F.data == "cancel")
async def on_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text("Отменил. Выберите действие кнопкой ниже.")
    await call.answer()


@router.message()
async def on_anything_else(message: Message, state: FSMContext, panel: PanelClient) -> None:
    """Мастер написал что-то мимо сценария — возвращаем к кнопкам, а не молчим."""
    if await require_master(message, panel, state):
        await _send_prompt(message, state, "Выберите действие кнопкой ниже.", reply_markup=main_menu())


@router.errors()
async def on_error(event, exception: Exception) -> bool:
    """Ошибки показываем словами. Мастер не должен видеть трассировку."""
    update = event.update
    target = getattr(update, "message", None)
    if target is None and getattr(update, "callback_query", None):
        target = update.callback_query.message
    if target:
        if isinstance(exception, ApiError):
            text = exception.message
        else:
            # Не пишем сообщение исключения: оно может содержать ответ внешней
            # системы. Типа достаточно для серверной диагностики.
            log.error("Unhandled bot update error: %s", type(exception).__name__)
            text = "Не получилось подтвердить результат. Проверьте карточку материала перед повтором; повторное подтверждение той же операции безопасно."
        await target.answer(text, reply_markup=main_menu())
    return True


def _label_of(call: CallbackQuery, good_id: int) -> str:
    """Название берём с нажатой кнопки: лишний запрос к панели не нужен."""
    if not call.message or not call.message.reply_markup:
        return "позиция"

    for row in call.message.reply_markup.inline_keyboard:
        for button in row:
            if button.callback_data and button.callback_data.endswith(f":{good_id}"):
                return button.text.split(" · ")[0]
    return "позиция"
