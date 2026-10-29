"""Регрессии фото-сценария без Telegram, OpenAI и YCLIENTS."""

from __future__ import annotations

import io
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import handlers
from bot.api_client import ApiError
from bot.config import Settings
from bot.keyboards import scan_match_keyboard
from bot.scan_history import ScanHistoryStore
from bot.vision import LabelReadResult


class FakeState:
    def __init__(self, data: dict | None = None) -> None:
        self.data = dict(data or {})
        self.state = None

    async def get_state(self):
        return self.state.state if self.state is not None else None

    async def get_data(self) -> dict:
        return dict(self.data)

    async def update_data(self, **values) -> None:
        self.data.update(values)

    async def set_state(self, value) -> None:
        self.state = value

    async def clear(self) -> None:
        self.data.clear()
        self.state = None


class FakeBot:
    def __init__(self) -> None:
        self.deleted: list[tuple[int, int]] = []

    async def get_file(self, _file_id: str):
        return SimpleNamespace(file_path="label.jpg")

    async def download_file(self, _path: str):
        return io.BytesIO(b"jpeg")

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        self.deleted.append((chat_id, message_id))


class FakeMessage:
    def __init__(
        self,
        user_id: int = 77,
        message_id: int = 100,
        text: str | None = None,
        bot: FakeBot | None = None,
    ) -> None:
        self.from_user = SimpleNamespace(id=user_id)
        self.chat = SimpleNamespace(id=900)
        self.message_id = message_id
        self.text = text
        self.photo = [SimpleNamespace(file_id="photo-large")]
        self.bot = bot or FakeBot()
        self.answers: list[dict] = []

    async def answer(self, text: str, reply_markup=None, parse_mode=None):
        sent = SimpleNamespace(message_id=1000 + len(self.answers))
        self.answers.append(
            {"text": text, "reply_markup": reply_markup, "parse_mode": parse_mode}
        )
        return sent

    async def edit_text(self, text: str, reply_markup=None, parse_mode=None):
        self.answers.append({"text": text, "reply_markup": reply_markup, "parse_mode": parse_mode})


class FakeCall:
    def __init__(self, message: FakeMessage, data: str = "scan:recognize") -> None:
        self.message = message
        self.data = data
        self.from_user = message.from_user
        self.bot = message.bot
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append({"text": text, "show_alert": show_alert})


class FakePanel:
    def __init__(self, exact=None) -> None:
        self.exact = exact
        self.lookups: list[tuple[str, str]] = []
        self.received: list[dict] = []

    async def whoami(self, _telegram_id: int):
        return {"id": "owner", "name": "Владелец"}

    async def find_good(self, *, telegram_id=None, title: str = "", barcode: str = ""):
        self.lookups.append((title, barcode))
        return {"exact": self.exact, "matches": []}

    async def receive(self, **payload):
        self.received.append(payload)
        return {
            "created": True,
            "added": payload.get("amount", payload.get("packageSize", 1) * payload.get("packageCount", 1)),
            "position": {
                "title": payload["title"],
                "unit": payload.get("unit", "шт"),
                "stock": payload.get("amount", payload.get("packageSize", 1) * payload.get("packageCount", 1)),
            },
        }

    async def categories(self, _telegram_id=None):
        return [{"id": 10, "title": "Бытовое"}]


SETTINGS = Settings(
    bot_token="test",
    api_base_url="http://127.0.0.1:3007",
    api_key="test",
    openai_api_key="test",
)


class ScanHistoryTests(unittest.TestCase):
    def test_first_scan_is_new_and_second_scan_is_recent(self):
        history = ScanHistoryStore(ttl_seconds=600, max_items_per_user=30)

        self.assertFalse(history.was_recent(10, "460123", now=1_000))
        history.remember(10, "460123", "Перчатки", now=1_000)
        self.assertTrue(history.was_recent(10, "460123", now=1_001))

    def test_history_survives_fsm_clear_but_is_isolated_by_user(self):
        history = ScanHistoryStore(ttl_seconds=600, max_items_per_user=30)
        history.remember(10, "460123", "Перчатки", now=1_000)

        self.assertTrue(history.was_recent(10, "460123", now=1_001))
        self.assertFalse(history.was_recent(11, "460123", now=1_001))

    def test_expired_scan_is_not_a_duplicate(self):
        history = ScanHistoryStore(ttl_seconds=600, max_items_per_user=30)
        history.remember(10, "460123", "Перчатки", now=1_000)

        self.assertFalse(history.was_recent(10, "460123", now=1_601))


class PhotoFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.history = ScanHistoryStore(ttl_seconds=600, max_items_per_user=30)
        self.history_patch = patch.object(handlers, "scan_history", self.history)
        self.history_patch.start()
        self.vision_patch = patch.object(
            handlers,
            "read_label_from_bytes",
            AsyncMock(
                return_value=LabelReadResult(
                    title="Перчатки нитриловые M",
                    barcode="4601234567890",
                    raw='{"title":"Перчатки нитриловые M","barcode":"4601234567890"}',
                    score="high",
                )
            ),
        )
        self.vision_patch.start()

    async def asyncTearDown(self) -> None:
        self.vision_patch.stop()
        self.history_patch.stop()

    async def test_first_unknown_barcode_asks_category_after_recognition(self):
        state = FakeState()
        message = FakeMessage()
        panel = FakePanel()

        await handlers.on_scan_photo(message, state, panel, SETTINGS)
        await handlers.on_scan_recognize(FakeCall(message), state, panel, SETTINGS)

        self.assertEqual(state.state, handlers.Receive.waiting_category)
        self.assertEqual(state.data["barcode"], "4601234567890")
        self.assertEqual(state.data["category_options"], [{"id": 10, "title": "Бытовое"}])
        self.assertEqual(panel.lookups, [("Перчатки нитриловые M", "4601234567890")])
        self.assertIn("категории", message.answers[-1]["text"])

    async def test_photos_are_collected_before_one_recognition(self):
        state = FakeState()
        first = FakeMessage(message_id=100)
        second = FakeMessage(message_id=101, bot=first.bot)

        await handlers.on_scan_photo(first, state, FakePanel(), SETTINGS)
        await handlers.on_scan_photo(second, state, FakePanel(), SETTINGS)

        self.assertEqual(state.state, handlers.Receive.waiting_scan_photo)
        self.assertEqual(state.data["scan_photo_ids"], ["photo-large", "photo-large"])
        self.assertIn("Фото 2", second.answers[-1]["text"])
        callbacks = [
            button.callback_data
            for row in second.answers[-1]["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertIn("scan:recognize", callbacks)

    async def test_second_unknown_barcode_warns_after_fsm_was_cleared(self):
        first_state = FakeState()
        first_message = FakeMessage()
        await handlers.on_scan_photo(first_message, first_state, FakePanel(), SETTINGS)
        await handlers.on_scan_recognize(FakeCall(first_message), first_state, FakePanel(), SETTINGS)
        await first_state.clear()

        second_state = FakeState()
        second_message = FakeMessage()
        await handlers.on_scan_photo(second_message, second_state, FakePanel(), SETTINGS)
        await handlers.on_scan_recognize(FakeCall(second_message), second_state, FakePanel(), SETTINGS)

        self.assertEqual(second_state.state, handlers.Receive.waiting_scan_confirm)
        self.assertIn("уже сканировали", second_message.answers[-1]["text"])
        callbacks = [
            button.callback_data
            for row in second_message.answers[-1]["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertEqual(callbacks, ["scan:again", "scan:skip"])

    async def test_quantity_input_removes_only_previous_recognition_prompt(self):
        bot = FakeBot()
        state = FakeState()
        photo_message = FakeMessage(message_id=100, bot=bot)
        panel = FakePanel()
        await handlers.on_scan_photo(photo_message, state, panel, SETTINGS)
        await handlers.on_scan_recognize(FakeCall(photo_message), state, panel, SETTINGS)
        await state.update_data(category_id=10, unit="мл")
        await state.set_state(handlers.Receive.waiting_quantity)
        recognition_prompt_id = state.data["prompt_message_id"]

        quantity_message = FakeMessage(message_id=101, text="3", bot=bot)
        await handlers.on_quantity_typed(quantity_message, state)

        self.assertIn((900, recognition_prompt_id), bot.deleted)
        self.assertNotIn((900, photo_message.message_id), bot.deleted)
        self.assertNotIn((900, quantity_message.message_id), bot.deleted)

    async def test_fallback_deletes_only_previous_bot_prompt_not_user_photo(self):
        state = FakeState({"prompt_message_id": 50})
        message = FakeMessage(message_id=100)
        with patch.object(
            handlers,
            "read_label_from_bytes",
            AsyncMock(return_value=LabelReadResult("", "", "")),
        ):
            await handlers.on_scan_photo(message, state, FakePanel(), SETTINGS)
            await handlers.on_scan_recognize(FakeCall(message), state, FakePanel(), SETTINGS)

        self.assertEqual(state.state, handlers.Receive.waiting_title)
        self.assertEqual(message.bot.deleted, [(900, 50)])
        self.assertNotIn((900, 100), message.bot.deleted)

    async def test_vision_without_api_key_returns_manual_fallback_without_network(self):
        from bot.vision import read_label_from_bytes

        result = await read_label_from_bytes(
            api_key="",
            model="gpt-4o-mini",
            file_bytes=b"jpeg",
        )

        self.assertEqual(result, LabelReadResult("", "", ""))

    async def test_recognized_sku_reaches_backend_with_quantity_price_and_barcode(self):
        state = FakeState()
        panel = FakePanel()
        photo_message = FakeMessage()
        await handlers.on_scan_photo(photo_message, state, panel, SETTINGS)
        await handlers.on_scan_recognize(FakeCall(photo_message), state, panel, SETTINGS)
        await state.update_data(category_id=10, unit="мл")
        await state.set_state(handlers.Receive.waiting_quantity)

        quantity_message = FakeMessage(text="100")
        await handlers.on_quantity_typed(quantity_message, state)
        await handlers.on_package_count(FakeMessage(text="3"), state)
        price_prompt_id = state.data["prompt_message_id"]
        price_message = FakeMessage(text="450")
        await handlers.on_price_typed(price_message, state, panel)

        self.assertEqual(panel.received, [])
        self.assertEqual(state.state, handlers.Receive.waiting_receive_confirm)
        self.assertIn("Подтвердите", price_message.answers[-1]["text"])

        await handlers.on_receive_confirm(
            FakeCall(price_message, data="receive:confirm"), state, panel
        )

        self.assertEqual(
            panel.received,
            [
                {
                    "telegramId": 77,
                    "goodId": None,
                    "title": "Перчатки нитриловые M",
                    "packageSize": 100.0,
                    "packageCount": 3.0,
                    "packagePrice": 450.0,
                    "idempotencyKey": panel.received[0]["idempotencyKey"],
                    "categoryId": 10,
                    "barcode": "4601234567890",
                    "unit": "мл",
                    "photoFileId": "photo-large",
                }
            ],
        )
        self.assertIn((900, price_prompt_id), price_message.bot.deleted)
        self.assertIsNone(state.state)

    async def test_category_name_is_kept_and_shown_in_confirmation(self):
        state = FakeState({
            "title": "Краска",
            "barcode": "4601",
            "category_options": [{"id": 10, "title": "Волосы"}],
        })
        message = FakeMessage()

        await handlers.on_category_picked(FakeCall(message, data="cat:10"), state)
        self.assertEqual(state.data["category_title"], "Волосы")
        self.assertEqual(state.state, handlers.Receive.waiting_unit)

        await handlers.on_unit_picked(FakeCall(message, data="unit:ml"), state)
        self.assertEqual(state.data["unit"], "мл")
        self.assertEqual(state.state, handlers.Receive.waiting_quantity)
        self.assertIn("мл", message.answers[-1]["text"])

        await state.update_data(quantity=1)
        await state.set_state(handlers.Receive.waiting_price)
        await handlers.on_zero_price(FakeCall(message, data="price:0"), state)

        self.assertIn("Категория: Волосы", message.answers[-1]["text"])

    async def test_invalid_category_returns_to_filtered_category_choice(self):
        class RejectingPanel(FakePanel):
            async def receive(self, **payload):
                raise ApiError("Выберите категорию расходного материала", "category_required")

        state = FakeState({
            "title": "Краска",
            "barcode": "4601",
            "category_id": 999,
            "category_title": "Старая категория",
            "quantity": 1,
            "unit": "шт",
            "price": 0,
        })
        state.state = handlers.Receive.waiting_receive_confirm
        message = FakeMessage()

        await handlers.on_receive_confirm(
            FakeCall(message, data="receive:confirm"), state, RejectingPanel()
        )

        self.assertEqual(state.state, handlers.Receive.waiting_category)
        self.assertEqual(state.data["category_options"], [{"id": 10, "title": "Бытовое"}])
        self.assertIn("Выберите конечную категорию заново", message.answers[-1]["text"])

    async def test_corrected_scan_title_is_checked_again_before_quantity(self):
        existing = {
            "goodId": 55,
            "title": "Перчатки нитриловые L",
            "stock": 4,
            "unit": "шт",
        }
        state = FakeState(
            {
                "skip_category": True,
                "scan_editing": True,
                "candidate_barcode": "",
            }
        )
        message = FakeMessage(text="Перчатки нитриловые L")
        panel = FakePanel(exact=existing)

        await handlers.on_title_typed(message, state, panel)

        self.assertEqual(state.state, handlers.Receive.waiting_scan_confirm)
        self.assertEqual(state.data["scan_candidate_good_id"], 55)
        self.assertEqual(panel.lookups, [("Перчатки нитриловые L", "")])
        self.assertIn("Найдено совпадение", message.answers[-1]["text"])

    def test_existing_match_keyboard_does_not_promise_forced_duplicate(self):
        keyboard = scan_match_keyboard(42)
        labels = [button.text for row in keyboard.inline_keyboard for button in row]
        callbacks = [
            button.callback_data for row in keyboard.inline_keyboard for button in row
        ]

        self.assertNotIn("➕ Завести как новый", labels)
        self.assertNotIn("scan:new", callbacks)
        self.assertIn("scan:edit", callbacks)


if __name__ == "__main__":
    unittest.main()
