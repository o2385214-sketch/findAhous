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
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

# ============== CONFIG ==============

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Property24 блокирует IP дата-центров (GitHub Actions) — 503 после первого запроса.
# Чтобы обойти, ходим через сервис с ротацией жилых IP. Задай ОДИН из секретов:
#   SCRAPER_API_KEY — ключ ScraperAPI (https://www.scraperapi.com, есть бесплатный тариф)
#   SCRAPE_PROXY    — либо URL обычного http/https-прокси вида http://user:pass@host:port
# Если ни один не задан — бот ходит напрямую (на Actions будет ловить 503).
SCRAPER_API_KEY = os.environ.get("SCRAPER_API_KEY", "")
SCRAPE_PROXY = os.environ.get("SCRAPE_PROXY", "")

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
    """GET с ретраями и нарастающей паузой на 503/429. Возвращает Response или None.
    При заданном SCRAPER_API_KEY / SCRAPE_PROXY ходит через сервис ротации IP."""
    request_url = url
    proxies = None
    timeout = 25
    if SCRAPER_API_KEY:
        # ScraperAPI сам крутит IP; country_code=za — ЮАР-адреса; render=false (нам не нужен JS)
        request_url = (
            "https://api.scraperapi.com/?"
            f"api_key={SCRAPER_API_KEY}&country_code=za&url={quote(url, safe='')}"
        )
        timeout = 70  # прокси-сервису нужно больше времени
    elif SCRAPE_PROXY:
        proxies = {"http": SCRAPE_PROXY, "https": SCRAPE_PROXY}

    for i in range(attempts):
        try:
            resp = SESSION.get(request_url, timeout=timeout, proxies=proxies)
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
    # Цена вида "R 35 000" — число с пробелами-разделителями тысяч.
    # ВАЖНО: не захватить следующее за ценой число (кол-во комнат):
    # "R 35 000 2 Bedroom" -> 35000, а НЕ 350002. Поэтому берём либо группы
    # ровно по 3 цифры (35 000), либо слитное 4-6-значное число (35000).
    match = re.search(r"R\s*(\d{1,3}(?:[\s ]\d{3})+|\d{4,7})", text)
    if not match:
        return None
    digits = re.sub(r"[\s ]", "", match.group(1))
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
        return {"lease_months": None, "furnished": None, "property_type": None,
                "parking": None, "bathrooms": None}

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

    # парковка и санузлы на странице объявления — запасной источник,
    # т.к. в карточке списка их обычно нет (там только цена и кол-во комнат)
    parking = None
    m = re.search(r"(\d+)\s*(?:Covered Parking|Parking Bay|Parking|Garage)", text, re.IGNORECASE)
    if m:
        parking = int(m.group(1))

    bathrooms = None
    m = re.search(r"(\d+(?:\.\d+)?)\s*Bathroom", text, re.IGNORECASE)
    if m:
        bathrooms = int(float(m.group(1)))

    return {
        "lease_months": lease_months,
        "furnished": furnished,
        "property_type": property_type,
        "parking": parking,
        "bathrooms": bathrooms,
    }


# ============== CORE ==============


def fetch_page(category: str, url: str):
    results = []
    resp = http_get(url)
    if resp is None:
        print(f"[{category}] не получили страницу {url}")
        return results

    soup = BeautifulSoup(resp.text, "html.parser")

    anchors = soup.select("a[href*='/to-rent/']")
    stats = {"cards": 0, "blocked": 0, "no_price": 0, "too_pricey": 0,
             "bedrooms": 0, "bathrooms": 0, "parking": 0, "passed": 0}
    samples = []
    seen_on_page = set()

    # Property24 listing cards — карточки со ссылкой на объявление вида /to-rent/.../NNNNNN
    for link in anchors:
        href = link.get("href", "")
        if not re.search(r"/\d{6,}$", href):
            continue  # это не карточка конкретного объявления, а ссылка на категорию

        listing_id = re.search(r"(\d{6,})$", href).group(1)
        if listing_id in seen_on_page:
            continue  # у карточки бывает несколько ссылок (фото + заголовок) — считаем один раз
        seen_on_page.add(listing_id)
        stats["cards"] += 1

        full_url = href if href.startswith("http") else f"https://www.property24.com{href}"

        # Опасный район? — отсекаем сразу по slug из ссылки.
        suburb_slug = suburb_from_url(full_url)
        if suburb_slug in DANGEROUS_SUBURBS:
            stats["blocked"] += 1
            continue

        card = link.find_parent(["div", "li", "article"]) or link
        card_text = card.get_text(" ", strip=True)

        price = parse_price(card_text)
        bedrooms = parse_bedrooms(card)
        bathrooms = parse_bathrooms(card)
        parking = parse_parking(card)

        if len(samples) < 3:
            samples.append(f"price={price} bed={bedrooms} bath={bathrooms} park={parking} | {card_text[:90]}")

        if price is None:
            stats["no_price"] += 1
            continue
        if price > MAX_PRICE:
            stats["too_pricey"] += 1
            continue
        # число комнат ОБЯЗАТЕЛЬНО должно быть распознано и попадать в диапазон
        # (bedrooms == 0 означает «не смогли определить» — такое больше не шлём)
        if not (MIN_BEDROOMS <= bedrooms <= MAX_BEDROOMS):
            stats["bedrooms"] += 1
            continue
        if bathrooms and not (MIN_BATHROOMS <= bathrooms <= MAX_BATHROOMS):
            stats["bathrooms"] += 1
            continue
        # парковку окончательно проверяем в run_once (там есть запасной источник — страница объявления)
        if parking and not (MIN_PARKING <= parking <= MAX_PARKING):
            stats["parking"] += 1
            continue

        title = link.get("title") or card_text[:80]
        stats["passed"] += 1
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

    print(f"[{category}] ссылок={len(anchors)} карточек={stats['cards']} "
          f"прошло={stats['passed']} | отсев: район={stats['blocked']} "
          f"нет_цены={stats['no_price']} дорого={stats['too_pricey']} "
          f"комнаты={stats['bedrooms']} санузлы={stats['bathrooms']} парковка={stats['parking']}")
    if stats["cards"] and not stats["passed"]:
        for s in samples:
            print(f"    пример: {s}")
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

        # санузлы: в карточке их обычно нет — берём со страницы объявления (для показа)
        listing["bathrooms"] = listing["bathrooms"] or details["bathrooms"] or 0

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
