import unittest

from bot.keyboards import (
    BTN_SETTINGS,
    category_goods_page,
    category_admin_keyboard,
    good_delete_confirm_keyboard,
    main_menu,
    settings_keyboard,
    service_users_keyboard,
    unit_keyboard,
)
from bot.handlers import service_users_text


def button_texts(markup):
    return [button.text for row in markup.keyboard for button in row]


def inline_buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


class SettingsKeyboardTests(unittest.TestCase):
    def test_owner_sees_settings_in_main_menu(self):
        self.assertIn(BTN_SETTINGS, button_texts(main_menu(can_manage=True)))

    def test_operator_sees_category_settings(self):
        self.assertIn(BTN_SETTINGS, button_texts(main_menu(can_manage=True)))

    def test_settings_has_one_clear_category_entry(self):
        buttons = inline_buttons(settings_keyboard(can_manage_users=True))
        self.assertEqual(buttons[0].text, "📁 Категории и материалы")
        self.assertEqual(buttons[0].callback_data, "settings:categories")
        self.assertEqual(buttons[-1].callback_data, "settings:close")

    def test_operator_settings_hide_user_management(self):
        callbacks = {button.callback_data for button in inline_buttons(settings_keyboard(can_manage_users=False))}
        self.assertIn("settings:categories", callbacks)
        self.assertNotIn("settings:users", callbacks)

    def test_only_service_owner_gets_global_users_button(self):
        allowed = {button.callback_data for button in inline_buttons(settings_keyboard(True, can_view_service_users=True))}
        ordinary = {button.callback_data for button in inline_buttons(settings_keyboard(True, can_view_service_users=False))}
        self.assertIn("settings:service-users", allowed)
        self.assertNotIn("settings:service-users", ordinary)

    def test_global_users_view_has_totals_and_pagination(self):
        payload = {
            "summary": {"organizations": 4, "users": 12, "webUsers": 3, "telegramUsers": 9,
                        "activeUsers": 8, "pendingUsers": 2, "blockedUsers": 2},
            "page": 2, "pages": 3,
            "users": [{"type": "telegram", "name": "Алена <script>", "telegramId": "123",
                       "role": "operator", "status": "active", "organizations": [{"title": "Салон А"}]}],
        }
        text = service_users_text(payload)
        self.assertIn("Организаций: 4", text)
        self.assertIn("Пользователей: 12", text)
        self.assertIn("2/3", text)
        self.assertNotIn("<script>", text)
        callbacks = [button.callback_data for button in inline_buttons(service_users_keyboard(2, 3))]
        self.assertEqual(callbacks[:2], ["settings:service-users:1", "settings:service-users:3"])

    def test_category_admin_page_has_crud_actions(self):
        page = {
            "current": {"id": 22, "title": "Маникюр"},
            "parentId": 11,
            "children": [{"id": 23, "title": "Базы"}],
            "goods": [],
            "isRoot": False,
        }
        callbacks = {button.callback_data for button in inline_buttons(category_admin_keyboard(page))}
        self.assertIn("settings:cat:23", callbacks)
        self.assertIn("settings:cat-add:22", callbacks)
        self.assertIn("settings:cat-rename:22", callbacks)
        self.assertIn("settings:cat-delete:22", callbacks)
        self.assertIn("settings:cat:11", callbacks)

    def test_root_category_cannot_be_renamed_or_deleted(self):
        page = {
            "current": {"id": 11, "title": "Расходные материалы"},
            "parentId": None,
            "children": [],
            "goods": [],
            "isRoot": True,
        }
        callbacks = {button.callback_data for button in inline_buttons(category_admin_keyboard(page))}
        self.assertNotIn("settings:cat-rename:11", callbacks)
        self.assertNotIn("settings:cat-delete:11", callbacks)

    def test_material_delete_requires_explicit_confirmation(self):
        callbacks = [button.callback_data for button in inline_buttons(good_delete_confirm_keyboard())]
        self.assertEqual(callbacks, ["settings:confirm-good-delete", "settings:categories"])

    def test_new_material_unit_choice_is_explicit(self):
        buttons = inline_buttons(unit_keyboard())
        self.assertEqual([button.text for button in buttons[:2]], ["🔢 штуки", "🧴 миллилитры"])
        self.assertEqual([button.callback_data for button in buttons[:2]], ["unit:pcs", "unit:ml"])

    def test_category_materials_are_paginated_without_losing_total(self):
        page = {
            "current": {"id": 22, "title": "Для волос"},
            "parentId": 11,
            "children": [],
            "goods": [{"goodId": index, "title": f"Краска {index}"} for index in range(1, 15)],
            "isRoot": False,
        }

        visible, current, pages, total = category_goods_page(page, 1)

        self.assertEqual([item["goodId"] for item in visible], [7, 8, 9, 10, 11, 12])
        self.assertEqual((current, pages, total), (1, 3, 14))

    def test_category_keyboard_has_previous_and_next_page_buttons(self):
        page = {
            "current": {"id": 22, "title": "Для волос"},
            "parentId": 11,
            "children": [],
            "goods": [{"goodId": index, "title": f"Краска {index}"} for index in range(1, 15)],
            "isRoot": False,
        }

        buttons = inline_buttons(category_admin_keyboard(page, goods_page=1))
        callbacks = [button.callback_data for button in buttons]

        self.assertEqual(callbacks[:6], [f"settings:good:{index}" for index in range(7, 13)])
        self.assertIn("settings:cat-page:22:0", callbacks)
        self.assertIn("settings:cat-page:22:2", callbacks)
        self.assertIn("settings:page-current", callbacks)


if __name__ == "__main__":
    unittest.main()
