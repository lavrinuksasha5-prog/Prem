import asyncio
import json
import os
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from storage import TEMP_DIR, get_account, import_zip_archive, init_storage, list_accounts, set_proxy_pool

BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬ_ТОКЕН_БОТА")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))  # Укажите ваш Telegram user_id для защиты

dp = Dispatcher()


def is_allowed(user_id: int) -> bool:
    return ADMIN_ID == 0 or user_id == ADMIN_ID


@dp.message(CommandStart())
async def cmd_start(message: Message):
    if not is_allowed(message.from_user.id):
        return
    await message.answer(
        "Панель готова:\n"
        "1. Кинь сюда <b>.zip</b> с парами <code>имя.session</code> + <code>имя.json</code> (от 1 шт).\n"
        "2. Отправь <code>/proxies</code> и с новой строки список прокси (распределятся по всем аккам).\n"
        "3. Нажми <code>/accs</code> — появится кнопка запуска в Web.",
        parse_mode="HTML",
    )


@dp.message(Command("proxies"))
async def cmd_proxies(message: Message):
    if not is_allowed(message.from_user.id):
        return
    lines = message.text.splitlines()[1:]
    if not lines:
        await message.answer("Формат:\n<code>/proxies\nhttp://user:pass@host:port\nhttp://user2:pass2@host2:port</code>", parse_mode="HTML")
        return
    count = set_proxy_pool(lines)
    await message.answer(f"Пул обновлен: <b>{count}</b> прокси распределены по аккаунтам.", parse_mode="HTML")


@dp.message(Command("accs"))
async def cmd_accs(message: Message):
    if not is_allowed(message.from_user.id):
        return
    rows = list_accounts(limit=20)
    if not rows:
        await message.answer("Аккаунтов пока нет. Загрузи .zip архив с session + json.")
        return

    for acc_id, name, proxy, fp_raw in rows:
        fp = json.loads(fp_raw)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=f"🌐 Open Web ({name})", callback_data=f"open_web:{acc_id}")]
            ]
        )
        await message.answer(
            f"<b>#{acc_id} {name}</b>\n"
            f"Proxy: <code>{proxy or 'не задан'}</code>\n"
            f"FP: <code>{fp['platform']} | {fp['timezone']} | {fp['viewport']['width']}x{fp['viewport']['height']}</code>",
            reply_markup=kb,
            parse_mode="HTML",
        )


@dp.message(F.document)
async def handle_doc(message: Message, bot: Bot):
    if not is_allowed(message.from_user.id):
        return
    doc = message.document
    if not doc.file_name or not doc.file_name.lower().endswith(".zip"):
        await message.answer("Нужен архив в формате <b>.zip</b> с парами .session и .json.", parse_mode="HTML")
        return

    zip_path = TEMP_DIR / f"upload_{message.from_user.id}_{doc.file_name}"
    await bot.download(doc, destination=zip_path)

    result = import_zip_archive(zip_path)
    added_cnt = len(result["added"])
    skipped_preview = "\n".join(result["skipped"][:10])
    msg = f"Импорт завершен.\nДобавлено: <b>{added_cnt}</b>"
    if result["skipped"]:
        msg += f"\nПропущено ({len(result['skipped'])}):\n<code>{skipped_preview}</code>"
    await message.answer(msg, parse_mode="HTML")


@dp.callback_query(F.data.startswith("open_web:"))
async def cb_open_web(callback: CallbackQuery):
    if not is_allowed(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    acc_id = int(callback.data.split(":", 1)[1])
    acc = get_account(acc_id)
    if not acc:
        await callback.answer("Аккаунт не найден", show_alert=True)
        return

    _, name, sess_path, json_path, proxy, fp_raw = acc
    fp = json.loads(fp_raw)

    # Здесь ставится вызов вашего локального агента / очереди воркера запуска Playwright
    await callback.answer(f"Задача на запуск {name} отправлена")
    await callback.message.answer(
        f"🚀 Запуск <b>{name}</b> в Web:\n"
        f"• Session: <code>{sess_path}</code>\n"
        f"• Proxy: <code>{proxy or 'без прокси'}</code>\n"
        f"• TZ / Locale: <code>{fp['timezone']} / {fp['locale']}</code>\n"
        f"• UA: <code>{fp['user_agent'][:55]}...</code>",
        parse_mode="HTML",
    )


async def main():
    init_storage()
    bot = Bot(token=BOT_TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
