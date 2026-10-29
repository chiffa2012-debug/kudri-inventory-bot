"""Campaign workflow. Only service metadata changes; SKU counts remain preview."""
from __future__ import annotations

import math
import re
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .api_client import ApiError
from .handlers import _send_prompt, require_master
from .keyboards import BTN_INVENTORY, main_menu

router = Router(name="inventory_cycle")


class Inventory(StatesGroup):
    viewing = State()
    waiting_counts = State()
    waiting_confirmation = State()
    waiting_sku = State()


def cycle_keyboard(category=None, confirmation=False):
    buttons = []
    if confirmation:
        buttons.append(("✅ Подтвердить завершение", "confirm"))
    elif category:
        buttons.append(("▶ Начать" if category.get("status") == "pending" else "▶ Продолжить",
                        "start" if category.get("status") == "pending" else "continue"))
        buttons.extend([("🔎 Сверить SKU", "preview"), ("↻ Повторная сверка", "recheck"),
                        ("✅ Завершить категорию", "complete")])
    buttons.extend([("↻ Обновить", "refresh"), ("← В меню", "menu")])
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=text, callback_data="cycle:" + action)
    ] for text, action in buttons])


def cycle_text(snapshot):
    campaign = snapshot.get("campaign") or {}
    progress = snapshot.get("progress") or {}
    category = snapshot.get("currentCategory")
    text = f"<b>Инвентаризация</b>\nПройдено категорий: {progress.get('completed', 0)}/{progress.get('total', 0)}"
    if category:
        statuses = {"pending": "ожидает", "in_progress": "в работе", "needs_recheck": "требуется повторная сверка", "completed": "завершена"}
        text += f"\nКатегория: <b>{escape(str(category['title']))}</b>\nСтатус: {statuses.get(category.get('status'), 'ожидает')}"
        text += f"\nСрок: {escape(str(category.get('dueAt') or 'не назначен'))}"
        text += f"\nПроверено SKU: {category.get('checkedSkuCount', 0)}; расхождений: {category.get('discrepancies', 0)}"
    elif campaign.get("status") == "empty":
        text += "\nКаталог пуст. Категории появятся после добавления расходников."
    elif campaign.get("status") == "completed":
        text += f"\nЦикл завершён. Следующий: {escape(str(campaign.get('nextCycleDueAt') or 'не назначен'))}"
    else:
        text += "\nНа сегодня все категории пройдены. Следующая задача будет по расписанию."
    text += "\n\nСверка сохраняет прогресс. Корректировка остатков отключена."
    return text


async def remember_snapshot(state, snapshot):
    data = await state.get_data()
    prompt_id = data.get("prompt_message_id")
    await state.clear()
    await state.update_data(prompt_message_id=prompt_id,
                            cycle_campaign_id=(snapshot.get("campaign") or {}).get("id"),
                            cycle_revision=(snapshot.get("campaign") or {}).get("revision", 0),
                            cycle_category=snapshot.get("currentCategory"))
    await state.set_state(Inventory.viewing)


async def edit_prompt(call, state, text, keyboard):
    try:
        await call.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise
    await state.update_data(prompt_message_id=call.message.message_id)


@router.message(F.text == BTN_INVENTORY)
async def on_inventory(message, state, panel):
    try:
        if not await require_master(message, panel, state):
            return
        snapshot = await panel.inventory_cycle(message.from_user.id)
        await remember_snapshot(state, snapshot)
        await _send_prompt(message, state, cycle_text(snapshot),
                           reply_markup=cycle_keyboard(snapshot.get("currentCategory")), parse_mode="HTML")
    except ApiError as exc:
        await _send_prompt(message, state, exc.message, reply_markup=main_menu(), clear_state=True)


@router.callback_query(F.data.startswith("cycle:"))
async def on_cycle_callback(call, state, panel):
    data = await state.get_data()
    if not call.message or data.get("prompt_message_id") != call.message.message_id:
        await call.answer("Это меню устарело. Откройте инвентаризацию заново.")
        return
    await call.answer()
    action = call.data.split(":", 1)[1]
    if action == "menu":
        # A reply keyboard requires a new message. Remove the previous bot prompt.
        try:
            await call.message.bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        await state.clear()
        await _send_prompt(call.message, state, "Выберите действие.", reply_markup=main_menu())
        return
    category = data.get("cycle_category")
    campaign_id = data.get("cycle_campaign_id")
    try:
        if action == "refresh":
            snapshot = await panel.inventory_cycle(call.from_user.id)
        elif not category or not campaign_id:
            await edit_prompt(call, state, "Категория уже недоступна. Обновите расписание.", cycle_keyboard())
            return
        elif action in {"start", "continue", "recheck"}:
            snapshot = await panel.cycle_action(call.from_user.id, campaign_id, category["id"], action,
                                              revision=data.get("cycle_revision", 0))
        elif action == "complete":
            await state.update_data(cycle_counts=None)
            await state.set_state(Inventory.waiting_counts)
            await edit_prompt(call, state,
                              "Введите: <b>проверено SKU | расхождений</b>\nНапример: 12 | 2. Только целые неотрицательные числа. Остатки не изменятся.", cycle_keyboard())
            return
        elif action == "confirm":
            counts = data.get("cycle_counts")
            if not counts:
                await edit_prompt(call, state, "Сначала укажите результаты сверки.", cycle_keyboard(category))
                return
            snapshot = await panel.cycle_action(call.from_user.id, campaign_id, category["id"], "complete",
                                              **counts, confirmed=True, revision=data.get("cycle_revision", 0))
        elif action == "preview":
            await state.set_state(Inventory.waiting_sku)
            await edit_prompt(call, state, "Введите <b>ID товара | фактическое количество</b>, например: 42 | 3. Это только предпросмотр, остатки не изменятся.", cycle_keyboard())
            return
        else:
            return
        await remember_snapshot(state, snapshot)
        await edit_prompt(call, state, cycle_text(snapshot), cycle_keyboard(snapshot.get("currentCategory")))
    except ApiError as exc:
        await edit_prompt(call, state, escape(exc.message), cycle_keyboard())


@router.message(Inventory.waiting_counts, F.text, ~F.text.startswith("/"))
async def on_cycle_counts(message, state, panel):
    match = re.fullmatch(r"\s*([0-9]{1,7})\s*\|\s*([0-9]{1,7})\s*", message.text or "")
    if not match or int(match[2]) > int(match[1]) or int(match[1]) > 1_000_000:
        await _send_prompt(message, state, "Введите два целых неотрицательных числа: проверено | расхождений. Расхождений не больше проверенных SKU.", reply_markup=cycle_keyboard())
        return
    counts = {"checkedSkuCount": int(match[1]), "discrepancies": int(match[2])}
    await state.update_data(cycle_counts=counts)
    await state.set_state(Inventory.waiting_confirmation)
    await _send_prompt(message, state, f"Проверено SKU: {counts['checkedSkuCount']}\nРасхождений: {counts['discrepancies']}\n\nПодтвердить завершение категории? Остатки YCLIENTS не изменятся.", reply_markup=cycle_keyboard(confirmation=True))


@router.message(Inventory.waiting_sku, F.text, ~F.text.startswith("/"))
async def on_cycle_sku(message, state, panel):
    try:
        parts = (message.text or "").split("|")
        if len(parts) != 2:
            raise ValueError()
        good_id, counted = int(parts[0].strip()), float(parts[1].strip().replace(",", "."))
        if good_id <= 0 or not math.isfinite(counted) or counted < 0:
            raise ValueError()
    except ValueError:
        await _send_prompt(message, state, "Нужны ID товара и неотрицательное количество: 42 | 3", reply_markup=cycle_keyboard())
        return
    data = await state.get_data()
    category = data.get("cycle_category")
    if not category:
        await on_inventory(message, state, panel)
        return
    try:
        preview = await panel.cycle_preview(telegram_id=message.from_user.id,
                                            campaign_id=data["cycle_campaign_id"],
                                            category_id=category["id"], good_id=good_id, counted=counted,
                                            revision=data.get("cycle_revision", 0))
        await state.update_data(cycle_sku_preview=preview)
        await state.set_state(Inventory.viewing)
        await _send_prompt(message, state,
                           f"<b>{escape(str(preview['title']))}</b>\nПо базе: {preview['expected']}\nПосчитано: {preview['counted']}\nРасхождение: {preview['delta']}\n\nТолько предпросмотр. Корректировка остатков отключена.",
                           reply_markup=cycle_keyboard(category), parse_mode="HTML")
    except ApiError as exc:
        await _send_prompt(message, state, exc.message, reply_markup=cycle_keyboard())
