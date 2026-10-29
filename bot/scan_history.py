"""Короткая память сканов, независимая от состояния текущего диалога.

FSM очищается после каждой операции, а предупреждение о случайном повторе
должно работать между двумя отдельными пополнениями. Поэтому последние
штрихкоды хранятся в памяти процесса отдельно, с коротким TTL.
"""

from __future__ import annotations

from time import time


class ScanHistoryStore:
    def __init__(self, *, ttl_seconds: int = 600, max_items_per_user: int = 30) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_items_per_user = max_items_per_user
        self._items: dict[int, list[dict]] = {}

    def _fresh(self, user_id: int, now: float) -> list[dict]:
        fresh = [
            item
            for item in self._items.get(user_id, [])
            if now - item["at"] <= self.ttl_seconds
        ]
        if fresh:
            self._items[user_id] = fresh
        else:
            self._items.pop(user_id, None)
        return fresh

    def was_recent(self, user_id: int, barcode: str, *, now: float | None = None) -> bool:
        if not barcode:
            return False
        checked_at = time() if now is None else now
        return any(
            item["barcode"] == barcode for item in self._fresh(user_id, checked_at)
        )

    def remember(
        self,
        user_id: int,
        barcode: str,
        title: str,
        *,
        now: float | None = None,
    ) -> None:
        if not barcode:
            return
        saved_at = time() if now is None else now
        fresh = self._fresh(user_id, saved_at)
        fresh.append({"barcode": barcode, "title": title, "at": saved_at})
        self._items[user_id] = fresh[-self.max_items_per_user :]


scan_history = ScanHistoryStore()
