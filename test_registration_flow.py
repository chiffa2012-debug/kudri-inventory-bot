import unittest

from bot.keyboards import (
    BTN_CREATE_ORGANIZATION,
    BTN_JOIN_ORGANIZATION,
    BTN_SWITCH_ORGANIZATION,
    main_menu,
    organization_switch_keyboard,
    registration_confirm_keyboard,
    registration_menu,
    source_menu,
    users_keyboard,
)
from bot.registration import Registration, registration_preview_text


def reply_texts(markup):
    return [button.text for row in markup.keyboard for button in row]


def inline_buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


class RegistrationUxTests(unittest.TestCase):
    def test_new_user_sees_registration_only(self):
        self.assertEqual(
            reply_texts(registration_menu()),
            [BTN_CREATE_ORGANIZATION, BTN_JOIN_ORGANIZATION],
        )

    def test_source_question_has_three_clear_answers(self):
        self.assertEqual(
            [button.text for button in inline_buttons(source_menu())],
            ["Да, подключить", "Нет, база бота", "Подключить позже", "Отмена"],
        )

    def test_multiple_memberships_add_switch_button(self):
        self.assertIn(BTN_SWITCH_ORGANIZATION, reply_texts(main_menu(can_manage=True, can_switch=True)))
        self.assertNotIn(BTN_SWITCH_ORGANIZATION, reply_texts(main_menu(can_manage=True, can_switch=False)))

    def test_switcher_contains_only_supplied_organizations(self):
        markup = organization_switch_keyboard([
            {"id": "org-a", "title": "Первая", "active": True},
            {"id": "org-b", "title": "Вторая", "active": False},
        ])
        buttons = inline_buttons(markup)
        self.assertEqual([button.callback_data for button in buttons[:-1]], ["org:select:org-a", "org:select:org-b"])
        self.assertTrue(buttons[0].text.startswith("✅"))

    def test_preview_escapes_telegram_html(self):
        text = registration_preview_text("<Салон>", "Анна & Ко", "internal")
        self.assertIn("&lt;Салон&gt;", text)
        self.assertIn("Анна &amp; Ко", text)
        self.assertNotIn("<Салон>", text)

    def test_confirmation_is_a_separate_button(self):
        callbacks = [button.callback_data for button in inline_buttons(registration_confirm_keyboard())]
        self.assertEqual(callbacks, ["reg:confirm", "reg:cancel"])

    def test_registration_states_cover_each_text_step(self):
        self.assertIsNotNone(Registration.waiting_title)
        self.assertIsNotNone(Registration.waiting_owner_name)
        self.assertIsNotNone(Registration.waiting_confirm)

    def test_owner_can_add_user_from_users_screen(self):
        callbacks = [button.callback_data for button in inline_buttons(users_keyboard())]
        self.assertEqual(callbacks, ["settings:user-add", "settings:home"])


if __name__ == "__main__":
    unittest.main()
