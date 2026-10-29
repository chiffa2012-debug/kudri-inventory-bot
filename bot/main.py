"""Точка входа бота учёта расходников «Кудри».

Long polling, а не вебхук: внешний адрес и сертификат боту не нужны, а нагрузка
здесь — несколько сообщений в день.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from .api_client import PanelClient
from .config import load_settings
from .handlers import router
from .cycle import router as cycle_router
from .action_log import ActionLog, ActionLogMiddleware
from .navigation import MainMenuResetMiddleware

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("kudri-inventory-bot")


async def start_polling_safely(bot: Bot, dispatcher: Dispatcher) -> None:
    """Keep pending updates and refuse to take over an existing webhook."""
    webhook = await bot.get_webhook_info()
    if webhook.url:
        raise RuntimeError("У бота настроен webhook. Polling не запущен; требуется проверить прежний сервис.")
    # No delete_webhook and no negative getUpdates offset: queued updates survive.
    await dispatcher.start_polling(bot)


async def main() -> None:
    settings = load_settings()
    panel = PanelClient(settings)

    bot = Bot(settings.bot_token)
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(cycle_router)
    dispatcher.include_router(router)

    # Reply-клавиатура служит глобальной навигацией. Сброс должен происходить
    # до проверки state-фильтров обработчиков, иначе кнопка станет вводом в
    # старом диалоге (например, названием подкатегории).
    dispatcher.message.outer_middleware(MainMenuResetMiddleware())

    if settings.action_log_path:
        action_log = ActionLog(settings.action_log_path)
        dispatcher.message.outer_middleware(ActionLogMiddleware(action_log))
        dispatcher.callback_query.outer_middleware(ActionLogMiddleware(action_log))

    # Клиент панели один на весь процесс: соединения переиспользуются, а
    # обработчики получают его аргументом и не создают свой.
    dispatcher["panel"] = panel
    dispatcher["settings"] = settings

    try:
        me = await bot.get_me()
        log.info("Подготовка запуска @%s, панель: %s", me.username, settings.api_base_url)
        await start_polling_safely(bot, dispatcher)
    finally:
        await panel.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Остановлен")
