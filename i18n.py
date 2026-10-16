"""Язык панели: русский (по умолчанию) и узбекский. Переводится заголовки, кнопки и основные подписи;
всё, чего нет в словаре, остаётся по-русски. Тексты сообщений в «Проёб» (задержки) не переводятся."""
from __future__ import annotations

UZ = {
    # главное меню и разделы
    "Панель управления": "Boshqaruv paneli",
    "Бот:": "Bot:", "ВКЛЮЧЁН": "YOQILGAN", "ВЫКЛЮЧЕН": "O'CHIRILGAN",
    "Юзербот:": "Asosiy akkaunt:", "Группа «Проёб»": "«Проёб» guruhi", "не задана": "belgilanmagan",
    "Задержки:": "Zaderjkalar:", "лимит": "limit", "мин": "daq", "рабочий день": "ish kuni", "Папок:": "Papkalar:",
    "📣 Push (горячие лиды)": "📣 Push (issiq lidlar)", "📣 Push": "📣 Push",
    "🚨 Проёб (задержки)": "🚨 Проёб (zaderjka)", "📲 Аккаунты": "📲 Akkauntlar",
    "📅 Лиды": "📅 Lidlar", "📈 Отчёты и рейтинг": "📈 Hisobot va reyting", "📈 Отчёты": "📈 Hisobotlar",
    "🎛 Функции и выключатели": "🎛 Funksiyalar", "🎛 Функции": "🎛 Funksiyalar",
    "⚙️ Ещё настройки": "⚙️ Qo'shimcha sozlamalar", "📊 Статус": "📊 Holat", "👮 Админы и права": "👮 Adminlar",
    "📖 Помощь": "📖 Yordam", "🌐 Язык: Русский": "🌐 Til: Ruscha", "🌐 Язык: Узбекский": "🌐 Til: O'zbekcha",
    "⬅️ Назад": "⬅️ Orqaga", "⬅️ К папкам": "⬅️ Papkalarga", "🏠 Меню": "🏠 Menyu",
    "📁 Папки и сотрудники": "📁 Papkalar va xodimlar", "⏱ Лимит ответа": "⏱ Javob limiti",
    "🕒 Рабочий день": "🕒 Ish kuni", "👥 Группа «Проёб»": "👥 «Проёб» guruhi",
    "🔎 Найти чат / ID": "🔎 Chat / ID topish", "🔑 Аккаунт юзербота": "🔑 Asosiy akkaunt",
    # Push
    "Push — горячие лиды": "Push — issiq lidlar", "🚀 Разослать сейчас": "🚀 Pushni boshlash",
    "✏️ Текст": "✏️ Matn", "📎 Медиа": "📎 Media", "🗑 Убрать медиа": "🗑 Mediani olib tashlash",
    "🧪 Тест: только мне": "🧪 Test: faqat o'zimga", "📋 Шаблоны": "📋 Shablonlar",
    "🔢 Количество": "🔢 Soni", "📅 Период лидов": "📅 Lidlar davri", "📨 Способ": "📨 Usul",
    "⚡ Скорость": "⚡ Tezlik", "⏸ Пауза после сообщения": "⏸ Yozishdan keyin pauza",
    "🔁 Повтор Push": "🔁 Push takrori", "🆕 Новые лиды: пауза": "🆕 Yangi lid: pauza",
    "🕒 Режим: все / день / вечер": "🕒 Rejim: hammasi / kun / kech", "🏷 Эмодзи для Push": "🏷 Push emoji",
    "🚫 Исключённые эмодзи": "🚫 Chiqarilgan emoji", "🧾 Журнал рассылок": "🧾 Push jurnali",
    "⚙️ Настройки Push": "⚙️ Push sozlamalari", "📁 Папки и аккаунты Push": "📁 Papka va Push akkauntlar",
    "Будет отправлено": "Yuboriladi", "Папка": "Papka", "аккаунт": "akkaunt", "не назначен": "biriktirilmagan",
    "✅ Начать": "✅ Boshlash", "❌ Отмена": "❌ Bekor", "🚀 Подтвердите Push": "🚀 Pushni tasdiqlang",
    "⏹ Остановить текущую рассылку": "⏹ Pushni to'xtatish", "⏹ Остановить": "⏹ To'xtatish",
    "Получателей": "Qabul qiluvchilar", "Текст:": "Matn:", "Вложение": "Biriktirma", "нет": "yo'q",
    # аккаунты
    "Аккаунты": "Akkauntlar", "➕ Добавить аккаунт": "➕ Akkaunt qo'shish",
    "⏸ Пауза": "⏸ Pauza", "▶️ Возобновить": "▶️ Davom ettirish", "🗑 Удалить": "🗑 O'chirish",
    "✏️ Переименовать": "✏️ Nomini o'zgartirish", "🚫 Убрать аккаунт": "🚫 Akkauntni olib tashlash",
    "на паузе": "pauzada", "🟢 подключён": "🟢 ulangan", "🟡 подключается…": "🟡 ulanmoqda…",
    "🔴 нет связи, переподключаюсь": "🔴 aloqa yo'q, qayta ulanmoqda", "🔴 нужен повторный вход": "🔴 qayta kirish kerak",
    "⚪ аккаунт не подключён": "⚪ akkaunt ulanmagan", "⏸ на паузе": "⏸ pauzada",
    "основной (следит за задержками)": "asosiy (zaderjkani kuzatadi)",
    # папка
    "Сотрудники:": "Xodimlar:", "Чатов под контролем:": "Nazoratdagi chatlar:", "Аккаунт Push:": "Push akkaunti:",
    "Штраф за задержку:": "Zaderjka jarimasi:", "не задан": "belgilanmagan",
    "👤➕ Добавить сотрудника": "👤➕ Xodim qo'shish", "💬 Чаты папки": "💬 Papka chatlari",
    "💰 Штраф за задержку": "💰 Zaderjka jarimasi", "➕ Добавить папку": "➕ Papka qo'shish",
    "🔄 Подтянуть чаты из Telegram": "🔄 Telegramdan chatlarni olish",
    "Контроль задержек": "Zaderjka nazorati", "✅ вкл": "✅ yoq", "⛔ выкл": "⛔ o'ch",
    "Лиды": "Lidlar", "Отчёты": "Hisobotlar", "Помощь": "Yordam",
    "Подождите": "Kuting", "Недостаточно прав для этого действия.": "Bu amal uchun huquq yetarli emas.",
    "Что-то пошло не так, попробуйте ещё раз.": "Xatolik bo'ldi, qayta urinib ko'ring.",
}
import re

# целые слова/фразы: «мин» не заденет «минут», «Папка» — «Папками»
_RE = re.compile("|".join(r"(?<!\w)" + re.escape(k) + r"(?!\w)" for k in sorted(UZ, key=len, reverse=True)))


def tr(text: str, lang: str) -> str:
    if lang != "uz" or not text:
        return text
    return _RE.sub(lambda m: UZ[m.group(0)], text)
