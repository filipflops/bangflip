import json
import os
import re
import sys
from pathlib import Path
import requests
from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

MAX_PLAUSIBLE_PRICE = 1_000_000


def load_json(path: Path, default_value):
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default_value


def save_json(path: Path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def parse_price(raw: str) -> float | None:
    """Converts raw price strings (e.g., '1,234.56', '716.10') into a float."""
    raw = raw.strip().replace("$", "")
    
    if "," in raw and "." in raw:
        if raw.find(",") < raw.find("."):
            raw = raw.replace(",", "")
        else:
            raw = raw.replace(".", "").replace(",", ".")
    elif "," in raw:
        if len(raw.split(",")[-1]) <= 2:
            raw = raw.replace(",", ".")
        else:
            raw = raw.replace(",", "")
            
    try:
        value = float(raw)
    except ValueError:
        return None
        
    if value <= 0 or value > MAX_PLAUSIBLE_PRICE:
        return None
    return value


PRICE_NUMBER_PATTERN = r"[\d]{1,3}(?:[.,\s]?\d{3})*(?:[.,]\d{1,2})?"


def extract_listing_prices(
    text: str,
    currency_symbol: str,
    buy_button_text: str = "Add Funds",
    section_heading: str = "On Sale",
    lookbehind_chars: int = 80,
) -> list[float]:
    """Scrapes individual listing row prices immediately preceding the action button."""
    scope_start = text.find(section_heading)
    scoped_text = text[scope_start:] if scope_start != -1 else text

    prices = []
    search_start = 0
    escaped_currency = re.escape(currency_symbol)
    row_price_pattern = re.compile(rf"{escaped_currency}\s?({PRICE_NUMBER_PATTERN})")

    while True:
        idx = scoped_text.find(buy_button_text, search_start)
        if idx == -1:
            # Fallback check for alternative button state (e.g., "Buy")
            if buy_button_text == "Add Funds":
                idx = scoped_text.find("Buy", search_start)
                if idx == -1:
                    break
            else:
                break
                
        window = scoped_text[max(0, idx - lookbehind_chars): idx]
        found = list(row_price_pattern.finditer(window))
        if found:
            price = parse_price(found[-1].group(1))
            if price is not None:
                prices.append(price)
        search_start = idx + len(buy_button_text)

    return prices


def fetch_lowest_price(
    playwright,
    url: str,
    currency_symbol: str,
    buy_button_text: str = "Add Funds",
) -> float | None:
    browser = playwright.chromium.launch(headless=True)
    try:
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=30000)

        try:
            page.wait_for_function(
                """(btnText) => document.body.innerText.includes(btnText) || document.body.innerText.includes('Buy')""",
                arg=buy_button_text,
                timeout=20000,
            )
        except Exception:
            print(f"  [WARNING] Delayed loading for element containing '{buy_button_text}'. Trying extraction anyway.", file=sys.stderr)

        page.wait_for_timeout(2000)
        text = page.inner_text("body")

        prices = extract_listing_prices(text, currency_symbol, buy_button_text)
        if prices:
            return min(prices)

        print(f"  [WARNING] No dynamic offers located using button text '{buy_button_text}'. verify your site localization configurations.", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  [ERROR] Connection or selector parsing failed for {url}: {e}", file=sys.stderr)
        return None
    finally:
        browser.close()


def send_telegram_message(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("  [WARNING] Target TELEGRAM_BOT_TOKEN or CHAT_ID missing from environment variables. Skipping alert pipeline.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text})
    if resp.status_code != 200:
        print(f"  [ERROR] Telegram Endpoint Response: {resp.status_code} {resp.text}", file=sys.stderr)


def add_skin_interactive():
    """CLI configuration engine to easily add tracked items."""
    print("\n⚡ --- SkinSwap Price Monitor Setup ---")
    name = input("🔹 Enter Skin Name (e.g., AK-47 | Rat Rod (Factory New)): ").strip()
    if not name:
        print("❌ Item name declaration required.")
        return

    url = input("🔹 Paste SkinSwap URL: ").strip()
    if not url.startswith("http"):
        print("❌ Invalid URL schema provided.")
        return

    try:
        threshold = float(input("🔹 Enter Target USD Alert Price (e.g., 12.50): ").strip())
    except ValueError:
        print("❌ Numerical valuation mandatory for thresholds.")
        return

    skin_id = re.sub(r'[^a-z0-9]', '-', name.lower()).strip('-')
    skin_id = re.sub(r'-+', '-', skin_id)

    config = load_json(CONFIG_PATH, {"skins": []})
    
    if any(item["id"] == skin_id for item in config["skins"]):
        skin_id = f"{skin_id}-{os.getpid()}"

    config["skins"].append({
        "id": skin_id,
        "name": name,
        "url": url,
        "threshold": threshold,
        "currency_symbol": "$",
        "buy_button_text": "Add Funds"
    })

    save_json(CONFIG_PATH, config)
    print(f"\n✅ Tracking configured! '{name}' logged successfully under target threshold ${threshold:.2f}.\n")


def main():
    config = load_json(CONFIG_PATH, {"skins": []})
    state = load_json(STATE_PATH, {})

    skins = config.get("skins", [])
    if not skins:
        print("Empty config array. Use 'python monitor.py add' to initiate new assets.")
        return

    with sync_playwright() as playwright:
        for skin in skins:
            skin_id = skin["id"]
            name = skin["name"]
            url = skin["url"]
            threshold = float(skin["threshold"])
            currency_symbol = skin.get("currency_symbol", "$")
            buy_button_text = skin.get("buy_button_text", "Add Funds")

            print(f"Checking: {name}")
            price = fetch_lowest_price(playwright, url, currency_symbol, buy_button_text)

            if price is None:
                print("  Failed to extract price metrics, skipping evaluation step.")
                continue

            print(f"  Lowest Price Found: {currency_symbol}{price:.2f} (Target: {currency_symbol}{threshold:.2f})")

            prev = state.get(skin_id, {"notified": False})

            if price <= threshold:
                if not prev.get("notified"):
                    message = (
                        f"🔔 {name}\n"
                        f"Price dropped to {currency_symbol}{price:.2f} "
                        f"(Threshold: {currency_symbol}{threshold:.2f})\n"
                        f"Link: {url}"
                    )
                    send_telegram_message(message)
                    print("  -> Telegram alert dispatched.")
                else:
                    print("  -> Premium pricing still available; anti-spam logic suppressed further updates.")
                state[skin_id] = {"notified": True, "last_price": price}
            else:
                state[skin_id] = {"notified": False, "last_price": price}

    save_json(STATE_PATH, state)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1].lower() == "add":
        add_skin_interactive()
    else:
        main()