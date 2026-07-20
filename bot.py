"""
Cape Town Rental Bot
Проверяет Property24 и PrivateProperty на новые объявления в безопасных районах
рядом с ночным клубом Mavericks (радиус ~20 минут без пробок) и шлёт их в Telegram.

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
PRIORITY_TYPES = ("house", "townhouse")  # такие объявления помечаются приоритетными и идут первыми

# Безопасные районы в ~20 минутах без пробок от Mavericks (Barrack St, City Centre).
# Комбинированный поиск "to-rent" включает квартиры, дома и таунхаусы.
PROPERTY24_SEARCHES = {
    "Green Point": "https://www.property24.com/to-rent/green-point/cape-town/western-cape/11017",
    "Sea Point": "https://www.property24.com/to-rent/sea-point/cape-town/western-cape/11021",
    "Vredehoek": "https://www.property24.com/to-rent/vredehoek/cape-town/western-cape/9166",
    "City Centre": "https://www.property24.com/to-rent/cape-town-city-centre/cape-town/western-cape/9138",
    "De Waterkant": "https://www.property24.com/to-rent/de-waterkant/cape-town/western-cape/9130",
}

SEEN_FILE = Path(__file__).parent / "seen_listings.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

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
    match = re.search(r"(\d+)\s*(?:Parking|Garage)", text, re.IGNORECASE)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return 0
    return 0


def fetch_details(url: str):
    """Заходит на страницу конкретного объявления и достаёт срок аренды,
    меблировку и тип недвижимости — этих данных нет в карточке списка."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"Ошибка запроса деталей {url}: {e}")
        return {"lease_months": None, "furnished": None, "property_type": None}

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

    return {"lease_months": lease_months, "furnished": furnished, "property_type": property_type}


# ============== CORE ==============


def fetch_listings(area: str, url: str):
    results = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[{area}] Ошибка запроса: {e}")
        return results

    soup = BeautifulSoup(resp.text, "html.parser")

    # Property24 listing cards — карточки со ссылкой на объявление вида /to-rent/.../NNNNNN
    for link in soup.select("a[href*='/to-rent/']"):
        href = link.get("href", "")
        if not re.search(r"/\d{6,}$", href):
            continue  # это не карточка конкретного объявления, а ссылка на категорию

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
        if bedrooms and not (MIN_BEDROOMS <= bedrooms <= MAX_BEDROOMS):
            continue
        if bathrooms and not (MIN_BATHROOMS <= bathrooms <= MAX_BATHROOMS):
            continue
        if parking and not (MIN_PARKING <= parking <= MAX_PARKING):
            continue

        full_url = href if href.startswith("http") else f"https://www.property24.com{href}"
        listing_id = re.search(r"(\d{6,})$", href).group(1)

        title = link.get("title") or card_text[:80]

        results.append(
            {
                "id": f"p24_{listing_id}",
                "area": area,
                "price": price,
                "bedrooms": bedrooms,
                "bathrooms": bathrooms,
                "parking": parking,
                "title": title.strip(),
                "url": full_url,
            }
        )

    return results


def run_once():
    seen = load_seen()
    candidates = []

    for area, url in PROPERTY24_SEARCHES.items():
        listings = fetch_listings(area, url)
        for listing in listings:
            if listing["id"] not in seen:
                candidates.append(listing)
        time.sleep(2)  # вежливая пауза между запросами

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

        listing["lease_months"] = details["lease_months"]
        listing["furnished"] = details["furnished"]
        listing["property_type"] = details["property_type"]
        listing["priority"] = details["property_type"] in PRIORITY_TYPES if details["property_type"] else False

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
                f"{star}🏠 <b>{listing['area']}</b> · {type_str}\n"
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
