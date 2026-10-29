import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bot.action_log import ActionLog, classify_update


class ActionLogTests(unittest.TestCase):
    def test_prunes_older_than_one_day_and_caps_entries(self):
        now = datetime(2026, 9, 29, 8, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "actions.jsonl"
            path.write_text(
                json.dumps({"at": (now - timedelta(hours=25)).isoformat(), "event": "old"}) + "\n",
                encoding="utf-8",
            )
            log = ActionLog(path, retention=timedelta(hours=24), max_entries=3, clock=lambda: now)
            for index in range(4):
                log.write("action", telegram_id=100000000 + index, state="Flow:step", status="ok")

            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), 3)
            self.assertNotIn("old", [row["event"] for row in rows])
            self.assertTrue(all(row["actor"].startswith("u_") for row in rows))
            self.assertNotIn("100000", path.read_text(encoding="utf-8"))

    def test_writes_only_allowlisted_fields_and_safe_error_code(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "actions.jsonl"
            ActionLog(path).write(
                "registration_confirm",
                telegram_id=123456789,
                state="Registration:waiting_confirm",
                status="error",
                error_code="bad token with private text",
                organization_id="org-a",
                ignored_secret="must-not-appear",
            )
            row = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                set(row),
                {"at", "event", "actor", "state", "status", "errorCode", "organizationId"},
            )
            self.assertEqual(row["errorCode"], "bad_token_with_private_text")
            self.assertNotIn("must-not-appear", path.read_text(encoding="utf-8"))

    def test_classifies_buttons_without_logging_message_text_or_entity_ids(self):
        self.assertEqual(classify_update(text="🏢 Создать организацию"), "registration_start")
        self.assertEqual(classify_update(callback_data="org:select:org-private-123"), "organization_select")
        self.assertEqual(classify_update(callback_data="settings:cat-delete:999"), "category_delete")
        self.assertEqual(classify_update(callback_data="settings:service-users:2"), "service_users_open")
        self.assertEqual(classify_update(callback_data="receive:confirm"), "receive_confirm")
        self.assertEqual(classify_update(has_photo=True), "photo_upload")
        self.assertEqual(classify_update(text="Секретное название материала"), "text_input")


if __name__ == "__main__":
    unittest.main()
