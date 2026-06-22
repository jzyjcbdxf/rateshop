import math
import os
import re
import shutil
import subprocess
import time
from datetime import date, timedelta
from typing import Dict, List, Optional
from urllib.parse import urlencode

import pandas as pd
import streamlit as st
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# ============================================================
# IMPORTANT VERSION MARKER
# If you do not see this marker in Streamlit sidebar, the old app.py is still running.
# ============================================================
APP_VERSION = "2026-06-22 Starwood Hotel Rateshop selectable email quotes"

# ============================================================
# Hotel map: dropdown label -> Starwood booking hotel code
# ============================================================
HOTEL_CODE_MAP: Dict[str, str] = {
    "1SB": "60507",
    "1CP": "60735",
    "1BB": "66266",
    "1TY": "96185",
    "1ML": "47157",
    "1MF": "40333",
    "1HNB": "5826",
    "1CPH": "41069",
    "1NV": "35903",
    "1SF": "36017",
    "1SE": "47314",
    "1TO": "31116",
    "1WH": "77961",
}

DEFAULT_HOTEL_KEY = "1SB"
DEFAULT_CHECKIN = date.today()
DEFAULT_CHECKOUT = date.today() + timedelta(days=1)
DEFAULT_DISCOUNT_PERCENT = 10
BASE_BOOKING_URL = "https://www.1hotels.com/book/{hotel_code}"

# Keywords used only as a room-name filter.
# The scraper scans h1/h2/h3/h4 titles and keeps titles that look like room names.
# This prevents non-room text such as navigation labels, banners, or policy copy from being parsed as rooms.
ROOM_NAME_HINTS = (
    "room",
    "king",
    "queen",
    "suite",
    "studio",
    "home",
    "ocean",
    "city",
    "skyline",
    "two",
    "one",
    "balcony",
)

PRICE_RE = re.compile(r"\$\s*([0-9][0-9,]*)")

st.set_page_config(page_title="Starwood Hotel Rateshop", layout="wide")


# ============================================================
# Authentication
# Streamlit Cloud -> App settings -> Secrets:
# user_name = 123
# password = 456
# ============================================================
def get_secret_value(key: str, default: str = "") -> str:
    try:
        value = st.secrets.get(key, default)
    except Exception:
        value = default
    return str(value)


def login_required() -> None:
    expected_user_name = get_secret_value("user_name")
    expected_password = get_secret_value("password")

    if not expected_user_name or not expected_password:
        st.error(
            "Please configure the login credentials in Streamlit Secrets first:\n\n"
            "user_name = 123\n"
            "password = 456"
        )
        st.stop()

    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False

    if st.session_state.authenticated:
        return

    st.title("🔐 Starwood Hotel Rateshop Login")
    with st.form("login_form", clear_on_submit=False):
        user_name = st.text_input("User Name")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("LOGIN", type="primary")

    if submitted:
        if user_name == expected_user_name and password == expected_password:
            st.session_state.authenticated = True
            st.session_state.authenticated_user_name = user_name
            st.rerun()
        else:
            st.error("Invalid username or password.")

    st.stop()


login_required()


# ============================================================
# User preference cache
# This is process-level cache for Streamlit reruns while the app process is alive.
# ============================================================
@st.cache_resource(show_spinner=False)
def get_preference_cache() -> Dict[str, Dict[str, object]]:
    return {}


def default_ending_text() -> str:
    valid_until = date.today() + timedelta(days=3)
    return (
        f"This quote is valid until {valid_until.isoformat()}. Change in stay date will result in new pricing.\n"
        "Rates are fully pre-paid and non-refundable. 100% room, tax and resort fee are charged at time of booking.\n\n"
        "Please let us know which room type would you like to choose."
    )


def get_current_user_key() -> str:
    return str(st.session_state.get("authenticated_user_name") or get_secret_value("user_name") or "default_user")


def load_user_preferences_once() -> None:
    user_key = get_current_user_key()
    cache = get_preference_cache()
    prefs = cache.get(user_key, {})

    if "email_opening" not in st.session_state:
        st.session_state.email_opening = str(prefs.get("email_opening", ""))
    if "email_ending" not in st.session_state:
        st.session_state.email_ending = str(prefs.get("email_ending", default_ending_text()))
    if "rates_include_tax" not in st.session_state:
        st.session_state.rates_include_tax = bool(prefs.get("rates_include_tax", False))


def save_user_preferences() -> None:
    user_key = get_current_user_key()
    cache = get_preference_cache()
    cache[user_key] = {
        "email_opening": st.session_state.get("email_opening", ""),
        "email_ending": st.session_state.get("email_ending", default_ending_text()),
        "rates_include_tax": bool(st.session_state.get("rates_include_tax", False)),
    }


load_user_preferences_once()


# ============================================================
# URL builder
# ============================================================
def build_booking_url(
    hotel_code: str,
    checkin: date,
    checkout: date,
    adults: int = 1,
    children: int = 0,
    language: str = "en",
    dogs: bool = False,
    cats: bool = False,
    currency: str = "USD",
    group_code: str = "",
    promo_code: str = "",
    sort: str = "low",
) -> str:
    params = {
        "startDate": checkin.isoformat(),
        "endDate": checkout.isoformat(),
        "adults": adults,
        "children": children,
        "exactMatchOnly": "false",
        "language": language,
        "dogs": str(dogs).lower(),
        "cats": str(cats).lower(),
        "rooms": "[]",
        "currency": currency,
        "groupCode": group_code,
        "promoCode": promo_code,
        "sort": sort,
    }
    return f"{BASE_BOOKING_URL.format(hotel_code=hotel_code)}?{urlencode(params)}"


# ============================================================
# Chrome / Chromedriver helpers
# This version intentionally NEVER falls back to Selenium Manager.
# If /usr/bin/chromedriver is not installed, it fails with a clear packages.txt message.
# This prevents Selenium from using:
# /home/appuser/.cache/selenium/chromedriver/linux64/.../chromedriver
# ============================================================
def shell_output(command: List[str], timeout: int = 10) -> Dict[str, str]:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return {
            "command": " ".join(command),
            "returncode": str(completed.returncode),
            "stdout": (completed.stdout or "").strip(),
            "stderr": (completed.stderr or "").strip(),
        }
    except Exception as exc:
        return {
            "command": " ".join(command),
            "returncode": "exception",
            "stdout": "",
            "stderr": str(exc),
        }


def first_existing_executable(paths: List[str]) -> Optional[str]:
    for path in paths:
        if path and os.path.exists(path) and os.access(path, os.X_OK):
            return path
    return None


def first_path_from_which(names: List[str]) -> Optional[str]:
    for name in names:
        path = shutil.which(name)
        if path and os.path.exists(path) and os.access(path, os.X_OK):
            return path
    return None


def version_of(binary_path: Optional[str]) -> str:
    if not binary_path:
        return "not found"
    result = shell_output([binary_path, "--version"], timeout=8)
    combined = (result.get("stdout") or result.get("stderr") or "").strip()
    return combined or f"version unavailable, rc={result.get('returncode')}"


@st.cache_resource(show_spinner=False)
def get_chrome_runtime() -> Dict[str, object]:
    chromium_binary = first_existing_executable(
        [
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
        ]
    ) or first_path_from_which(["chromium", "chromium-browser", "google-chrome", "google-chrome-stable"])

    chromedriver_binary = first_existing_executable(
        [
            "/usr/bin/chromedriver",
            "/usr/lib/chromium/chromedriver",
            "/usr/lib/chromium-browser/chromedriver",
            "/snap/bin/chromium.chromedriver",
        ]
    ) or first_path_from_which(["chromedriver"])

    diagnostics = {
        "app_version": APP_VERSION,
        "cwd": os.getcwd(),
        "python": shell_output(["python", "--version"]),
        "which_chromium": shell_output(["/bin/sh", "-lc", "which chromium || true"]),
        "which_chromium_browser": shell_output(["/bin/sh", "-lc", "which chromium-browser || true"]),
        "which_chromedriver": shell_output(["/bin/sh", "-lc", "which chromedriver || true"]),
        "ls_usr_bin_chromium": shell_output(["/bin/sh", "-lc", "ls -l /usr/bin/chromium /usr/bin/chromedriver 2>&1 || true"]),
        "dpkg_chromium": shell_output(["/bin/sh", "-lc", "dpkg -l | grep -E 'chromium|chromedriver|chrome' || true"]),
        "selenium_cache": shell_output(["/bin/sh", "-lc", "ls -la /home/appuser/.cache/selenium 2>&1 || true"]),
    }

    return {
        "chromium_binary": chromium_binary,
        "chromedriver_binary": chromedriver_binary,
        "chromium_version": version_of(chromium_binary),
        "chromedriver_version": version_of(chromedriver_binary),
        "diagnostics": diagnostics,
    }


def validate_chrome_runtime() -> Dict[str, object]:
    runtime = get_chrome_runtime()
    chromium_binary = runtime.get("chromium_binary")
    chromedriver_binary = runtime.get("chromedriver_binary")

    if not chromium_binary or not chromedriver_binary:
        missing = []
        if not chromium_binary:
            missing.append("chromium")
        if not chromedriver_binary:
            missing.append("chromedriver")
        raise RuntimeError(
            "Streamlit Cloud did not detect the required browser dependencies: "
            + ", ".join(missing)
            + ". Please confirm packages.txt is in the GitHub repo root and contains exactly two lines: chromium and chromium-driver."
            + " After fixing it, reboot the app, clear cache, and redeploy."
        )

    return runtime


def build_chrome_options(chromium_binary: str) -> Options:
    chrome_options = Options()
    chrome_options.binary_location = chromium_binary
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-software-rasterizer")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-background-networking")
    chrome_options.add_argument("--disable-default-apps")
    chrome_options.add_argument("--disable-sync")
    chrome_options.add_argument("--metrics-recording-only")
    chrome_options.add_argument("--mute-audio")
    chrome_options.add_argument("--no-first-run")
    chrome_options.add_argument("--disable-setuid-sandbox")
    chrome_options.add_argument("--single-process")
    chrome_options.add_argument("--remote-debugging-port=9222")
    chrome_options.add_argument("--window-size=1920,1400")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument("--lang=en-US,en")
    chrome_options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    chrome_options.add_experimental_option("useAutomationExtension", False)
    return chrome_options


def init_driver() -> webdriver.Chrome:
    runtime = validate_chrome_runtime()
    chromium_binary = str(runtime["chromium_binary"])
    chromedriver_binary = str(runtime["chromedriver_binary"])

    # CRITICAL: always pass Service(executable_path=...).
    # Do not call webdriver.Chrome(options=...), because that invokes Selenium Manager.
    service = Service(executable_path=chromedriver_binary)
    chrome_options = build_chrome_options(chromium_binary)
    driver = webdriver.Chrome(service=service, options=chrome_options)
    driver.set_page_load_timeout(75)
    return driver


# ============================================================
# Parsing helpers
# ============================================================
def parse_price_to_int(text: str) -> Optional[int]:
    if not text:
        return None
    match = PRICE_RE.search(text.replace("\xa0", " "))
    if not match:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return None


def normalize_room_name(name: str) -> str:
    return re.sub(r"\s+", " ", name or "").strip()


def looks_like_room_name(name: str) -> bool:
    cleaned = normalize_room_name(name)
    if len(cleaned) < 4 or len(cleaned) > 90:
        return False
    lower_name = cleaned.lower()
    return any(hint in lower_name for hint in ROOM_NAME_HINTS)


def discount_price(current_price: int, discount_percent: float) -> int:
    discounted = current_price * (1 - discount_percent / 100)
    return int(round(discounted))


def format_money(value: int) -> str:
    return f"${value:,}"


def dedupe_rooms(raw_rooms: List[Dict]) -> List[Dict]:
    best_by_room: Dict[str, Dict] = {}
    for room in raw_rooms:
        room_name = normalize_room_name(str(room.get("room_name", "")))
        current_price = room.get("current_selling")
        if not looks_like_room_name(room_name) or not isinstance(current_price, int):
            continue
        if current_price <= 0 or current_price > 20000:
            continue

        key = room_name.lower()
        if key not in best_by_room or current_price < best_by_room[key]["current_selling"]:
            best_by_room[key] = {
                "room_name": room_name,
                "current_selling": current_price,
                "all_detected_prices": sorted(set(room.get("all_detected_prices", [current_price]))),
            }

    return sorted(best_by_room.values(), key=lambda item: (item["current_selling"], item["room_name"]))


def parse_rooms_with_browser_dom(driver: webdriver.Chrome) -> List[Dict]:
    script = r"""
    const priceRegex = /^\$\s*[0-9][0-9,]*/;
    const titleNodes = Array.from(document.querySelectorAll('h1,h2,h3,h4'));
    const rows = [];

    function cleanText(value) {
      return (value || '').replace(/\s+/g, ' ').trim();
    }

    function isRoomLike(value) {
      const text = cleanText(value).toLowerCase();
      return text.length >= 4 && text.length <= 90 &&
        /(room|king|queen|suite|studio|home|ocean|city|skyline|two|one|balcony)/i.test(text);
    }

    function getCard(node) {
      return node.closest('[data-scope="carousel"][data-part="item"]') ||
             node.closest('.chakra-card__root') ||
             node.closest('article') ||
             node.closest('section') ||
             node.parentElement;
    }

    for (const titleNode of titleNodes) {
      const roomName = cleanText(titleNode.innerText || titleNode.textContent);
      if (!isRoomLike(roomName)) continue;

      const card = getCard(titleNode);
      if (!card) continue;

      const priceNodes = Array.from(card.querySelectorAll('p,span,div,label'));
      const pPrices = [];
      const allPrices = [];

      for (const node of priceNodes) {
        const text = cleanText(node.innerText || node.textContent);
        if (!priceRegex.test(text)) continue;
        if (/amenity|fee|tax|total|include/i.test(text)) continue;

        const match = text.match(/\$\s*([0-9][0-9,]*)/);
        if (!match) continue;
        const value = parseInt(match[1].replace(/,/g, ''), 10);
        if (!Number.isFinite(value) || value <= 0 || value > 20000) continue;

        allPrices.push(value);
        if (node.tagName.toLowerCase() === 'p') {
          pPrices.push(value);
        }
      }

      const candidatePrices = pPrices.length ? pPrices : allPrices;
      if (!candidatePrices.length) continue;

      rows.push({
        room_name: roomName,
        current_selling: Math.min(...candidatePrices),
        all_detected_prices: Array.from(new Set(allPrices)).sort((a, b) => a - b),
      });
    }

    return rows;
    """
    try:
        rows = driver.execute_script(script)
    except Exception:
        return []
    return rows if isinstance(rows, list) else []


def parse_rooms_with_bs4(html_source: str) -> List[Dict]:
    soup = BeautifulSoup(html_source, "html.parser")
    raw_rooms: List[Dict] = []

    for title in soup.find_all(["h1", "h2", "h3", "h4"]):
        room_name = normalize_room_name(title.get_text(" ", strip=True))
        if not looks_like_room_name(room_name):
            continue

        card = title.find_parent(attrs={"data-scope": "carousel", "data-part": "item"})
        if card is None:
            card = title.find_parent(class_=lambda c: c and "chakra-card__root" in c)
        if card is None:
            card = title.find_parent(["article", "section", "div"])
        if card is None:
            continue

        p_prices: List[int] = []
        all_prices: List[int] = []
        for node in card.find_all(["p", "span", "div", "label"]):
            text = node.get_text(" ", strip=True)
            if not text or not PRICE_RE.search(text):
                continue
            if re.search(r"amenity|fee|tax|total|include", text, re.I):
                continue
            price = parse_price_to_int(text)
            if price is None or price <= 0 or price > 20000:
                continue
            all_prices.append(price)
            if node.name == "p":
                p_prices.append(price)

        candidate_prices = p_prices or all_prices
        if candidate_prices:
            raw_rooms.append(
                {
                    "room_name": room_name,
                    "current_selling": min(candidate_prices),
                    "all_detected_prices": sorted(set(all_prices)),
                }
            )

    return raw_rooms


def scrape_1hotels(url: str, wait_seconds: int = 35, settle_seconds: int = 6) -> Dict:
    driver = None
    try:
        driver = init_driver()
        driver.get(url)

        wait = WebDriverWait(driver, wait_seconds)
        wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))

        try:
            wait.until(
                lambda d: d.execute_script(
                    "return document.body && "
                    "(/\\$\\s*[0-9]/.test(document.body.innerText || '') || "
                    "document.querySelectorAll('h2,h3,h4').length > 0);"
                )
            )
        except TimeoutException:
            pass

        time.sleep(settle_seconds)

        raw_rooms = parse_rooms_with_browser_dom(driver)
        html_source = driver.page_source
        rooms = dedupe_rooms(raw_rooms)

        if not rooms:
            rooms = dedupe_rooms(parse_rooms_with_bs4(html_source))

        page_text = BeautifulSoup(html_source, "html.parser").get_text("\n", strip=True)
        return {
            "ok": True,
            "rooms": rooms,
            "raw_count": len(raw_rooms),
            "page_text_preview": page_text[:3500],
            "html_preview": html_source[:3500],
        }
    finally:
        if driver is not None:
            driver.quit()


def build_output_lines(rooms: List[Dict], discount_percent: float) -> List[str]:
    lines: List[str] = []
    for room in rooms:
        current = int(room["current_selling"])
        best = discount_price(current, discount_percent)
        room_name = room["room_name"]
        lines.append(
            f"▫{room_name} | Best offer: {format_money(best)} per night. "
            f"(Currently selling: {format_money(current)})"
        )
    return lines


def build_output_dataframe(rooms: List[Dict], discount_percent: float) -> pd.DataFrame:
    rows = []
    for room in rooms:
        current = int(room["current_selling"])
        best = discount_price(current, discount_percent)
        rows.append(
            {
                "Room Type": room["room_name"],
                "Best offer": format_money(best),
                "Currently selling": format_money(current),
                "Discount % Off": f"{discount_percent:g}%",
                "Detected prices": ", ".join(format_money(x) for x in room.get("all_detected_prices", [])),
            }
        )
    return pd.DataFrame(rows)


def get_selected_room_lines(rooms: List[Dict], discount_percent: float) -> List[str]:
    selected_rooms = []
    for index, room in enumerate(rooms):
        key = f"room_selected_{index}_{normalize_room_name(room['room_name']).lower()}"
        if st.session_state.get(key, False):
            selected_rooms.append(room)
    return build_output_lines(selected_rooms, discount_percent)


def build_email_body(
    opening: str,
    ending: str,
    checkin: date,
    checkout: date,
    selected_room_lines: List[str],
    rates_include_tax: bool,
) -> str:
    room_nights = max((checkout - checkin).days, 1)
    tax_phrase = "including tax" if rates_include_tax else "excluding tax"

    parts: List[str] = []
    opening_clean = (opening or "").strip()
    ending_clean = (ending or "").strip()

    if opening_clean:
        parts.append(opening_clean)

    details = [
        "Please see options and availability for requested date below:",
        f"Arrival: {checkin.isoformat()}",
        f"Departure: {checkout.isoformat()}",
        f"Room nights: {room_nights}",
        f"Rates are per night {tax_phrase}.",
    ]

    if selected_room_lines:
        details.extend(["", *selected_room_lines])
    else:
        details.extend(["", "No room type selected."])

    parts.append("\n".join(details))

    if ending_clean:
        parts.append(ending_clean)

    return "\n\n".join(parts)


# ============================================================
# UI
# ============================================================
st.title("🏨 Starwood Hotel Rateshop")
st.caption("Secure Streamlit Secrets login, hotel-code dropdown, dynamic date-based URL, selectable room quotes, and email-ready output.")

with st.sidebar:
    st.header("⚙️ Search Settings")
    st.caption(f"App version: {APP_VERSION}")

    if st.button("LOGOUT"):
        save_user_preferences()
        st.session_state.authenticated = False
        st.session_state.pop("authenticated_user_name", None)
        st.rerun()

    hotel_key = st.selectbox(
        "Hotel dropdown menu",
        options=list(HOTEL_CODE_MAP.keys()),
        index=list(HOTEL_CODE_MAP.keys()).index(DEFAULT_HOTEL_KEY),
    )
    hotel_code = HOTEL_CODE_MAP[hotel_key]
    st.text_input("Hotel code", value=hotel_code, disabled=True)

    checkin = st.date_input("Check-in / startDate", value=DEFAULT_CHECKIN)
    default_checkout = max(DEFAULT_CHECKOUT, checkin + timedelta(days=1))
    checkout = st.date_input("Check-out / endDate", value=default_checkout)

    adults = st.number_input("Adults", min_value=1, max_value=10, value=1, step=1)
    children = st.number_input("Children", min_value=0, max_value=10, value=0, step=1)
    discount_percent = st.number_input(
        "% OFF",
        min_value=0.0,
        max_value=100.0,
        value=float(DEFAULT_DISCOUNT_PERCENT),
        step=1.0,
    )
    st.checkbox("Rates include tax", key="rates_include_tax")
    currency = st.selectbox("Currency", options=["USD"], index=0)
    sort = st.selectbox("Sort", options=["low", "high"], index=0)
    group_code = st.text_input("Group Code", value="")
    promo_code = st.text_input("Promo Code", value="")
    wait_seconds = st.slider("Browser wait seconds", 15, 75, 35, 5)

    with st.expander("Chrome runtime check", expanded=False):
        runtime = get_chrome_runtime()
        st.write("Chromium:", runtime.get("chromium_binary") or "not found")
        st.write("Chromedriver:", runtime.get("chromedriver_binary") or "not found")
        st.caption(str(runtime.get("chromium_version") or ""))
        st.caption(str(runtime.get("chromedriver_version") or ""))

if checkout <= checkin:
    st.error("Check-out date must be later than Check-in date.")
    st.stop()

save_user_preferences()

target_url = build_booking_url(
    hotel_code=hotel_code,
    checkin=checkin,
    checkout=checkout,
    adults=int(adults),
    children=int(children),
    currency=currency,
    group_code=group_code,
    promo_code=promo_code,
    sort=sort,
)

url_col, button_col = st.columns([5, 1])
with url_col:
    st.text_area("Dynamic URL", value=target_url, height=88, disabled=True)
with button_col:
    st.write("")
    st.write("")
    search_clicked = st.button("SEARCH", type="primary", use_container_width=True)
    email_clicked = st.button("EMAIL", use_container_width=True)

rate_tax_word = "including" if st.session_state.rates_include_tax else "excluding"
st.markdown(
    "**Rates are fully pre-paid and non-refundable.** "
    f"**{discount_percent:g}% OFF** · **Rates {rate_tax_word} tax**"
)

if "last_output_text" not in st.session_state:
    st.session_state.last_output_text = ""
if "last_df" not in st.session_state:
    st.session_state.last_df = pd.DataFrame()
if "last_rooms" not in st.session_state:
    st.session_state.last_rooms = []
if "last_error" not in st.session_state:
    st.session_state.last_error = ""
if "generated_email" not in st.session_state:
    st.session_state.generated_email = ""

if search_clicked:
    with st.spinner("Starting the headless browser and fetching live rates. This usually takes 20-45 seconds..."):
        try:
            result = scrape_1hotels(target_url, wait_seconds=int(wait_seconds))
            rooms = result.get("rooms", [])
            st.session_state.last_error = ""

            if not rooms:
                st.session_state.last_output_text = ""
                st.session_state.last_df = pd.DataFrame()
                st.session_state.last_rooms = []
                st.error("No room rates were parsed. The page structure may have changed, the rate component may not have loaded, or anti-bot verification may have been triggered.")
                with st.expander("Debug: page text preview"):
                    st.text(result.get("page_text_preview", "")[:3500])
                with st.expander("Debug: HTML preview"):
                    st.code(result.get("html_preview", "")[:3500], language="html")
            else:
                output_lines = build_output_lines(rooms, discount_percent)
                st.session_state.last_output_text = "\n".join(output_lines)
                st.session_state.last_df = build_output_dataframe(rooms, discount_percent)
                st.session_state.last_rooms = rooms
                st.session_state.generated_email = ""
                for index, room in enumerate(rooms):
                    room_key = f"room_selected_{index}_{normalize_room_name(room['room_name']).lower()}"
                    st.session_state[room_key] = False
                st.success(f"Search completed: parsed {len(rooms)} room type(s).")
        except Exception as exc:
            st.session_state.last_error = str(exc)
            st.error(f"Browser startup or runtime failed: {exc}")
            with st.expander("Debug: Chrome runtime diagnostics", expanded=True):
                st.json(get_chrome_runtime())
            st.warning(
                "Important: if the error still shows /home/appuser/.cache/selenium/chromedriver, "
                "Streamlit is still running an older app.py, or the app was not rebooted successfully."
            )

left, right = st.columns([1.15, 1])
with left:
    st.subheader("Room Type Selection")
    rooms_for_selection = st.session_state.last_rooms
    if rooms_for_selection:
        st.caption("Select the room type(s) you want to include in the email quote.")
        select_all_col, clear_all_col = st.columns(2)
        with select_all_col:
            if st.button("Select all room types", use_container_width=True):
                for index, room in enumerate(rooms_for_selection):
                    room_key = f"room_selected_{index}_{normalize_room_name(room['room_name']).lower()}"
                    st.session_state[room_key] = True
                st.rerun()
        with clear_all_col:
            if st.button("Clear selections", use_container_width=True):
                for index, room in enumerate(rooms_for_selection):
                    room_key = f"room_selected_{index}_{normalize_room_name(room['room_name']).lower()}"
                    st.session_state[room_key] = False
                st.rerun()

        for index, room in enumerate(rooms_for_selection):
            line = build_output_lines([room], discount_percent)[0]
            room_key = f"room_selected_{index}_{normalize_room_name(room['room_name']).lower()}"
            st.checkbox(line, key=room_key)
    else:
        st.info("Click SEARCH to load room types and live rates here.")

with right:
    st.subheader("Structured Result")
    if not st.session_state.last_df.empty:
        st.dataframe(st.session_state.last_df, use_container_width=True, hide_index=True)
        csv_data = st.session_state.last_df.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            label="Download CSV",
            data=csv_data,
            file_name=f"starwood_{hotel_key}_{checkin}_{checkout}.csv",
            mime="text/csv",
            use_container_width=True,
        )
    else:
        st.info("Click SEARCH to load room types and live rates here.")

st.divider()

email_left, email_right = st.columns([1, 1])
with email_left:
    st.subheader("Email Opening")
    st.text_area(
        "Opening",
        key="email_opening",
        height=150,
        placeholder="Type the email opening here. It will be saved for your next login while the app cache is alive.",
        label_visibility="collapsed",
    )

with email_right:
    st.subheader("Email Ending")
    st.text_area(
        "Ending",
        key="email_ending",
        height=150,
        label_visibility="collapsed",
    )

save_user_preferences()

if email_clicked:
    selected_lines = get_selected_room_lines(st.session_state.last_rooms, discount_percent)
    st.session_state.generated_email = build_email_body(
        opening=st.session_state.email_opening,
        ending=st.session_state.email_ending,
        checkin=checkin,
        checkout=checkout,
        selected_room_lines=selected_lines,
        rates_include_tax=bool(st.session_state.rates_include_tax),
    )
    save_user_preferences()

st.subheader("Generated Email")
st.text_area(
    "Email Output",
    value=st.session_state.generated_email,
    height=420,
    label_visibility="collapsed",
)

with st.expander("Deployment files for Streamlit Cloud"):
    st.markdown("**requirements.txt**")
    st.code(
        """
streamlit
selenium
beautifulsoup4
pandas
        """.strip(),
        language="text",
    )
    st.markdown("**packages.txt**")
    st.code(
        """
chromium
chromium-driver
        """.strip(),
        language="text",
    )
    st.markdown("**Streamlit Secrets**")
    st.code(
        """
user_name = 123
password = 456
        """.strip(),
        language="toml",
    )
