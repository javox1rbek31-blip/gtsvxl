"""Точка входа: запускает бота, юзербота, мониторинг и планировщик под надзором.

Каждый модуль работает в отдельной задаче; при падении он автоматически
перезапускается с нарастающей паузой, а администратор получает уведомление.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time
from zoneinfo import ZoneInfo

from .config import ConfigError, load_config

log = logging.getLogger("main")


async def supervise(name: str, factory, notifier) -> None:
    delay = 3
    while True:
        started = time.monotonic()
        try:
            await factory()
            log.warning("Модуль %s завершился — перезапуск", name)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("Модуль %s упал", name)
            await notifier.notify_admin(
                f"⚠️ Модуль «{name}» упал ({type(exc).__name__}) и перезапускается автоматически.",
                key=f"crash-{name}", min_interval=900,
            )
        if time.monotonic() - started > 120:
            delay = 3
        await asyncio.sleep(delay)
        delay = min(delay * 2, 120)


def _sync_pool(ctx):
    async def step() -> None:
        ctx.pool.sync()
    return step


async def monitor_loop(ctx) -> None:
    """Каждые 5 секунд: проверка задержек и перерывов; раз в час — уборка базы."""
    last_prune = 0.0
    while True:
        for name, step in (
            ("задержки", ctx.monitor.tick),
            ("перерывы", ctx.breaks.tick),
            ("автоставки", ctx.monitor.watch_bots),
            ("сервисы", ctx.services.tick),
            ("аккаунты Push", _sync_pool(ctx)),
        ):
            try:
                await step()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Ошибка шага «%s»", name)
        if time.time() - last_prune > 3600:
            last_prune = time.time()
            try:
                ctx.db.prune(int(time.time()))
            except Exception:
                log.exception("Ошибка очистки базы")
        await asyncio.sleep(5)


async def polling(ctx, dp) -> None:
    await ctx.bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(
        ctx.bot, allowed_updates=dp.resolve_used_update_types(), handle_signals=False,
    )


async def amain() -> None:
    cfg = load_config()          # сначала настройки: понятная ошибка, если чего-то не хватает

    from aiogram import Bot, Dispatcher
    from aiogram.fsm.storage.memory import MemoryStorage

    from .breaks import BreakService
    from .broadcast import Broadcaster
    from .context import Ctx
    from .db import Database
    from .group import build_group_router
    from .monitor import Monitor
    from .notifier import BotNotifier
    from .panel import build_admin_router, build_public_router
    from .services import Services
    from .accounts import AccountPool
    from .userbot import Userbot

    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("aiogram").setLevel(logging.INFO)

    tz = ZoneInfo(cfg.tz_name)
    db = Database(cfg.db_path)
    bot = Bot(cfg.bot_token)
    me = await bot.get_me()
    log.info("Бот @%s запущен, база: %s", me.username, cfg.db_path)

    notifier = BotNotifier(bot, db, cfg.admin_ids)
    breaks = BreakService(db, tz, notifier)
    monitor = Monitor(db, tz, notifier, breaks)
    monitor.reload()
    userbot = Userbot(cfg, db, monitor, notifier, tz)
    broadcaster = Broadcaster(db, bot, userbot, notifier, tz)
    ctx = Ctx(cfg=cfg, db=db, tz=tz, bot=bot, notifier=notifier, monitor=monitor,
              breaks=breaks, userbot=userbot, broadcaster=broadcaster)
    ctx.services = Services(db, tz, notifier, userbot, data_dir=str(cfg.data_dir))
    ctx.pool = AccountPool(cfg, db, notifier, tz, userbot)
    ctx.pool.sync()
    broadcaster.pool = ctx.pool
    notifier.userbot = userbot
    notifier.pool = ctx.pool
    ctx.services.pool = ctx.pool

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(build_group_router(ctx))
    dp.include_router(build_admin_router(ctx))
    dp.include_router(build_public_router(ctx))

    try:
        from aiogram.types import BotCommand, MenuButtonCommands
        await bot.set_my_commands([BotCommand(command="menu", description="🏠 Главное меню"),
                                   BotCommand(command="status", description="📊 Статус"),
                                   BotCommand(command="push", description="📣 Push"),
                                   BotCommand(command="report", description="📈 Отчёты"),
                                   BotCommand(command="functions", description="🎛 Функции")])
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except Exception:
        log.warning("Не удалось установить команды/кнопку меню", exc_info=True)

    interrupted = db.interrupted_broadcasts()
    note = "✅ Бот запущен. Откройте /menu."
    if interrupted:
        note += "\n⚠️ Рассылка была прервана перезапуском: " + ", ".join(
            f"#{r['id']} ({r['sent']}/{r['total']})" for r in interrupted)
    await notifier.notify_admin(note)

    tasks = [
        asyncio.create_task(supervise("бот (Telegram API)", lambda: polling(ctx, dp), notifier)),
        asyncio.create_task(supervise("юзербот", userbot.run, notifier)),
        asyncio.create_task(supervise("мониторинг", lambda: monitor_loop(ctx), notifier)),
    ]

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    await stop.wait()

    log.info("Останавливаюсь…")
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    try:
        await ctx.pool.stop()
        await userbot._disconnect()
        await bot.session.close()
    finally:
        db.close()


def main() -> None:
    try:
        asyncio.run(amain())
    except ConfigError as exc:
        print(f"Ошибка настройки: {exc}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
