"""
Pokemon TCG UK stock/new-release monitor.

Checks a list of retailer pages for new or restocked products and
sends a Telegram message with a direct link the moment something
changes. It does NOT attempt to buy anything or bypass any
anti-bot / CAPTCHA protection - it only reads pages that are
already public, the same way your browser would.

Runs from GitHub Actions on a schedule (see .github/workflows/monitor.yml),
so your own computer never needs to be on.
"""

import json
import os
import re
import requests
from bs4 import BeautifulSoup

STATE_FILE = "seen_products.json"
SITES_FILE = "sites.json"

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

# Rough typical UK RRPs by product type, used only as a sanity ceiling
# (checked against a generous multiplier below) to filter out obviously
# marked-up / scalper-priced listings. Not exact - just meant to catch
# things priced way outside the normal range. Update these as RRPs change.
PRICE_CEILINGS = [
    # (keyword to match in title, typical RRP, allowed multiplier)
    ("booster box", 150, 1.3),
    ("elite trainer box", 55, 1.3),
    ("etb", 55, 1.3),
    ("premium collection", 60, 1.4),
    ("ultra-premium collection", 120, 1.4),
    ("ultra premium collection", 120, 1.4),
    ("booster bundle", 30, 1.3),
    ("build & battle", 30, 1.3),
    ("build and battle", 30, 1.3),
    ("battle deck", 20, 1.4),
    ("tin", 25, 1.5),
    ("booster pack", 6, 1.6),
    ("blister", 15, 1.5),
    ("collection box", 50, 1.4),
]


def price_looks_reasonable(title, price):
    """
    Returns True if we should notify about this price, False if it looks
    like a markup we should skip. If we can't tell (no price data, or no
    matching product type), we default to True rather than silently
    hiding things.
    """
    if price is None:
        return True
    title_lower = title.lower()
    for keyword, rrp, multiplier in PRICE_CEILINGS:
        if keyword in title_lower:
            return price <= rrp * multiplier
    return True  # unknown product type - don't filter blind


def extract_price(text):
    """Pulls a plain £ price out of a text blob, if present."""
    match = re.search(r"£\s?(\d+(?:\.\d{1,2})?)", text)
    if match:
        return float(match.group(1))
    return None


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=15,
        )
        if resp.status_code != 200:
            print(f"Telegram send failed: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"Telegram send error: {e}")


def check_shopify_json(site):
    """
    Most small/independent UK TCG shops (Gathering Games, The Card
    Vault, etc.) run on Shopify, which exposes a public products.json
    feed for every collection. This is far more reliable than
    scraping HTML and is normal public data, not a protected endpoint.
    """
    url = site["url"].rstrip("/") + "/products.json?limit=250"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"[{site['name']}] Error fetching: {e}")
        return []

    products = []
    for p in data.get("products", []):
        pid = str(p["id"])
        title = p["title"]
        handle = p["handle"]
        link = f"{site['base_url'].rstrip('/')}/products/{handle}"
        variants = p.get("variants", [])
        available = any(v.get("available") for v in variants)
        prices = [float(v["price"]) for v in variants if v.get("price") is not None]
        price = min(prices) if prices else None
        products.append(
            {"id": pid, "title": title, "link": link, "available": available, "price": price}
        )
    return products


def check_html(site):
    """
    Generic fallback for non-Shopify sites: scans a listing page for
    links whose text or URL contains the site's keyword (default
    'pokemon'). Works for simple server-rendered pages. Sites that
    render their catalogue with JavaScript, or that sit behind heavy
    bot-protection (common on big UK high-street retailers), may
    return incomplete results or block the request - that's a hard
    technical limit, not something this script tries to work around.
    """
    try:
        resp = requests.get(site["url"], headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        print(f"[{site['name']}] Error fetching: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    keyword = site.get("keyword", "pokemon").lower()
    products = []
    seen_links = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True)
        if not text or len(text) < 4:
            continue
        if keyword not in text.lower() and keyword not in href.lower():
            continue
        link = href if href.startswith("http") else site["base_url"].rstrip("/") + href
        if link in seen_links:
            continue
        seen_links.add(link)
        products.append(
            {
                "id": link,
                "title": text,
                "link": link,
                "available": True,
                "price": extract_price(text),
            }
        )

    return products


def main():
    sites = load_json(SITES_FILE, [])
    state = load_json(STATE_FILE, {})
    changed = False

    for site in sites:
        name = site["name"]
        seen_ids = set(state.get(name, []))

        if site["type"] == "shopify_json":
            products = check_shopify_json(site)
        elif site["type"] == "html":
            products = check_html(site)
        else:
            print(f"[{name}] Unknown site type '{site['type']}', skipping.")
            continue

        new_seen = set(seen_ids)
        for p in products:
            new_seen.add(p["id"])
            if p["id"] in seen_ids:
                continue

            # Only alert on things actually purchasable right now.
            if not p.get("available", True):
                continue

            # Skip anything priced well above a typical UK RRP for its
            # product type (see PRICE_CEILINGS above).
            if not price_looks_reasonable(p["title"], p.get("price")):
                print(f"Skipped (overpriced): [{name}] {p['title']} - £{p.get('price')}")
                continue

            price_str = f" - £{p['price']:.2f}" if p.get("price") is not None else ""
            msg = f"\U0001F195 <b>{name}</b>\n{p['title']}{price_str}\nIN STOCK\n{p['link']}"
            print(f"New: [{name}] {p['title']}{price_str}")
            send_telegram(msg)
            changed = True

        state[name] = list(new_seen)

    save_json(STATE_FILE, state)
    if not changed:
        print("No new products this run.")


if __name__ == "__main__":
    main()
