"""
Cape Town Rental Bot
Проверяет Property24 на новые объявления по ВСЕМУ Кейптауну (только жилые категории:
квартиры, дома, таунхаусы) и шлёт подходящие в Telegram. Опасные районы (townships,
Mitchell's Plain и т.п.) исключаются по чёрному списку DANGEROUS_SUBURBS.

Настройки — ниже, в блоке CONFIG.
"""

import json
import os
import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ============== CONFIG ==============

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

MAX_PRICE = 20000          # максимум ZAR / месяц
MIN_BEDROOMS = 1
MAX_BEDROOMS = 2
MIN_BATHROOMS = 1
MAX_BATHROOMS = 2
MIN_PARKING = 1
MAX_PARKING = 2

REQUIRED_LEASE_MONTHS = 12   # None = не проверять срок аренды
FURNISHED_ONLY = True        # слать только меблированные (если статус не удалось определить — не отбрасываем)
REQUIRE_PARKING = True       # слать только если парковка определена и в диапазоне MIN/MAX_PARKING
PRIORITY_TYPES = ("house", "townhouse")  # такие объявления помечаются приоритетными и идут первыми

# Зона поиска — ВЕСЬ Кейптаун (код района 432), только жилые категории:
# коммерция (офисы, склады, участки) сюда физически не попадает.
# Сортировка "сначала новые" + проверяем первые PAGES_PER_SEARCH страниц каждой категории.
CAPE_TOWN_SEARCHES = {
    "apartment": "https://www.property24.com/apartments-to-rent/cape-town/western-cape/432",
    "house":     "https://www.property24.com/houses-to-rent/cape-town/western-cape/432",
    "townhouse": "https://www.property24.com/townhouses-to-rent/cape-town/western-cape/432",
}
SORT_NEWEST = "?sp=so%3dNewest"
PAGES_PER_SEARCH = 1   # 1 страницы «самых новых» хватает при ежечасном запуске; меньше запросов = меньше 503

# Опасные районы Кейптауна (townships и высококриминальные зоны Cape Flats).
# Совпадение по slug в URL объявления => объявление пропускается.
# Список легко расширять: добавь slug строчными буквами, слова через дефис.
# (Несколько пограничных районов — retreat/steenberg/grassy-park — включены на всякий
#  случай ради безопасности; если считаешь их нормальными, просто удали из набора.)
DANGEROUS_SUBURBS = {
    # Mitchell's Plain и его части
    "mitchells-plain", "mitchell-s-plain", "tafelsig", "beacon-valley", "lentegeur",
    "portlands", "westridge", "eastridge", "rocklands", "woodlands", "strandfontein",
    # Крупные townships
    "khayelitsha", "nyanga", "gugulethu", "langa", "philippi", "crossroads",
    "browns-farm", "samora-machel", "mfuleni",
    # Cape Flats — высокий уровень преступности
    "manenberg", "hanover-park", "heideveld", "bonteheuwel", "bishop-lavis",
    "valhalla-park", "elsies-river", "factreton", "kensington", "delft", "wesbank",
    "lavender-hill", "seawinds", "vrygrond", "capricorn",
    # Южные/восточные пограничные
    "ocean-view", "atlantis", "macassar", "blue-downs", "eerste-river", "scottsdene",
    "retreat", "steenberg", "grassy-park",
}

SEEN_FILE = Path(__file__).parent / "seen_listings.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-ZA,en;q=0.9",
    "Referer": "https://www.property24.com/to-rent/cape-town/western-cape/432",
    "Upgrade-Insecure-Requests": "1",
}

# Property24 ратлимитит IP дата-центров (GitHub Actions) и отдаёт 503/429.
# Ходим через одну сессию (общий keep-alive/cookies) с ретраями и паузами.
SESSION = requests.Session()
SESSION.headers.update(HEADERS)
RETRY_STATUS = {429, 503, 502, 504}


def http_get(url: str, attempts: int = 4):
    """GET с ретраями и нарастающей паузой на 503/429. Возвращает Response или None."""
    for i in range(attempts):
        try:
            resp = SESSION.get(url, timeout=25)
        except requests.RequestException as e:
            if i < attempts - 1:
                time.sleep(6 * (i + 1))
                continue
            print(f"  запрос не удался {url}: {e}")
            return None
        if resp.status_code in RETRY_STATUS and i < attempts - 1:
            wait = 6 * (i + 1)
            print(f"  {resp.status_code} для {url} — ретрай через {wait}s")
            time.sleep(wait)
            continue
        if not resp.ok:
            print(f"  {resp.status_code} для {url} — пропуск")
            return None
        return resp
    return None

# ============== HELPERS ==============


def load_seen() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()


def save_seen(seen: set) -> None:
    SEEN_FILE.write_text(json.dumps(sorted(seen)))


def send_telegram(text: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("WARNING: TELEGRAM_TOKEN / TELEGRAM_CHAT_ID не заданы — сообщение не отправлено.")
        print(text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=15,
    )
    if not resp.ok:
        print("Telegram error:", resp.status_code, resp.text)


def parse_price(text: str):
    match = re.search(r"R\s?([\d\s]+)", text)
    if not match:
        return None
    digits = match.group(1).replace(" ", "").replace("\xa0", "")
    return int(digits) if digits.isdigit() else None


def parse_bedrooms(card) -> int:
    # Property24 listing cards mark bedroom count with a specific span;
    # fall back to searching the card text for "N Bedroom".
    text = card.get_text(" ", strip=True)
    match = re.search(r"(\d+(?:\.\d+)?)\s*Bedroom", text, re.IGNORECASE)
    if match:
        try:
            return int(float(match.group(1)))
        except ValueError:
            return 0
    return 0


def parse_bathrooms(card) -> int:
    text = card.get_text(" ", strip=True)
    match = re.search(r"(\d+(?:\.\d+)?)\s*Bathroom", text, re.IGNORECASE)
    if match:
        try:
            return int(float(match.group(1)))
        except ValueError:
            return 0
    return 0


def parse_parking(card) -> int:
    text = card.get_text(" ", strip=True)
    # Property24 обычно указывает как "Parking" / "Garage" / "Parking Bay(s)"
    match = re.search(r"(\d+)\s*(?:Covered Parking|Parking|Garage)", text, re.IGNORECASE)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return 0
    return 0


def suburb_from_url(url: str) -> str:
    """Достаёт slug района из ссылки на объявление:
    .../to-rent/sea-point/cape-town/... -> 'sea-point'."""
    m = re.search(r"/to-rent/([a-z0-9-]+)/cape-town/", url)
    return m.group(1) if m else ""


def prettify_suburb(slug: str) -> str:
    return slug.replace("-", " ").title() if slug else "Cape Town"


def fetch_details(url: str):
    """Заходит на страницу конкретного объявления и достаёт срок аренды,
    меблировку и тип недвижимости — этих данных нет в карточке списка."""
    resp = http_get(url)
    if resp is None:
        print(f"Не получили детали {url}")
        return {"lease_months": None, "furnished": None, "property_type": None, "parking": None}

    text = BeautifulSoup(resp.text, "html.parser").get_text(" ", strip=True)

    lease_months = None
    m = re.search(r"Lease Period\D{0,15}?(\d+)\s*month", text, re.IGNORECASE)
    if m:
        lease_months = int(m.group(1))

    furnished = None
    lower = text.lower()
    if "unfurnished" in lower:
        furnished = False
    elif "furnished" in lower:
        furnished = True

    property_type = None
    m = re.search(r"Type of Property\D{0,10}?(House|Townhouse|Apartment|Flat|Duplex|Cluster)", text, re.IGNORECASE)
    if m:
        property_type = m.group(1).lower()

    # парковка на странице объявления — запасной источник, если в карточке её не было
    parking = None
    m = re.search(r"(\d+)\s*(?:Covered Parking|Parking Bay|Parking|Garage)", text, re.IGNORECASE)
    if m:
        parking = int(m.group(1))

    return {
        "lease_months": lease_months,
        "furnished": furnished,
        "property_type": property_type,
        "parking": parking,
    }


# ============== CORE ==============


def fetch_page(category: str, url: str):
    results = []
    resp = http_get(url)
    if resp is None:
        print(f"[{category}] не получили страницу {url}")
        return results

    soup = BeautifulSoup(resp.text, "html.parser")

    # Property24 listing cards — карточки со ссылкой на объявление вида /to-rent/.../NNNNNN
    for link in soup.select("a[href*='/to-rent/']"):
        href = link.get("href", "")
        if not re.search(r"/\d{6,}$", href):
            continue  # это не карточка конкретного объявления, а ссылка на категорию

        full_url = href if href.startswith("http") else f"https://www.property24.com{href}"

        # Опасный район? — отсекаем сразу по slug из ссылки.
        suburb_slug = suburb_from_url(full_url)
        if suburb_slug in DANGEROUS_SUBURBS:
            continue

        card = link.find_parent(["div", "li", "article"]) or link
        card_text = card.get_text(" ", strip=True)

        price = parse_price(card_text)
        bedrooms = parse_bedrooms(card)
        bathrooms = parse_bathrooms(card)
        parking = parse_parking(card)

        if price is None:
            continue
        if price > MAX_PRICE:
            continue
        # число комнат ОБЯЗАТЕЛЬНО должно быть распознано и попадать в диапазон
        # (bedrooms == 0 означает «не смогли определить» — такое больше не шлём)
        if not (MIN_BEDROOMS <= bedrooms <= MAX_BEDROOMS):
            continue
        if bathrooms and not (MIN_BATHROOMS <= bathrooms <= MAX_BATHROOMS):
            continue
        # парковку окончательно проверяем в run_once (там есть запасной источник — страница объявления)
        if parking and not (MIN_PARKING <= parking <= MAX_PARKING):
            continue

        listing_id = re.search(r"(\d{6,})$", href).group(1)
        title = link.get("title") or card_text[:80]

        results.append(
            {
                "id": f"p24_{listing_id}",
                "category": category,
                "suburb": prettify_suburb(suburb_slug),
                "price": price,
                "bedrooms": bedrooms,
                "bathrooms": bathrooms,
                "parking": parking,
                "title": title.strip(),
                "url": full_url,
            }
        )

    return results


def fetch_listings(category: str, base_url: str):
    """Первые PAGES_PER_SEARCH страниц категории, отсортированных «сначала новые»."""
    results = []
    for page in range(1, PAGES_PER_SEARCH + 1):
        page_path = "" if page == 1 else f"/p{page}"
        url = f"{base_url}{page_path}{SORT_NEWEST}"
        results.extend(fetch_page(category, url))
        time.sleep(3)
    return results


def run_once():
    seen = load_seen()
    candidates = []
    seen_ids_this_run = set()  # объявление может встретиться в нескольких категориях — не дублируем

    for category, base_url in CAPE_TOWN_SEARCHES.items():
        listings = fetch_listings(category, base_url)
        for listing in listings:
            if listing["id"] in seen or listing["id"] in seen_ids_this_run:
                continue
            seen_ids_this_run.add(listing["id"])
            candidates.append(listing)
        time.sleep(4)  # вежливая пауза между категориями, чтобы не ловить 503

    new_listings = []
    for listing in candidates:
        details = fetch_details(listing["url"])
        time.sleep(1)

        if REQUIRED_LEASE_MONTHS and details["lease_months"] and details["lease_months"] != REQUIRED_LEASE_MONTHS:
            seen.add(listing["id"])  # запомнили, чтобы не проверять повторно
            continue
        if FURNISHED_ONLY and details["furnished"] is False:
            seen.add(listing["id"])
            continue

        # парковка: из карточки, а если там не было — со страницы объявления
        parking = listing["parking"] or details["parking"] or 0
        if REQUIRE_PARKING and not (MIN_PARKING <= parking <= MAX_PARKING):
            seen.add(listing["id"])
            continue
        listing["parking"] = parking

        listing["lease_months"] = details["lease_months"]
        listing["furnished"] = details["furnished"]
        # тип: со страницы объявления, иначе — по категории поиска
        listing["property_type"] = details["property_type"] or listing["category"]
        listing["priority"] = listing["property_type"] in PRIORITY_TYPES

        new_listings.append(listing)
        seen.add(listing["id"])

    # приоритетные (дом/таунхаус) — в начало списка
    new_listings.sort(key=lambda x: not x["priority"])

    if new_listings:
        for listing in new_listings:
            bedrooms_str = f"{listing['bedrooms']}-комн." if listing["bedrooms"] else "комнаты н/д"
            bathrooms_str = f"{listing['bathrooms']} с/у" if listing["bathrooms"] else "с/у н/д"
            parking_str = f"{listing['parking']} парк.места" if listing["parking"] else "парковка н/д"
            type_str = (listing["property_type"] or "тип н/д").capitalize()
            furnished_str = {True: "меблир.", False: "без мебели", None: "мебель н/д"}[listing["furnished"]]
            lease_str = f"{listing['lease_months']} мес." if listing["lease_months"] else "срок н/д"
            star = "⭐ " if listing["priority"] else ""

            text = (
                f"{star}🏠 <b>{listing['suburb']}</b> · {type_str}\n"
                f"💰 R{listing['price']:,}/мес · {bedrooms_str} · {bathrooms_str} · {parking_str}\n"
                f"{furnished_str} · аренда {lease_str}\n"
                f"{listing['title']}\n"
                f"{listing['url']}"
            ).replace(",", " ")
            send_telegram(text)
            time.sleep(1)
        save_seen(seen)
        print(f"Отправлено новых объявлений: {len(new_listings)}")
    else:
        save_seen(seen)
        print("Новых объявлений нет.")


if __name__ == "__main__":
    run_once()
