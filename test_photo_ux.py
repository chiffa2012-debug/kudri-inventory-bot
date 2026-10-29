"""UX-регрессии: главное меню, серия фото и явное подтверждение прихода."""

from __future__ import annotations

import unittest
import io
import httpx
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import handlers
from bot.api_client import PanelClient
from bot.config import Settings
from bot.keyboards import BTN_RECEIVE, quantity_keyboard
from bot.navigation import MainMenuResetMiddleware
from bot.photo_intake import AutoPhotoResult, merge_label_reads
from bot.vision import LabelReadResult


class TenantContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_catalog_reads_always_send_authenticated_telegram_user(self):
        requests = []

        def transport(request):
            requests.append(request)
            if request.url.path.endswith("/find-good"):
                return httpx.Response(200, json={"ok": True, "exact": None, "matches": []})
            key = "categories" if request.url.path.endswith("/categories") else "positions"
            return httpx.Response(200, json={"ok": True, key: []})

        panel = PanelClient(Settings(
            bot_token="test", api_base_url="http://local.test", api_key="inventory-key"
        ))
        await panel.close()
        panel._client = httpx.AsyncClient(
            transport=httpx.MockTransport(transport), headers={"x-inventory-key": "inventory-key"}
        )
        try:
            await panel.find_good(telegram_id=77, title="Материал", barcode="4601234567893")
            await panel.categories(77)
            await panel.search("Материал", 77)
            await panel.open_positions(77)
        finally:
            await panel.close()

        self.assertEqual(len(requests), 4)
        self.assertTrue(all(request.url.params["telegramId"] == "77" for request in requests))


class FakeState:
    def __init__(self) -> None:
        self.state = "AdminSettings:waiting_add_name"
        self.data = {"draft": "stale"}

    async def clear(self) -> None:
        self.state = None
        self.data.clear()


class NavigationTests(unittest.IsolatedAsyncioTestCase):
    async def test_main_menu_action_clears_stale_dialog_before_dispatch(self):
        state = FakeState()
        event = SimpleNamespace(text=BTN_RECEIVE)
        observed = {}

        async def handler(_event, data):
            observed["state"] = data["state"].state
            return "handled"

        result = await MainMenuResetMiddleware()(handler, event, {"state": state})

        self.assertEqual(result, "handled")
        self.assertIsNone(observed["state"])
        self.assertEqual(state.data, {})

    async def test_free_text_does_not_cancel_dialog(self):
        state = FakeState()
        event = SimpleNamespace(text="Sanfor WC Gel")

        async def handler(_event, data):
            return data["state"].state

        result = await MainMenuResetMiddleware()(handler, event, {"state": state})

        self.assertEqual(result, "AdminSettings:waiting_add_name")

    async def test_unexpected_error_always_gets_safe_user_response(self):
        answers = []

        class Target:
            async def answer(self, text, reply_markup=None):
                answers.append(text)

        event = SimpleNamespace(update=SimpleNamespace(message=Target(), callback_query=None))
        handled = await handlers.on_error(event, RuntimeError("secret internal detail"))

        self.assertTrue(handled)
        self.assertEqual(len(answers), 1)
        self.assertNotIn("secret internal detail", answers[0])


class MultiPhotoTests(unittest.TestCase):
    def test_front_title_and_back_barcode_are_merged_into_one_sku(self):
        merged = merge_label_reads([
            LabelReadResult("Sanfor WC Gel", "", "", "high"),
            LabelReadResult("", "4602984004614", "", "high"),
        ])

        self.assertEqual(merged.title, "Sanfor WC Gel")
        self.assertEqual(merged.barcode, "4602984004614")
        self.assertEqual(merged.score, "high")

    def test_common_quantities_are_one_tap_actions(self):
        callbacks = [
            button.callback_data
            for row in quantity_keyboard().inline_keyboard
            for button in row
        ]
        self.assertEqual(callbacks[:3], ["qty:1", "qty:2", "qty:5"])

    def test_low_confidence_title_does_not_replace_high_confidence_title(self):
        merged = merge_label_reads([
            LabelReadResult("Sanfor WC Gel", "", "", "high"),
            LabelReadResult("Санфор что-то", "4602984004614", "", "low"),
        ])

        self.assertEqual(merged.title, "Sanfor WC Gel")
        self.assertEqual(merged.barcode, "4602984004614")


class DirectPhotoTests(unittest.IsolatedAsyncioTestCase):
    async def test_photo_without_menu_is_classified_and_starts_label_flow(self):
        class State(FakeState):
            async def get_data(self):
                return dict(self.data)

            async def update_data(self, **values):
                self.data.update(values)

            async def set_state(self, value):
                self.state = value

        class Bot:
            async def get_file(self, _file_id):
                return SimpleNamespace(file_path="photo.jpg")

            async def download_file(self, _path):
                return io.BytesIO(b"jpeg")

            async def delete_message(self, *_args):
                return None

        class Message:
            def __init__(self):
                self.from_user = SimpleNamespace(id=77)
                self.chat = SimpleNamespace(id=900)
                self.message_id = 100
                self.photo = [SimpleNamespace(file_id="photo")]
                self.bot = Bot()
                self.answers = []

            async def answer(self, text, reply_markup=None, parse_mode=None):
                self.answers.append({"text": text, "reply_markup": reply_markup, "parse_mode": parse_mode})
                return SimpleNamespace(message_id=1000 + len(self.answers))

        class Panel:
            async def whoami(self, _telegram_id):
                return {"id": "owner", "name": "Owner"}

            async def find_good(self, **_kwargs):
                return {"exact": None, "matches": []}

            async def categories(self, _telegram_id):
                return [{"id": 10, "title": "Бытовое"}]

        settings = Settings(
            bot_token="test", api_base_url="http://127.0.0.1:3007", api_key="test",
            vision_provider="hermes-local", inventory_vision_url="http://127.0.0.1:8642/v1/inventory/vision",
            inventory_vision_key="internal",
        )
        state, message = State(), Message()
        result = AutoPhotoResult("label", label={
            "title": "Sanfor WC Gel", "barcode": "4602984004614", "score": "high"
        })

        with patch.object(handlers, "read_auto_from_bytes", AsyncMock(return_value=result)):
            await handlers.on_unclassified_photo(message, state, Panel(), settings)

        self.assertEqual(state.state, handlers.Receive.waiting_category)
        self.assertEqual(state.data["barcode"], "4602984004614")
        self.assertIn("К какой категории", message.answers[-1]["text"])


if __name__ == "__main__":
    unittest.main()
