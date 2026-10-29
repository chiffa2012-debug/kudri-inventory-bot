"""Кнопки бота.

Всё, что можно сделать нажатием, делается нажатием: мастер вводит текст только
там, где значение нельзя предугадать — название новой позиции, количество и
цена.
"""

from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from .parsing import format_amount

BTN_RECEIVE = "📦 Пополнить"
BTN_DEPLETE = "🔻 Закончилось"
BTN_SCAN_LABEL = "📷 Сканировать этикетку"
BTN_RECEIPT = "🧾 Фото чека"
BTN_INVENTORY = "📋 Инвентаризация"
BTN_SETTINGS = "⚙️ Настройки"
BTN_CREATE_ORGANIZATION = "🏢 Создать организацию"
BTN_JOIN_ORGANIZATION = "🔑 Войти по коду"
BTN_SWITCH_ORGANIZATION = "🏢 Сменить организацию"


def main_menu(can_manage: bool = True, can_switch: bool = False) -> ReplyKeyboardMarkup:
    """Две кнопки внизу экрана — они всегда под рукой и не теряются в истории."""
    rows = [
            [KeyboardButton(text=BTN_RECEIVE), KeyboardButton(text=BTN_DEPLETE)],
            [KeyboardButton(text=BTN_RECEIPT), KeyboardButton(text=BTN_INVENTORY)],
        ]
    if can_manage:
        rows.append([KeyboardButton(text=BTN_SETTINGS)])
    if can_switch:
        rows.append([KeyboardButton(text=BTN_SWITCH_ORGANIZATION)])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        input_field_placeholder="Выберите действие",
    )


def registration_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_CREATE_ORGANIZATION)],
            [KeyboardButton(text=BTN_JOIN_ORGANIZATION)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Создайте организацию или войдите по коду",
    )


def source_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Да, подключить", callback_data="reg:source:yclients")],
        [InlineKeyboardButton(text="Нет, база бота", callback_data="reg:source:internal")],
        [InlineKeyboardButton(text="Подключить позже", callback_data="reg:source:later")],
        [InlineKeyboardButton(text="Отмена", callback_data="reg:cancel")],
    ])


def owner_name_keyboard(name: str) -> InlineKeyboardMarkup:
    clean = str(name or "").strip()
    rows = []
    if clean:
        shown = clean if len(clean) <= 40 else clean[:39] + "…"
        rows.append([InlineKeyboardButton(text=f"✅ Использовать: {shown}", callback_data="reg:owner:telegram")])
    rows.append([InlineKeyboardButton(text="✏️ Ввести другое имя", callback_data="reg:owner:type")])
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="reg:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def registration_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Создать организацию", callback_data="reg:confirm")],
        [InlineKeyboardButton(text="Отмена", callback_data="reg:cancel")],
    ])


def organization_switch_keyboard(organizations: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for organization in organizations:
        title = str(organization.get("title") or "Без названия")
        if len(title) > 48:
            title = title[:47] + "…"
        prefix = "✅ " if organization.get("active") else ""
        rows.append([InlineKeyboardButton(
            text=prefix + title,
            callback_data=f"org:select:{organization['id']}",
        )])
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="org:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_keyboard(can_manage_users: bool = False, can_view_service_users: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="📁 Категории и материалы", callback_data="settings:categories")],
        [InlineKeyboardButton(text="🛒 Список закупки", callback_data="material:shopping")],
    ]
    if can_manage_users:
        rows.append([InlineKeyboardButton(text="👥 Пользователи", callback_data="settings:users")])
    if can_view_service_users:
        rows.append([InlineKeyboardButton(text="👥 Показать всех пользователей", callback_data="settings:service-users")])
    rows.append([InlineKeyboardButton(text="Закрыть", callback_data="settings:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def users_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить пользователя", callback_data="settings:user-add")],
        [InlineKeyboardButton(text="← Назад", callback_data="settings:home")],
    ])


def service_users_keyboard(page: int, pages: int) -> InlineKeyboardMarkup:
    rows = []
    navigation = []
    if page > 1:
        navigation.append(InlineKeyboardButton(text="⬅️", callback_data=f"settings:service-users:{page - 1}"))
    if page < pages:
        navigation.append(InlineKeyboardButton(text="➡️", callback_data=f"settings:service-users:{page + 1}"))
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton(text="← Настройки", callback_data="settings:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


CATEGORY_GOODS_PAGE_SIZE = 6


def category_goods_page(page: dict, page_index: int = 0, page_size: int = CATEGORY_GOODS_PAGE_SIZE):
    goods = list(page.get("goods", []))
    total = len(goods)
    total_pages = max(1, (total + page_size - 1) // page_size)
    current = min(max(int(page_index), 0), total_pages - 1)
    start = current * page_size
    return goods[start:start + page_size], current, total_pages, total


def category_admin_keyboard(page: dict, goods_page: int = 0) -> InlineKeyboardMarkup:
    visible_goods, current_page, total_pages, _ = category_goods_page(page, goods_page)
    rows = [
        [InlineKeyboardButton(text=f"📁 {item['title']}", callback_data=f"settings:cat:{item['id']}")]
        for item in page.get("children", [])
    ]
    rows.extend(
        [InlineKeyboardButton(text=f"📦 {_position_label(item)}", callback_data=f"settings:good:{item['goodId']}")]
        for item in visible_goods
    )
    current_id = page["current"]["id"]
    if total_pages > 1:
        navigation = []
        if current_page > 0:
            navigation.append(InlineKeyboardButton(
                text="⬅️", callback_data=f"settings:cat-page:{current_id}:{current_page - 1}"
            ))
        navigation.append(InlineKeyboardButton(
            text=f"{current_page + 1}/{total_pages}", callback_data="settings:page-current"
        ))
        if current_page + 1 < total_pages:
            navigation.append(InlineKeyboardButton(
                text="➡️", callback_data=f"settings:cat-page:{current_id}:{current_page + 1}"
            ))
        rows.append(navigation)
    if page.get("goods"):
        rows.append([InlineKeyboardButton(text="📋 Сверить остатки", callback_data=f"stock-count:{current_id}")])
    rows.append([InlineKeyboardButton(text="➕ Подкатегория", callback_data=f"settings:cat-add:{current_id}")])
    if not page.get("isRoot", page.get("parentId") is None):
        rows.append([
            InlineKeyboardButton(text="✏️ Переименовать", callback_data=f"settings:cat-rename:{current_id}"),
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"settings:cat-delete:{current_id}"),
        ])
    if page.get("parentId") is not None:
        rows.append([InlineKeyboardButton(text="⬅️ Выше", callback_data=f"settings:cat:{page['parentId']}")])
    rows.append([InlineKeyboardButton(text="⬅️ Настройки", callback_data="settings:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def category_change_confirm_keyboard(action: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"settings:confirm-{action}")],
        [InlineKeyboardButton(text="Отмена", callback_data="settings:categories")],
    ])


def good_delete_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Да, убрать материал", callback_data="settings:confirm-good-delete")],
        [InlineKeyboardButton(text="Отмена", callback_data="settings:categories")],
    ])


def _position_label(position: dict, with_stock: bool = False) -> str:
    title = position["title"]
    if len(title) > 52:
        title = title[:51] + "…"
    if with_stock:
        return f"{title} · {format_amount(position['stock'])} {position.get('unit', 'шт')}"
    return title


def positions_keyboard(
    positions: list[dict], action: str, extra_new: bool = False, with_stock: bool = False
) -> InlineKeyboardMarkup:
    """По одной позиции в строке: названия длинные, в два столбца не читаются."""
    rows = [
        [
            InlineKeyboardButton(
                text=_position_label(item, with_stock),
                callback_data=f"{action}:{item['goodId']}",
            )
        ]
        for item in positions
    ]

    if extra_new:
        rows.append([InlineKeyboardButton(text="🔍 Найти по названию", callback_data="find")])
        rows.append([InlineKeyboardButton(text=BTN_SCAN_LABEL, callback_data="scan")])
        rows.append([InlineKeyboardButton(text="➕ Новая позиция", callback_data="new")])

    rows.append([InlineKeyboardButton(text="Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def categories_keyboard(categories: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=item["title"], callback_data=f"cat:{item['id']}")]
        for item in categories
    ]
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def catalog_keyboard(page: dict, can_manage: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"📁 {item['title']}", callback_data=f"catalog:{item['id']}")]
        for item in page.get("children", [])
    ]
    rows.extend(
        [InlineKeyboardButton(text=_position_label(item), callback_data=f"recv:{item['goodId']}")]
        for item in page.get("goods", [])
    )
    current_id = page["current"]["id"]
    if page.get("isLeaf"):
        rows.append([InlineKeyboardButton(text="➕ Добавить расходный материал", callback_data=f"catalog:new:{current_id}")])
        rows.append([InlineKeyboardButton(text=BTN_SCAN_LABEL, callback_data=f"catalog:scan:{current_id}")])
    if can_manage:
        rows.append([InlineKeyboardButton(text="📂 Добавить подкатегорию", callback_data=f"catalog:add-category:{current_id}")])
    if page.get("parentId") is not None:
        rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"catalog:{page['parentId']}")])
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_keyboard(action: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Да", callback_data=f"yes:{action}"),
            InlineKeyboardButton(text="Отмена", callback_data="cancel"),
        ]]
    )


def receive_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Добавить на склад", callback_data="receive:confirm")],
        [InlineKeyboardButton(text="✏️ Изменить данные", callback_data="receive:edit")],
        [InlineKeyboardButton(text="Отмена", callback_data="cancel")],
    ])


def quantity_keyboard(package_size: float | None = None, unit: str = "шт") -> InlineKeyboardMarkup:
    if package_size is not None:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"✅ {format_amount(package_size)} {unit} в упаковке", callback_data=f"qty:{format_amount(package_size)}")],
            [InlineKeyboardButton(text="✏️ Изменить фасовку", callback_data="qty:manual")],
            [InlineKeyboardButton(text="Отмена", callback_data="cancel")],
        ])
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="1", callback_data="qty:1"),
            InlineKeyboardButton(text="2", callback_data="qty:2"),
            InlineKeyboardButton(text="5", callback_data="qty:5"),
        ],
        [InlineKeyboardButton(text="✍️ Другое количество", callback_data="qty:manual")],
        [InlineKeyboardButton(text="Отмена", callback_data="cancel")],
    ])


def unit_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔢 штуки", callback_data="unit:pcs"),
            InlineKeyboardButton(text="🧴 миллилитры", callback_data="unit:ml"),
        ],
        [InlineKeyboardButton(text="Отмена", callback_data="cancel")],
    ])


def price_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Цена неизвестна — 0 ₽", callback_data="price:0")],
        [InlineKeyboardButton(text="Отмена", callback_data="cancel")],
    ])


def category_create_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Создать категорию", callback_data="catalog:confirm-category")],
        [InlineKeyboardButton(text="Отмена", callback_data="cancel")],
    ])


def scan_match_keyboard(good_id: int) -> InlineKeyboardMarkup:
    """Подтверждение распознанного по штрихкоду товара.

    Предполагаем, что это либо уже существующий товар, либо новая завязка в
    каталоге. Отдельная кнопка помогает не закрывать воронку случайными
    действиями.
    """

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Пополнить существующий", callback_data=f"scan:use:{good_id}")],
            [InlineKeyboardButton(text="✏️ Это другой товар", callback_data="scan:edit")],
            [InlineKeyboardButton(text="Отмена", callback_data="cancel")],
        ]
    )


def scan_duplicates_keyboard() -> InlineKeyboardMarkup:
    """Подтверждение того, что фото может быть отправлено повторно."""

    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="➕ Добавить ещё раз", callback_data="scan:again"),
            InlineKeyboardButton(text="⏭️ Это дубль, пропустить", callback_data="scan:skip"),
        ]]
    )


def scan_photo_collection_keyboard(count: int) -> InlineKeyboardMarkup:
    """Одна карточка для лицевой стороны, состава и штрихкода товара."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"✨ Распознать {count} фото", callback_data="scan:recognize")],
            [InlineKeyboardButton(text="Отмена", callback_data="cancel")],
        ]
    )


def recognition_retry_keyboard(kind: str) -> InlineKeyboardMarkup:
    callback = "scan:retry" if kind == "label" else "receipt:retry"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Повторить", callback_data=callback)],
            [InlineKeyboardButton(text="✍️ Ввести вручную", callback_data=f"{kind}:manual")],
            [InlineKeyboardButton(text="Отмена", callback_data="cancel")],
        ]
    )


def photo_kind_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🧴 Товар", callback_data="photo:kind:label"),
            InlineKeyboardButton(text="🧾 Чек", callback_data="photo:kind:receipt"),
        ],
        [InlineKeyboardButton(text="Отмена", callback_data="cancel")],
    ])


def receipt_preview_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Проверить чек", callback_data="receipt:confirm")],
            [InlineKeyboardButton(text="📋 Категории и строки", callback_data="receipt:edit")],
            [InlineKeyboardButton(text="Отмена", callback_data="cancel")],
        ]
    )


def receipt_write_keyboard() -> InlineKeyboardMarkup:
    """Отдельное подтверждение перед реальным изменением склада."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Добавить на склад", callback_data="receipt:write")],
            [InlineKeyboardButton(text="Отмена", callback_data="cancel")],
        ]
    )


def receipt_result_keyboard(*, partial: bool = False) -> InlineKeyboardMarkup:
    """После записи: продолжить незаписанные строки или закрыть сценарий."""
    if partial:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔁 Продолжить запись", callback_data="receipt:resume")],
                [InlineKeyboardButton(text="Отмена", callback_data="cancel")],
            ]
        )
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Готово", callback_data="receipt:done")]]
    )


def receipt_lines_keyboard(lines: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for index, line in enumerate(lines):
        title = str(line.get("title") or "Без названия")
        if len(title) > 42:
            title = title[:41] + "…"
        rows.append([
            InlineKeyboardButton(
                text=f"{index + 1}. {title}",
                callback_data=f"receipt:line:{index}",
            )
        ])
    rows.extend([
        [InlineKeyboardButton(text="← К предпросмотру", callback_data="receipt:back")],
        [InlineKeyboardButton(text="Отмена", callback_data="cancel")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def receipt_item_keyboard(index: int, *, matched: bool, skipped: bool, needs_review: bool = False) -> InlineKeyboardMarkup:
    rows = []
    if needs_review and not skipped:
        rows.append([InlineKeyboardButton(
            text="✅ Это расходник — на склад",
            callback_data=f"receipt:item:include:{index}",
        )])
    if not matched:
        rows.append([InlineKeyboardButton(
            text="📁 Выбрать категорию",
            callback_data=f"receipt:item:category:{index}",
        )])
    rows.append([InlineKeyboardButton(
        text="↩️ Вернуть в чек" if skipped else "🚫 Не добавлять на склад",
        callback_data=f"receipt:item:{'include' if skipped else 'skip'}:{index}",
    )])
    rows.extend([
        [InlineKeyboardButton(text="✏️ Исправить строку", callback_data=f"receipt:item:edit:{index}")],
        [InlineKeyboardButton(text="← К чеку", callback_data="receipt:back")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def receipt_category_keyboard(index: int, categories: list[dict]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        text=str(item.get("title") or "Категория")[:48],
        callback_data=f"receipt:item:cat:{index}:{int(item['id'])}",
    )] for item in categories[:30]]
    rows.append([InlineKeyboardButton(text="← К строке", callback_data=f"receipt:line:{index}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def receive_result_keyboard(has_category: bool = True, can_undo: bool = False) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="📷 Следующий в ту же категорию", callback_data="receive:next")]] if has_category else []
    if can_undo:
        rows.append([InlineKeyboardButton(text="↩️ Отменить приход", callback_data="material:undo-last")])
    rows.append([InlineKeyboardButton(text="Готово", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def material_keyboard(material: dict) -> InlineKeyboardMarkup:
    gid = material["goodId"]
    rows = [
        [InlineKeyboardButton(text="📦 Пополнить", callback_data=f"material:receive:{gid}"), InlineKeyboardButton(text="➖ Списать", callback_data=f"material:writeoff:{gid}")],
        [InlineKeyboardButton(text="✏️ Изменить", callback_data=f"material:edit:{gid}"), InlineKeyboardButton(text="📋 Реальный остаток", callback_data=f"material:count:{gid}")],
        [InlineKeyboardButton(text="🗑 Архивировать", callback_data=f"material:archive:{gid}")],
        [InlineKeyboardButton(text="⬅️ Категории", callback_data="settings:categories")],
    ]
    if material.get("photoFileId"):
        rows.insert(0, [InlineKeyboardButton(text="📷 Фото материала", callback_data=f"material:photo:{gid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def material_edit_keyboard(material: dict) -> InlineKeyboardMarkup:
    names = {"title": "Название", "barcode": "Штрихкод", "packageSize": "Фасовка", "minStock": "Минимальный остаток", "categoryId": "Категория"}
    rows = [[InlineKeyboardButton(text=names[field], callback_data=f"material:field:{field}")] for field in material.get("editableFields", []) if field in names]
    rows.append([InlineKeyboardButton(text="Назад", callback_data=f"settings:good:{material['goodId']}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def material_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="material:confirm")],
        [InlineKeyboardButton(text="Отмена", callback_data="cancel")],
    ])


def shopping_keyboard(items: list[dict], page: int = 0, pages: int = 1) -> InlineKeyboardMarkup:
    rows = []
    for item in items:
        gid = item["goodId"]
        rows.append([InlineKeyboardButton(text=f"{'✅ Заказано' if item.get('ordered') else '🛒 Заказать'} · {item['title']}"[:64], callback_data=f"material:order:{gid}:{0 if item.get('ordered') else 1}")])
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton(text="⬅️", callback_data=f"material:shopping-page:{page - 1}"))
    if page + 1 < pages:
        navigation.append(InlineKeyboardButton(text="➡️", callback_data=f"material:shopping-page:{page + 1}"))
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton(text="⬅️ Настройки", callback_data="settings:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
