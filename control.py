"""
Обработчик команд Telegram для настройки фильтров бота поиска жилья.

Запускается по расписанию (workflow bot-control.yml, раз в ~10 минут).
Читает новые сообщения боту через getUpdates, применяет команды к config.json
и отвечает пользователю. Property24 НЕ трогает — только Telegram API (бесплатно).

Команды (см. HELP): /статус /цена /комнаты /санузлы /парковка /мебель /срок /сброс
"""

import json
import os
import re
from pathlib import Path

import requests

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
# В TELEGRAM_CHAT_ID может быть несколько получателей через запятую.
# ПЕРВЫЙ — владелец (админ): только он может менять фильтры.
# Остальные (например, сотрудник) получают объявления и могут смотреть
# настройки через /статус, но изменить их не могут — иначе любой из них
# незаметно перенастроил бы поиск владельцу.
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
CHAT_IDS = [c for c in re.split(r"[,;\s]+", TELEGRAM_CHAT_ID) if c]
ADMIN_CHAT_ID = CHAT_IDS[0] if CHAT_IDS else ""

# Команды, доступные всем получателям, а не только владельцу.
READONLY_COMMANDS = {"статус", "status", "помощь", "help", "start", "приложение", "app"}

HERE = Path(__file__).parent
CONFIG_FILE = HERE / "config.json"
STATE_FILE = HERE / "tg_state.json"

API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# Мини-апп (webapp/index.html) раздаётся с GitHub Pages этого же репозитория.
# Telegram открывает ТОЛЬКО https — локальный файл или
# http-адрес он молча проигнорирует.
WEBAPP_URL = os.environ.get(
    "WEBAPP_URL", "https://o2385214-sketch.github.io/findAhous/webapp/")

DEFAULTS = {
    "min_price": 0,
    "max_price": 20000,
    "min_bedrooms": 1, "max_bedrooms": 2,
    "min_bathrooms": 1, "max_bathrooms": 2,
    "min_parking": 1, "max_parking": 2,
    "require_parking": True,
    "furnished_only": True,
    "lease_months": 12,
}

HELP = (
    "🛠 <b>Команды настройки поиска</b>\n\n"
    "/статус — показать текущие настройки\n"
    "/цена 20000 — потолок цены (ZAR/мес)\n"
    "/цена 8000 20000 — диапазон цены (от и до)\n"
    "/комнаты 1 3 — спальни: от и до (можно одно число)\n"
    "/санузлы 1 2 — санузлы: от и до\n"
    "/парковка вкл — требовать парковку (1–2)\n"
    "/парковка выкл — не требовать парковку\n"
    "/парковка 1 2 — парковка: от и до\n"
    "/мебель вкл — только меблированные\n"
    "/мебель выкл — любые\n"
    "/срок 12 — срок аренды, мес. (0 — любой)\n"
    "/сброс — вернуть настройки по умолчанию\n"
    "/приложение — открыть мини-апп: карточки квартир с фото "
    "и те же фильтры кнопками\n\n"
    "💡 Можно слать несколько команд одним сообщением — каждую с новой строки:\n"
    "/цена 25000\n/комнаты 1 3\n/парковка выкл"
)


NOT_OWNER = ("🔒 Менять настройки поиска может только владелец.")

APP_INTRO = (
    "<b>Мини-апп готов</b>\n"
    "Кнопка «Поиск жилья» внизу экрана откроет витрину "
    "с фото и фильтры.\n"
    "Если кнопки не видно — нажмите значок клавиатуры справа от поля ввода."
)


def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return json.loads(json.dumps(default))  # глубокая копия


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_config():
    cfg = dict(DEFAULTS)
    cfg.update(load_json(CONFIG_FILE, {}))
    return cfg


def send(text, chat_id=None, reply_markup=None):
    """Отвечаем ТОМУ, кто написал (chat_id), а не всегда владельцу — иначе
    сотрудник шлёт /статус, а ответ уходит владельцу, и оба в недоумении."""
    to = chat_id or ADMIN_CHAT_ID
    if not TELEGRAM_TOKEN or not to:
        print("нет TELEGRAM_TOKEN / TELEGRAM_CHAT_ID — не отвечаю")
        return
    payload = {"chat_id": to, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        requests.post(f"{API}/sendMessage", data=payload, timeout=15)
    except requests.RequestException as e:
        print("sendMessage error:", e)


def webapp_keyboard():
    """Кнопка запуска мини-аппа. Именно reply-клавиатура (нижняя), а не inline:
    Telegram разрешает мини-аппу слать данные боту (sendData) ТОЛЬКО когда тот
    открыт отсюда. Из inline-кнопки фильтры бы не сохранялись — открылась бы
    красивая страница, а кнопка «Сохранить» молча ничего не делала."""
    return {
        "keyboard": [[{"text": "🏠 Поиск жилья",
                       "web_app": {"url": WEBAPP_URL}}]],
        "resize_keyboard": True,
        "is_persistent": True,
    }


# Границы «здравого смысла» для значений из мини-аппа.
WEBAPP_LIMITS = {
    "min_price": (0, 500000), "max_price": (0, 500000),
    "min_bedrooms": (0, 9), "max_bedrooms": (0, 9),
    "min_bathrooms": (0, 9), "max_bathrooms": (0, 9),
    "min_parking": (0, 9), "max_parking": (0, 9),
    "lease_months": (0, 60),
}

PAIRS = (("min_price", "max_price"), ("min_bedrooms", "max_bedrooms"),
         ("min_bathrooms", "max_bathrooms"), ("min_parking", "max_parking"))


def apply_webapp(raw, cfg):
    """Фильтры, присланные мини-аппом через sendData.

    Страница выполняется на телефоне пользователя, и её содержимое при желании
    подменяется — поэтому ничему из неё не верим на слово: каждое число
    зажимаем в допустимый диапазон, неизвестные ключи выбрасываем."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return False, "Не разобрал данные из мини-аппа — настройки не менял."
    if not isinstance(data, dict) or data.get("t") != "filters":
        return False, "Не разобрал данные из мини-аппа — настройки не менял."

    for key, (lo, hi) in WEBAPP_LIMITS.items():
        if key not in data:
            continue
        try:
            cfg[key] = max(lo, min(hi, int(data[key])))
        except (TypeError, ValueError):
            pass
    for key in ("require_parking", "furnished_only"):
        if key in data:
            cfg[key] = bool(data[key])
    for lo_k, hi_k in PAIRS:   # границы могли приехать перевёрнутыми
        if cfg[lo_k] > cfg[hi_k]:
            cfg[lo_k], cfg[hi_k] = cfg[hi_k], cfg[lo_k]

    return True, "✅ Фильтры из мини-аппа сохранены.\n\n" + status_text(cfg)


def status_text(cfg):
    hi = f"{cfg['max_price']:,}".replace(",", " ")
    lo = cfg.get("min_price", 0)
    price = f"R{f'{lo:,}'.replace(',', ' ')}–{hi}/мес" if lo else f"до R{hi}/мес"
    park = f"{cfg['min_parking']}–{cfg['max_parking']}" if cfg["require_parking"] else "не важно"
    lease = f"{cfg['lease_months']} мес." if cfg["lease_months"] else "любой"
    return (
        "⚙️ <b>Текущие настройки поиска</b>\n"
        f"💰 Цена: {price}\n"
        f"🛏 Комнаты: {cfg['min_bedrooms']}–{cfg['max_bedrooms']}\n"
        f"🚿 Санузлы: {cfg['min_bathrooms']}–{cfg['max_bathrooms']}\n"
        f"🅿 Парковка: {park}\n"
        f"🛋 Только меблированные: {'да' if cfg['furnished_only'] else 'нет'}\n"
        f"📅 Срок аренды: {lease}\n\n"
        "Изменить — /помощь"
    )


def parse_range(args, cur_min, cur_max):
    """1–2 числа -> (min, max). '1 3' -> (1,3); '2' -> (2,2); пусто -> без изменений."""
    nums = [int(a) for a in args if a.lstrip("-").isdigit()]
    if len(nums) >= 2:
        return min(nums[0], nums[1]), max(nums[0], nums[1])
    if len(nums) == 1:
        return nums[0], nums[0]
    return cur_min, cur_max


def apply_command(text, cfg):
    """Обрабатывает ОДНУ команду, меняя cfg на месте.
    Возвращает (changed, reply): changed — изменён ли config;
    reply — отдельный текст для показа (статус/помощь/ошибка) или None
    (при обычном успешном изменении итоговый статус шлём один раз в main)."""
    parts = text.strip().split()
    if not parts:
        return False, None
    cmd = parts[0].lower().lstrip("/").split("@")[0]
    args = parts[1:]
    on = {"вкл", "on", "да", "1"}
    off = {"выкл", "off", "нет", "0"}

    if cmd in ("start", "помощь", "help"):
        return False, HELP
    if cmd in ("статус", "status"):
        return False, status_text(cfg)

    if cmd in ("цена", "price"):
        nums = [int(a) for a in args if a.isdigit()]
        if len(nums) >= 2:
            cfg["min_price"], cfg["max_price"] = min(nums[0], nums[1]), max(nums[0], nums[1])
        elif len(nums) == 1:
            cfg["max_price"] = nums[0]  # одно число — только потолок, нижнюю границу не трогаем
        else:
            return False, "Формат: /цена 20000  или  /цена 8000 20000"
    elif cmd in ("комнаты", "bedrooms", "bed"):
        cfg["min_bedrooms"], cfg["max_bedrooms"] = parse_range(args, cfg["min_bedrooms"], cfg["max_bedrooms"])
    elif cmd in ("санузлы", "bathrooms", "bath"):
        cfg["min_bathrooms"], cfg["max_bathrooms"] = parse_range(args, cfg["min_bathrooms"], cfg["max_bathrooms"])
    elif cmd in ("парковка", "parking"):
        if args and args[0].lower() in off:
            cfg["require_parking"] = False
        elif args and args[0].lower() in on:
            cfg["require_parking"] = True
        elif args:
            cfg["require_parking"] = True
            cfg["min_parking"], cfg["max_parking"] = parse_range(args, cfg["min_parking"], cfg["max_parking"])
        else:
            return False, "Формат: /парковка вкл | выкл | 1 2"
    elif cmd in ("мебель", "furnished"):
        if args and args[0].lower() in off:
            cfg["furnished_only"] = False
        elif args and args[0].lower() in on:
            cfg["furnished_only"] = True
        else:
            return False, "Формат: /мебель вкл  или  /мебель выкл"
    elif cmd in ("срок", "lease"):
        if args and args[0].isdigit():
            cfg["lease_months"] = int(args[0])
        else:
            return False, "Формат: /срок 12  (0 — любой)"
    elif cmd in ("сброс", "reset"):
        cfg.clear()
        cfg.update(DEFAULTS)
    else:
        return False, f"Не понял команду «{parts[0]}». Список: /помощь"

    return True, None


def note_unknown(chat_id, msg, state):
    """Боту написал кто-то, кого нет в списке. Один раз сообщаем владельцу его
    имя и chat_id — так подключить сотрудника можно, не выковыривая id вручную
    из getUpdates. Повторно про того же человека не пишем."""
    seen = state.setdefault("unknown_chats", [])
    if chat_id in seen:
        return
    seen.append(chat_id)
    who = msg.get("from") or {}
    name = " ".join(x for x in (who.get("first_name"), who.get("last_name")) if x) or "без имени"
    uname = "@" + who["username"] if who.get("username") else "username не указан"
    send(
        "👤 <b>Боту написал новый человек</b>\n"
        f"{name} ({uname})\n"
        f"chat_id: <code>{chat_id}</code>\n\n"
        "Если это твой сотрудник — скажи, и я подключу его к рассылке объявлений.",
        ADMIN_CHAT_ID,
    )


def main():
    if not TELEGRAM_TOKEN:
        print("нет TELEGRAM_TOKEN — выход")
        return

    state = load_json(STATE_FILE, {"offset": 0})
    offset = state.get("offset", 0)
    cfg = get_config()

    try:
        r = requests.get(
            f"{API}/getUpdates",
            params={"offset": offset, "timeout": 10, "allowed_updates": '["message"]'},
            timeout=30,
        )
        updates = r.json().get("result", [])
    except (requests.RequestException, ValueError) as e:
        print("getUpdates error:", e)
        return

    changed = False
    for upd in updates:
        offset = upd["update_id"] + 1
        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            continue
        chat_id = str(msg.get("chat", {}).get("id", ""))
        # Мини-апп шлёт настройки не текстом, а отдельным полем
        # web_app_data — обычная проверка на "/" его бы не пропустила.
        webapp = msg.get("web_app_data")
        text = msg.get("text", "") or ""
        if not webapp and "/" not in text:
            continue
        if CHAT_IDS and chat_id not in CHAT_IDS:
            print(f"игнор команды из чужого чата {chat_id}")
            note_unknown(chat_id, msg, state)
            continue
        is_admin = chat_id == ADMIN_CHAT_ID

        if webapp:
            if not is_admin:
                send(NOT_OWNER, chat_id)
                continue
            ok, reply = apply_webapp(webapp.get("data", ""), cfg)
            changed = changed or ok
            send(reply, chat_id)
            continue

        # одно сообщение может содержать несколько команд (по строкам или подряд):
        # "/цена 25000 /комнаты 1 3" -> ["/цена 25000 ", "/комнаты 1 3"]
        commands = re.findall(r"/[^/]+", text)
        replies = []
        msg_changed = False
        denied = False
        want_app = False
        for c in commands:
            head = c.strip().split()
            name = head[0].lower().lstrip("/").split("@")[0] if head else ""
            if not is_admin and name not in READONLY_COMMANDS:
                denied = True
                continue
            # /start новичку тоже показываем кнопкой, а не только текстом
            if name in ("приложение", "app", "start"):
                want_app = True
            if name in ("приложение", "app"):
                continue   # своего текста у команды нет — только клавиатура
            ch, rep = apply_command(c, cfg)
            if ch:
                msg_changed = True
            if rep:
                replies.append(rep)
        if denied:
            replies.append("🔒 Менять настройки поиска может только владелец.\n"
                           "Вам доступны /статус и /помощь.")
        if msg_changed:
            changed = True
            replies.append("✅ Готово, применю при следующем поиске.\n\n" + status_text(cfg))
        if replies:
            send("\n\n".join(replies), chat_id)
        if want_app:
            send(APP_INTRO, chat_id, webapp_keyboard())

    if changed:
        save_json(CONFIG_FILE, cfg)
    state["offset"] = offset          # сохраняем и offset, и список unknown_chats
    save_json(STATE_FILE, state)
    print(f"Обновлений: {len(updates)}, config изменён: {changed}")


if __name__ == "__main__":
    main()
