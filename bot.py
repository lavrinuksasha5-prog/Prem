import asyncio
import html
import json
import os

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from browser_launcher import launch_web_telegram
from storage import (
    TEMP_DIR,
    count_accounts,
    get_account,
    import_zip_archive,
    init_storage,
    list_accounts_page,
    set_proxy_pool,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬ_ТОКЕН_БОТА")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
PAGE_SIZE = 6

dp = Dispatcher()


def is_allowed(user_id: int) -> bool:
    return ADMIN_ID == 0 or user_id == ADMIN_ID


async def _send_accounts_page(message: Message, offset: int = 0, edit: bool = False):
    total = count_accounts()
    if total == 0:
        text = "Аккаунтов пока нет. Отправь <b>.zip</b> архив с парами <code>имя.session</code> и <code>имя.json</code>."
        if edit:
            await message.edit_text(text, parse_mode="HTML")
        else:
            await message.answer(text, parse_mode="HTML")
        return

    offset = max(0, min(offset, ((total - 1) // PAGE_SIZE) * PAGE_SIZE))
    rows = list_accounts_page(offset=offset, limit=PAGE_SIZE)

    page_num = offset // PAGE_SIZE + 1
    total_pages = (total - 1) // PAGE_SIZE + 1

    lines = [f"<b>Аккаунты (стр. {page_num}/{total_pages}, всего {total}):</b>\n"]
    acc_buttons = []

    for acc_id, name, proxy, fp_raw, two_fa in rows:
        fp = json.loads(fp_raw)
        has_2fa = "да" if two_fa else "нет"
        safe_name = html.escape(str(name))
        safe_proxy = html.escape(str(proxy or "нет"))
        lines.append(
            f"• <b>#{acc_id} {safe_name}</b> | 2FA: <code>{has_2fa}</code>\n"
            f"  Proxy: <code>{safe_proxy}</code>\n"
            f"  FP: <code>{html.escape(str(fp['platform']))} | {html.escape(str(fp['timezone']))} | {fp['viewport']['width']}x{fp['viewport']['height']}</code>"
        )
        acc_buttons.append(
            [InlineKeyboardButton(text=f"🌐 Open Web — {name}", callback_data=f"open_web:{acc_id}:{offset}")]
        )

    nav_row = []
    if offset > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"page:{max(0, offset - PAGE_SIZE)}"))
    if offset + PAGE_SIZE < total:
        nav_row.append(InlineKeyboardButton(text="Вперед ➡️", callback_data=f"page:{offset + PAGE_SIZE}"))
    if nav_row:
        acc_buttons.append(nav_row)

    kb = InlineKeyboardMarkup(inline_keyboard=acc_buttons)
    text = "\n".join(lines)

    if edit:
        await message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=kb, parse_mode="HTML")


@dp.message(CommandStart())
async def cmd_start(message: Message):
    if not is_allowed(message.from_user.id):
        return
    await message.answer(
        "<b>Панель готова:</b>\n"
        "1. Кинь <b>.zip</b> с парами <code>имя.session</code> + <code>имя.json</code> (поиск по всем папкам).\n"
        "2. Залей прокси через <code>/proxies</code> (можно 1 прокси на 10+ акков).\n"
        "3. Открой <code>/accs</code> и жми <b>Open Web</b>.",
        parse_mode="HTML",
    )


@dp.message(Command("proxies"))
async def cmd_proxies(message: Message):
    if not is_allowed(message.from_user.id):
        return
    lines = message.text.splitlines()[1:]
    if not lines:
        await message.answer(
            "Формат:\n<code>/proxies\nhttp://user:pass@host:port</code>",
            parse_mode="HTML",
        )
        return
    cnt = set_proxy_pool(lines)
    await message.answer(f"Пул обновлен: <b>{cnt}</b> прокси распределены по аккаунтам.", parse_mode="HTML")


@dp.message(Command("accs"))
async def cmd_accs(message: Message):
    if not is_allowed(message.from_user.id):
        return
    await _send_accounts_page(message, offset=0, edit=False)


@dp.callback_query(F.data.startswith("page:"))
async def cb_page(callback: CallbackQuery):
    if not is_allowed(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    try:
        offset = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректная страница", show_alert=True)
        return
    await _send_accounts_page(callback.message, offset=offset, edit=True)
    await callback.answer()


@dp.message(F.document)
async def handle_zip(message: Message, bot: Bot):
    if not is_allowed(message.from_user.id):
        return
    doc = message.document
    if not doc.file_name or not doc.file_name.lower().endswith(".zip"):
        await message.answer("Нужен архив в формате <b>.zip</b>.", parse_mode="HTML")
        return

    zip_path = TEMP_DIR / f"upload_{message.from_user.id}_{doc.file_name}"
    await bot.download(doc, destination=zip_path)

    try:
        res = import_zip_archive(zip_path)
    except Exception as exc:
        await message.answer(
            f"Ошибка обработки архива: <code>{html.escape(str(exc))}</code>",
            parse_mode="HTML",
        )
        return

    msg = (
        f"<b>Импорт завершен:</b>\n"
        f"• Добавлено: <b>{len(res['added'])}</b>\n"
        f"• Обновлено: <b>{len(res['updated'])}</b>\n"
        f"• Пропущено: <b>{len(res['skipped'])}</b>"
    )
    if res["skipped"]:
        preview = "\n".join(res["skipped"][:10])
        msg += f"\n\nПричины пропуска:\n<code>{html.escape(preview)}</code>"
    await message.answer(msg, parse_mode="HTML")


@dp.callback_query(F.data.startswith("open_web:"))
async def cb_open_web(callback: CallbackQuery):
    if not is_allowed(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    parts = callback.data.split(":")
    try:
        acc_id = int(parts[1])
    except (IndexError, ValueError):
        await callback.answer("Некорректный ID аккаунта", show_alert=True)
        return

    acc = get_account(acc_id)
    if not acc:
        await callback.answer("Аккаунт не найден", show_alert=True)
        return

    _, name, sess_path, json_path, two_fa, proxy, fp_raw = acc
    fp = json.loads(fp_raw)
    safe_name = html.escape(str(name))
    safe_proxy = html.escape(str(proxy or "без прокси"))

    await callback.answer(f"Запускаю {name}...")
    try:
        novnc_url, injected_ok = await launch_web_telegram(
            acc_name=name,
            raw_proxy=proxy,
            fp=fp,
            session_path=sess_path,
            json_path=json_path,
        )
        inj_status = "впрыснута в профиль" if injected_ok else "не найдена / требуется ручной вход"
        two_fa_note = "\n• 2FA пароль: зашифрован в БД" if two_fa else ""

        markup = None
        if novnc_url:
            markup = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="📱 Открыть в браузере телефона", url=novnc_url)]]
            )

        await callback.message.answer(
            f"✅ Открыт профиль <b>{safe_name}</b>:\n"
            f"• Сессия: <code>{inj_status}</code>\n"
            f"• Proxy: <code>{safe_proxy}</code>\n"
            f"• FP: <code>{html.escape(str(fp['platform']))} | {fp['viewport']['width']}x{fp['viewport']['height']}</code>\n"
            f"• TZ / Locale: <code>{html.escape(str(fp['timezone']))} / {html.escape(str(fp['locale']))}</code>"
            f"{two_fa_note}",
            reply_markup=markup,
            parse_mode="HTML",
        )
    except Exception as exc:
        await callback.message.answer(
            f"❌ Ошибка запуска <b>{safe_name}</b>: <code>{html.escape(str(exc))}</code>",
            parse_mode="HTML",
        )


async def main():
    init_storage()
    bot = Bot(token=BOT_TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
