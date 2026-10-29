"""Startup preserves Telegram backlog; no-key flows never download images."""
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import handlers, main
from test_receipt_flow import FakeState, FakeMessage, FakeCall, FakePanel, SETTINGS

NO_KEY = replace(SETTINGS, openai_api_key="")


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_webhook_blocks_polling_without_deletion(self):
        bot = SimpleNamespace(get_webhook_info=AsyncMock(return_value=SimpleNamespace(url="https://old-bot.example/hook")),
                              delete_webhook=AsyncMock())
        dispatcher = SimpleNamespace(start_polling=AsyncMock())
        with self.assertRaises(RuntimeError):
            await main.start_polling_safely(bot, dispatcher)
        bot.delete_webhook.assert_not_awaited()
        dispatcher.start_polling.assert_not_awaited()

    async def test_pending_updates_survive_start_and_restart(self):
        pending = ["photo-1", "command-2"]
        received = []
        async def poll(bot):
            received.extend(pending)
            pending.clear()
        async def delete(**kwargs):
            if kwargs.get("drop_pending_updates"):
                pending.clear()
        bot = SimpleNamespace(get_webhook_info=AsyncMock(return_value=SimpleNamespace(url="", pending_update_count=2)),
                              delete_webhook=AsyncMock(side_effect=delete))
        dispatcher = SimpleNamespace(start_polling=poll)
        await main.start_polling_safely(bot, dispatcher)
        pending.append("photo-after-restart")
        await main.start_polling_safely(bot, dispatcher)
        self.assertEqual(received, ["photo-1", "command-2", "photo-after-restart"])
        bot.delete_webhook.assert_not_awaited()

    async def test_failed_webhook_check_does_not_start_polling(self):
        bot = SimpleNamespace(get_webhook_info=AsyncMock(side_effect=RuntimeError("offline")))
        dispatcher = SimpleNamespace(start_polling=AsyncMock())
        with self.assertRaises(RuntimeError):
            await main.start_polling_safely(bot, dispatcher)
        dispatcher.start_polling.assert_not_awaited()


class ManualFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_label_entry_without_key_immediately_requests_manual_title(self):
        call, state = FakeCall("scan"), FakeState()
        await handlers.on_scan_start(call, state, NO_KEY)
        self.assertEqual(state.state, handlers.Receive.waiting_title)
        self.assertTrue(state.data["scan_editing"])
        self.assertIn("Распознавание фото временно недоступно", call.message.edits[-1]["text"])
        self.assertEqual(state.data["prompt_message_id"], call.message.message_id)

    async def test_receipt_entry_without_key_immediately_requests_manual_lines(self):
        message, state = FakeMessage(), FakeState({"prompt_message_id": 50})
        await handlers.on_receipt_start(message, state, FakePanel(), NO_KEY)
        self.assertEqual(state.state, handlers.Receipt.waiting_manual_lines)
        self.assertIn("Распознавание фото временно недоступно", message.answers[-1]["text"])
        self.assertIn("количество", message.answers[-1]["text"])
        self.assertEqual(message.bot.deleted, [(900, 50)])

    async def test_no_key_photos_never_download_or_call_vision(self):
        for handler, expected_state in [(handlers.on_scan_photo, handlers.Receive.waiting_title),
                                        (handlers.on_receipt_photo, handlers.Receipt.waiting_manual_lines)]:
            with self.subTest(handler=handler.__name__):
                message, state = FakeMessage(), FakeState({"prompt_message_id": 50})
                message.bot.get_file = AsyncMock(side_effect=AssertionError("must not fetch"))
                message.bot.download_file = AsyncMock(side_effect=AssertionError("must not download"))
                with patch.object(handlers, "read_label_from_bytes", AsyncMock(side_effect=AssertionError("must not OCR"))) as label, \
                     patch.object(handlers, "read_receipt_from_bytes", AsyncMock(side_effect=AssertionError("must not OCR"))) as receipt:
                    await handler(message, state, FakePanel(), NO_KEY)
                    label.assert_not_awaited()
                    receipt.assert_not_awaited()
                message.bot.get_file.assert_not_awaited()
                message.bot.download_file.assert_not_awaited()
                self.assertEqual(state.state, expected_state)
                self.assertIn("Распознавание фото временно недоступно", message.answers[-1]["text"])
                self.assertNotIn((900, message.message_id), message.bot.deleted)

    async def test_keyed_entry_still_requests_photo(self):
        call, state = FakeCall("scan"), FakeState()
        await handlers.on_scan_start(call, state, SETTINGS)
        self.assertEqual(state.state, handlers.Receive.waiting_scan_photo)
        message = FakeMessage()
        await handlers.on_receipt_start(message, state, FakePanel(), SETTINGS)
        self.assertEqual(state.state, handlers.Receipt.waiting_photo)

    async def test_photo_sent_after_manual_entry_keeps_manual_prompt(self):
        for handler, manual_state in [(handlers.on_manual_label_photo, handlers.Receive.waiting_title),
                                      (handlers.on_manual_receipt_photo, handlers.Receipt.waiting_manual_lines)]:
            message, state = FakeMessage(), FakeState({"prompt_message_id": 50})
            await state.set_state(manual_state)
            message.bot.get_file = AsyncMock(side_effect=AssertionError("must not fetch"))
            await handler(message, state, NO_KEY)
            self.assertEqual(state.state, manual_state)
            self.assertIn("Распознавание фото временно недоступно", message.answers[-1]["text"])
            message.bot.get_file.assert_not_awaited()
            self.assertEqual(message.bot.deleted, [(900, 50)])


if __name__ == "__main__":
    unittest.main()
