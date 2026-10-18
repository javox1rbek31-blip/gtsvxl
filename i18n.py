"""Язык панели: русский (по умолчанию) и узбекский.

Как устроен перевод:
* app/i18n_uz.py (PHRASES) — все неизменные части строк панели (то, что в f-строках между {…}) и их перевод.
  tr() находит эти части в готовом тексте и заменяет; длинные — первыми, чтобы «⏸ Пауза» не съела «⏸ Пауза после…».
* Данные (названия папок и чатов, имена, текст Push) экраны оборачивают невидимой меткой DATA через h():
  внутри меток ничего не переводится, а сами метки tr() всегда убирает — и в русском режиме тоже.
* Тексты в группу «Проёб» и клиентам через tr() не проходят и остаются как есть.
"""
from __future__ import annotations

import re

from .i18n_uz import PHRASES

DATA = "⁣"                       # невидимый разделитель: граница пользовательских данных

# Уведомления владельцу в личку и тексты ошибок из модулей вне панели.
NOTICES = {
    "⚠️ Не удаётся отправить уведомление в группу «Проёб»: ": "⚠️ «Проёб» guruhiga xabar yuborib bo'lmadi: ",
    "\nПроверьте, что бот добавлен в группу и ID группы указан верно.":
        "\nBot guruhga qo'shilganini va guruh ID si to'g'ri ekanini tekshiring.",
    "⚠️ Не удаётся проверить кружки ставки нового лида (топик удалён или у аккаунта нет доступа): ":
        "⚠️ Yangi lid stavkasining dumaloq videolarini tekshirib bo'lmadi (topik o'chirilgan yoki akkauntda ruxsat yo'q): ",
    "⚠️ У новых лидов не пришли кружки ставки (": "⚠️ Yangi lidlarda stavka dumaloq videolari kelmadi (",
    "), а в «Проёб» написать не удалось.\n": "), «Проёб»ga yozib bo'lmadi.\n",
    "⚠️ Юзербот не в сети более 3 минут (состояние: ": "⚠️ Asosiy akkaunt 3 daqiqadan beri tarmoqda emas (holati: ",
    "). Бот не видит новые сообщения. Проверьте «🔑 Аккаунт юзербота».":
        "). Bot yangi xabarlarni ko'rmayapti. «🔑 Asosiy akkaunt» ni tekshiring.",
    "✅ Юзербот снова онлайн.": "✅ Asosiy akkaunt yana onlayn.",
    "✅ Юзербот снова онлайн (": "✅ Asosiy akkaunt yana onlayn (",
    "🩺 Давно нет событий от Telegram (90+ мин в рабочее время). Если в чатах есть активность — "
    "нажмите «🔑 Аккаунт юзербота → 🔄 Обновить».":
        "🩺 Telegram'dan uzoq vaqt hodisa yo'q (ish vaqtida 90+ daqiqa). Chatlarda faollik bo'lsa — "
        "«🔑 Asosiy akkaunt → 🔄 Yangilash» ni bosing.",
    "⚠️ Юзербот отключился: ": "⚠️ Asosiy akkaunt uzildi: ",
    ". Переподключаюсь автоматически.": ". Avtomatik qayta ulanyapman.",
    "🔑 Сессия юзербота недействительна. Откройте Меню → Аккаунты → Основной аккаунт и подключите аккаунт заново.":
        "🔑 Asosiy akkaunt seansi yaroqsiz. Menyu → Akkauntlar → Asosiy akkaunt ni oching va akkauntni qayta ulang.",
    "🔑 Аккаунт Push «": "🔑 Push akkaunti «",
    "» разлогинен. Меню → Аккаунты → удалите этот аккаунт и добавьте заново.":
        "» tizimdan chiqib ketgan. Menyu → Akkauntlar → bu akkauntni o'chirib, qaytadan qo'shing.",
    "⚠️ Модуль «": "⚠️ «",
    "» упал (": "» moduli to'xtadi (",
    ") и перезапускается автоматически.": ") va avtomatik qayta ishga tushyapti.",
    "бот (Telegram API)": "bot (Telegram API)", "юзербот": "asosiy akkaunt", "мониторинг": "monitoring",
    "✅ Бот запущен. Откройте /menu.": "✅ Bot ishga tushdi. /menu ni oching.",
    "\n⚠️ Рассылка была прервана перезапуском: ": "\n⚠️ Push qayta ishga tushish sababli uzildi: ",
    "💾 Резервная копия настроек (без текста сообщений и без сессии аккаунта).":
        "💾 Sozlamalar zaxira nusxasi (xabar matnisiz va akkaunt seansisiz).",
    # команды бота (меню «/»)
    "🏠 Главное меню": "🏠 Asosiy menyu",
    # поиск чата (тип чата приходит из юзербота)
    "(без названия)": "(nomsiz)", "канал": "kanal", "группа": "guruh", "личный чат": "shaxsiy chat",
    # ошибки из других модулей, которые панель показывает как есть (str(exc))
    "Ожидается время в формате ЧЧ:ММ, например 10:00": "Vaqt SS:DD ko'rinishida bo'lsin, masalan 10:00",
    "Часы 0–23, минуты 0–59": "Soat 0–23, daqiqa 0–59",
    "Ожидается дата, например 26.08.2026": "Sana kerak, masalan 26.08.2026",
    "Юзербот не подключён": "Asosiy akkaunt ulanmagan",
    "нет ответа от Telegram": "Telegram javob bermadi",
    "Файл вложения не найден — загрузите медиа заново": "Biriktirma fayli topilmadi — mediani qayta yuklang",
    "Telegram ограничил рассылку с аккаунта (PeerFlood)": "Telegram akkauntdan yuborishni chekladi (PeerFlood)",
    "не удалось отправить": "yuborib bo'lmadi",
    "ID группы «Проёб» не задан (Меню → Группа «Проёб»)": "«Проёб» guruhi ID si belgilanmagan (Menyu → «Проёб» guruhi)",
}

_TABLE = {**PHRASES, **NOTICES}
_LETTER = r"[^\W\d_]"


def _pattern(key: str) -> str:
    """Ключ, начинающийся/кончающийся буквой, не должен цеплять кусок другого слова («мин» ≠ «минут»)."""
    p = re.escape(key)
    if re.match(_LETTER, key):
        p = rf"(?<!{_LETTER})" + p
    if re.search(_LETTER + r"$", key):
        p += rf"(?!{_LETTER})"
    return p


_RE = re.compile("|".join(_pattern(k) for k in sorted(_TABLE, key=len, reverse=True)))


def mark(text: str) -> str:
    """Пометить пользовательские данные: tr() их не трогает (своя метка внутри данных убирается — не собьёт разметку)."""
    return f"{DATA}{text.replace(DATA, '')}{DATA}"


def strip(text: str) -> str:
    return text.replace(DATA, "")


def tr(text: str, lang: str | None) -> str:
    """Перевести готовый текст панели. Русский режим — только убрать метки данных."""
    if not text:
        return text
    if lang != "uz":
        return strip(text)
    parts = text.split(DATA)
    # чётные куски — текст панели, нечётные — данные между метками
    return "".join(_RE.sub(lambda m: _TABLE[m.group(0)], p) if i % 2 == 0 else p for i, p in enumerate(parts))
