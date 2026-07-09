import html
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import date, timedelta
from typing import Dict, List, Optional
from urllib.parse import urlencode

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
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
APP_VERSION = "2026-07-09 Starwood Hotel Rateshop strict-card-parser-app-ready-reload"

# ============================================================
# Hotel map: dropdown label -> Starwood booking hotel code
# ============================================================
HOTEL_CODE_MAP: Dict[str, Dict[str, str]] = {
    "1SB": {"code": "60507", "currency_symbol": "$"},
    "1CP": {"code": "60735", "currency_symbol": "$"},
    "1BB": {"code": "66266", "currency_symbol": "$"},
    "1TY": {"code": "96185", "currency_symbol": "¥"},
    "1ML": {"code": "47157", "currency_symbol": "$"},
    "1MF": {"code": "40333", "currency_symbol": "£"},
    "1HNB": {"code": "5826", "currency_symbol": "$"},
    "1CPH": {"code": "41069", "currency_symbol": "kr."},
    "1NV": {"code": "35903", "currency_symbol": "$"},
    "1SF": {"code": "36017", "currency_symbol": "$"},
    "1SE": {"code": "47314", "currency_symbol": "$"},
    "1TO": {"code": "31116", "currency_symbol": "$"},
    "1WH": {"code": "77961", "currency_symbol": "$"},
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

# Text that can appear inside a room card but is not a room type.
# This prevents rate labels, warnings, CTA text, and availability badges from
# being treated as room names simply because they contain the word "room".
ROOM_NAME_BLOCKLIST_RE = re.compile(
    r"(?:"
    r"price\s+is\s+subject\s+to\s+change|"
    r"must\s+be\s+18|"
    r"room\s+left|"
    r"rooms?\s+left|"
    r"select\s+room|"
    r"available\s+rates?|"
    r"avg\s*/?\s*night|"
    r"average\s+size|"
    r"non[-\s]?refundable|"
    r"flexible\s+cancellation|"
    r"all\s+rates\s+include|"
    r"amenity\s+fee|"
    r"per\s+night|"
    r"best\s+offer|"
    r"currently\s+selling"
    r")",
    re.I,
)

PRICE_RE = re.compile(r"(?P<symbol>[$€£¥₹₩₪₫₱฿₦₵₡₲₴₺₽]|USD|CAD|AUD|EUR|GBP|kr\.?)\s*(?P<amount>[0-9][0-9,]*)", re.I)

st.set_page_config(page_title="Starwood Hotel Rateshop", layout="wide")

st.markdown(
    """
    <style>
    div[data-testid="stCheckbox"] label p {
        font-size: 18px !important;
        font-weight: 700 !important;
        line-height: 1.35 !important;
    }
    div[data-testid="stCheckbox"] label {
        align-items: flex-start !important;
        gap: 0.45rem !important;
    }
    div[data-testid="stCheckbox"] {
        margin-bottom: 0.25rem !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


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
# Browser-local user preference cache
# Email templates are intentionally stored in each user's browser localStorage,
# not in Streamlit server cache. This prevents one logged-in user from
# overriding another user's Email Opening / Email Ending template.
# ============================================================
def default_ending_text() -> str:
    valid_until = date.today() + timedelta(days=3)
    return (
        f"This quote is valid until {valid_until.isoformat()}. Change in stay date will result in new pricing.\n"
        "Rates are fully pre-paid and non-refundable. 100% room, tax and resort fee are charged at time of booking.\n\n"
        "Please let us know which room type would you like to choose."
    )


def get_browser_storage_namespace() -> str:
    """Return the browser-local namespace used for this app's saved preferences."""
    return "starwood_rateshop_email_template_v1"


def render_local_storage_loader() -> None:
    """
    Load browser localStorage into temporary URL query params once per browser tab.

    Streamlit Python cannot directly read browser localStorage. This small JS
    bridge reads localStorage, writes temporary query params, and reloads once.
    Python then consumes the query params into st.session_state.
    """
    namespace = get_browser_storage_namespace()

    components.html(
        f"""
        <script>
        const namespace = {json.dumps(namespace)};
        const openingKey = namespace + "_email_opening";
        const endingKey = namespace + "_email_ending";
        const taxKey = namespace + "_rates_include_tax";

        const parentWindow = window.parent;
        const params = new URLSearchParams(parentWindow.location.search);

        if (!params.has("browser_template_loaded")) {{
            const opening = parentWindow.localStorage.getItem(openingKey) || "";
            const ending = parentWindow.localStorage.getItem(endingKey) || "";
            const tax = parentWindow.localStorage.getItem(taxKey) || "";

            params.set("browser_template_loaded", "1");
            params.set("browser_email_opening", opening);
            params.set("browser_email_ending", ending);
            params.set("browser_rates_include_tax", tax);

            const newUrl = parentWindow.location.pathname + "?" + params.toString();
            parentWindow.history.replaceState(null, "", newUrl);
            parentWindow.location.reload();
        }}
        </script>
        """,
        height=0,
    )


def consume_browser_template_from_query_params() -> None:
    """Move browser-loaded template values from query params into session_state."""
    params = st.query_params

    if "browser_template_consumed" in st.session_state:
        return

    # Wait until the JavaScript loader has copied browser localStorage into
    # query params. This prevents Streamlit from rendering text_area widgets
    # with default values before browser values arrive.
    if params.get("browser_template_loaded", "") != "1":
        st.stop()

    browser_opening = params.get("browser_email_opening", "")
    browser_ending = params.get("browser_email_ending", "")
    browser_tax = params.get("browser_rates_include_tax", "")

    st.session_state.email_opening = browser_opening or ""
    st.session_state.email_ending = browser_ending or default_ending_text()
    st.session_state.rates_include_tax = str(browser_tax).lower() == "true"
    st.session_state.browser_template_consumed = True


def render_local_storage_saver(opening: str, ending: str, rates_include_tax: bool) -> None:
    """Save the current template into this browser's localStorage only."""
    namespace = get_browser_storage_namespace()

    components.html(
        f"""
        <script>
        const namespace = {json.dumps(namespace)};
        const parentWindow = window.parent;
        parentWindow.localStorage.setItem(namespace + "_email_opening", {json.dumps(opening or "")});
        parentWindow.localStorage.setItem(namespace + "_email_ending", {json.dumps(ending or "")});
        parentWindow.localStorage.setItem(namespace + "_rates_include_tax", {json.dumps(str(bool(rates_include_tax)).lower())});
        </script>
        <div style="
            padding: 10px 12px;
            border-radius: 8px;
            border: 1px solid #d8ead8;
            background: #f3fbf3;
            color: #1d5f1d;
            font-weight: 700;
            font-family: sans-serif;
        ">
            Template saved to this browser.
        </div>
        """,
        height=52,
    )




render_local_storage_loader()
consume_browser_template_from_query_params()


# ============================================================
# Hotel config helpers
# ============================================================
def get_hotel_code(hotel_key: str) -> str:
    return str(HOTEL_CODE_MAP[hotel_key]["code"])


def get_hotel_currency_symbol(hotel_key: str) -> str:
    return str(HOTEL_CODE_MAP[hotel_key].get("currency_symbol") or "$")


def apply_hotel_currency_symbol(rooms: List[Dict], hotel_key: str) -> List[Dict]:
    """Use the configured hotel currency symbol for all displayed quotes and email output."""
    currency_symbol = get_hotel_currency_symbol(hotel_key)
    updated_rooms: List[Dict] = []
    for room in rooms:
        updated_room = dict(room)
        updated_room["currency_symbol"] = currency_symbol
        updated_rooms.append(updated_room)
    return updated_rooms


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


def build_chrome_options(chromium_binary: str, fallback_mode: bool = False) -> Options:
    chrome_options = Options()
    chrome_options.binary_location = chromium_binary
    # Do not wait for every image/tracking request. The booking page body loads fast,
    # while live prices arrive shortly after via JavaScript. Eager keeps driver.get()
    # from blocking unnecessarily.
    chrome_options.page_load_strategy = "eager"

    # Use a fresh Chrome profile on every attempt. This avoids profile-lock issues on
    # Streamlit Cloud when a previous browser process exits slowly or crashes.
    user_data_dir = tempfile.mkdtemp(prefix="starwood_chrome_profile_")

    # Keep Chrome closer to a normal browser. The Selfbook React app can fail to
    # hydrate in headless Chrome when --single-process or a fixed debugging port is
    # used on Streamlit Cloud. Use a random debugging port for every run and avoid
    # --single-process.
    if fallback_mode:
        chrome_options.add_argument("--headless")
        chrome_options.add_argument("--disable-features=VizDisplayCompositor")
    else:
        chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--remote-debugging-port=0")

    chrome_options.add_argument(f"--user-data-dir={user_data_dir}")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-software-rasterizer")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-default-apps")
    chrome_options.add_argument("--disable-sync")
    chrome_options.add_argument("--metrics-recording-only")
    chrome_options.add_argument("--mute-audio")
    chrome_options.add_argument("--no-first-run")
    chrome_options.add_argument("--disable-setuid-sandbox")
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


def init_driver(fallback_mode: bool = False) -> webdriver.Chrome:
    runtime = validate_chrome_runtime()
    chromium_binary = str(runtime["chromium_binary"])
    chromedriver_binary = str(runtime["chromedriver_binary"])

    # CRITICAL: always pass Service(executable_path=...).
    # Do not call webdriver.Chrome(options=...), because that invokes Selenium Manager.
    service = Service(executable_path=chromedriver_binary)
    chrome_options = build_chrome_options(chromium_binary, fallback_mode=fallback_mode)
    driver = webdriver.Chrome(service=service, options=chrome_options)
    # The user observed that the page shell normally loads in about 7 seconds.
    # Keep this short, then poll the DOM for prices instead of waiting for full page load.
    driver.set_page_load_timeout(12 if fallback_mode else 10)
    return driver


# ============================================================
# Parsing helpers
# ============================================================
def parse_price_match(text: str) -> Optional[Dict[str, object]]:
    if not text:
        return None
    match = PRICE_RE.search(text.replace("\xa0", " "))
    if not match:
        return None
    try:
        amount = int(match.group("amount").replace(",", ""))
    except ValueError:
        return None
    symbol = str(match.group("symbol") or "$")
    return {"amount": amount, "symbol": symbol}


def parse_price_to_int(text: str) -> Optional[int]:
    parsed = parse_price_match(text)
    if not parsed:
        return None
    return int(parsed["amount"])


def normalize_room_name(name: str) -> str:
    return re.sub(r"\s+", " ", name or "").strip()


def looks_like_room_name(name: str) -> bool:
    cleaned = normalize_room_name(name)
    if len(cleaned) < 4 or len(cleaned) > 90:
        return False
    if PRICE_RE.search(cleaned):
        return False
    if ROOM_NAME_BLOCKLIST_RE.search(cleaned):
        return False
    lower_name = cleaned.lower()
    return any(hint in lower_name for hint in ROOM_NAME_HINTS)


def discount_price(current_price: int, discount_percent: float) -> int:
    discounted = current_price * (1 - discount_percent / 100)
    return int(round(discounted))


def format_money(value: int, currency_symbol: str = "$") -> str:
    symbol = currency_symbol or "$"
    return f"{symbol}{value:,}"


def escape_streamlit_label(text: str) -> str:
    # Streamlit checkbox labels render Markdown, so a literal dollar sign can be
    # interpreted as a math delimiter. Escaping it keeps the scraped currency
    # symbol visible in Room Type Selection while preserving the normal symbol
    # in generated emails and CSV output.
    return (text or "").replace("$", r"\$")


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
                "currency_symbol": str(room.get("currency_symbol") or "$"),
                "all_detected_prices": sorted(set(room.get("all_detected_prices", [current_price]))),
            }

    return sorted(best_by_room.values(), key=lambda item: (item["current_selling"], item["room_name"]))


def parse_rooms_with_browser_dom(driver: webdriver.Chrome) -> List[Dict]:
    """
    Parse room-card prices from the current browsing context, including open shadow DOM.

    Important: do not infer room names from arbitrary text inside the card. The 1 Hotels
    booking UI contains labels such as "1 Room Left!", "Select Room", and policy copy
    inside the same card. Those contain the word "room" and can be near prices, so they
    must never be used as room type titles.
    """
    script = r"""
    const priceRegex = /([$€£¥₹₩₪₫₱฿₦₵₡₲₴₺₽]|USD|CAD|AUD|EUR|GBP|kr\.?)\s*([0-9][0-9,]*)/i;
    const roomRegex = /(room|king|queen|suite|studio|home|ocean|city|skyline|two|one|balcony|connecting)/i;
    const blockedTitleRegex = /(price\s+is\s+subject\s+to\s+change|must\s+be\s+18|rooms?\s+left|select\s+room|available\s+rates?|avg\s*\/?\s*night|average\s+size|non[-\s]?refundable|flexible\s+cancellation|all\s+rates\s+include|amenity\s+fee|per\s+night|best\s+offer|currently\s+selling)/i;
    const rows = [];

    function cleanText(value) {
      return (value || '').replace(/\s+/g, ' ').trim();
    }

    function elementText(element) {
      return cleanText(element ? (element.innerText || element.textContent || '') : '');
    }

    function collectElements(root) {
      const out = [];
      const seen = new Set();
      function walk(node) {
        if (!node || seen.has(node)) return;
        seen.add(node);
        if (node.nodeType === Node.ELEMENT_NODE) {
          out.push(node);
          if (node.shadowRoot) walk(node.shadowRoot);
        }
        const children = node.children ? Array.from(node.children) : [];
        for (const child of children) walk(child);
      }
      walk(root || document.body || document.documentElement);
      return out;
    }

    function isRoomTitle(value) {
      const text = cleanText(value);
      if (text.length < 4 || text.length > 90) return false;
      if (priceRegex.test(text)) return false;
      if (blockedTitleRegex.test(text)) return false;
      return roomRegex.test(text);
    }

    function nearestRoomCard(titleNode) {
      if (!titleNode) return null;
      const selectors = [
        '[data-scope="carousel"][data-part="item"]',
        '.chakra-card__root',
        '[class*="chakra-card"]',
        'article',
        'section'
      ];
      for (const selector of selectors) {
        try {
          const card = titleNode.closest(selector);
          if (card) return card;
        } catch (error) {}
      }
      let node = titleNode.parentElement || (titleNode.getRootNode && titleNode.getRootNode().host) || null;
      for (let depth = 0; node && depth < 6; depth += 1) {
        const text = elementText(node);
        if (priceRegex.test(text) && text.length >= 30 && text.length <= 4500) return node;
        node = node.parentElement || (node.getRootNode && node.getRootNode().host) || null;
      }
      return null;
    }

    function findRoomTitle(card) {
      const cardElements = collectElements(card);

      // Primary selector from the current 1 Hotels/Selfbook UI:
      // <h3 class="chakra-card__title ...">City View King</h3>
      const titleSelectors = [
        'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
        '[role="heading"]',
        '[class*="card__title"]',
        '[class*="card_title"]'
      ];
      const seen = new Set();
      const candidates = [];
      for (const selector of titleSelectors) {
        try {
          for (const node of card.querySelectorAll(selector)) {
            if (!seen.has(node)) {
              seen.add(node);
              candidates.push(node);
            }
          }
        } catch (error) {}
      }

      for (const node of candidates) {
        const text = elementText(node);
        if (isRoomTitle(text)) return text;
      }
      return '';
    }

    function collectCandidateCards() {
      const allElements = collectElements(document.body || document.documentElement);
      const cards = [];
      const seenCards = new Set();
      const titleNodes = allElements.filter(el => {
        const tagName = String(el.tagName || '').toUpperCase();
        const isHeading = /^(H1|H2|H3|H4|H5|H6)$/.test(tagName) || el.getAttribute('role') === 'heading' || /card__title|card_title/i.test(String(el.className || ''));
        return isHeading && isRoomTitle(elementText(el));
      });

      for (const titleNode of titleNodes) {
        const card = nearestRoomCard(titleNode);
        if (!card || seenCards.has(card)) continue;
        const text = elementText(card);
        if (!priceRegex.test(text)) continue;
        seenCards.add(card);
        cards.push(card);
      }
      return cards;
    }

    function parsePrices(card) {
      const priceNodes = collectElements(card).filter(el => /^(P|SPAN|DIV|LABEL|BUTTON|A|LI)$/i.test(el.tagName || ''));
      const allPrices = [];
      const sellingPrices = [];

      for (const node of priceNodes) {
        const text = elementText(node);
        const match = text.match(priceRegex);
        if (!match) continue;

        // Ignore fees/taxes/policy text and crossed-out/original price elements.
        if (/amenity|fee|tax|total|include|included|resort|destination|deposit|due now/i.test(text)) continue;
        const style = window.getComputedStyle ? window.getComputedStyle(node) : null;
        const decoration = style ? String(style.textDecorationLine || style.textDecoration || '') : '';
        if (/line-through/i.test(decoration)) continue;
        if (node.closest && node.closest('s, strike, del')) continue;

        const symbol = match[1] || '$';
        const value = parseInt(String(match[2] || '').replace(/,/g, ''), 10);
        if (!Number.isFinite(value) || value <= 0 || value > 20000) continue;

        const item = {value, symbol};
        allPrices.push(item);

        const tagName = (node.tagName || '').toLowerCase();
        const parentText = elementText(node.parentElement || node);
        const nodeClass = String(node.className || '');
        const parentClass = String((node.parentElement && node.parentElement.className) || '');

        // Prefer the visible selling-rate text. Avoid discount badges like "34% off"
        // and avoid large container text that bundles multiple rates together.
        if (
          text.length <= 80 &&
          !/%\s*off/i.test(text) &&
          !/was|original|strike/i.test(text) &&
          (
            tagName === 'p' ||
            /avg\s*\/\s*night|average|night|rate/i.test(parentText) ||
            /price|rate/i.test(nodeClass + ' ' + parentClass)
          )
        ) {
          sellingPrices.push(item);
        }
      }
      return {allPrices, sellingPrices};
    }

    for (const card of collectCandidateCards()) {
      const roomName = findRoomTitle(card);
      if (!roomName) continue;

      const parsed = parsePrices(card);
      const candidatePrices = parsed.sellingPrices.length ? parsed.sellingPrices : parsed.allPrices;
      if (!candidatePrices.length) continue;

      const bestPrice = candidatePrices.reduce((best, item) => item.value < best.value ? item : best, candidatePrices[0]);
      rows.push({
        room_name: roomName,
        current_selling: bestPrice.value,
        currency_symbol: bestPrice.symbol || '$',
        all_detected_prices: Array.from(new Set(parsed.allPrices.map(item => item.value))).sort((a, b) => a - b),
      });
    }

    return rows;
    """
    try:
        rows = driver.execute_script(script)
    except Exception:
        return []
    return rows if isinstance(rows, list) else []

def current_context_has_price_text(driver: webdriver.Chrome) -> bool:
    """Return True if the current document or any open shadow root contains a price-looking string."""
    script = r"""
    const priceRegex = /([$€£¥₹₩₪₫₱฿₦₵₡₲₴₺₽]|USD|CAD|AUD|EUR|GBP|kr\.?)\s*([0-9][0-9,]*)/i;
    function collectText(root) {
      let text = '';
      const seen = new Set();
      function walk(node) {
        if (!node || seen.has(node)) return;
        seen.add(node);
        if (node.nodeType === Node.TEXT_NODE) {
          text += ' ' + (node.nodeValue || '');
          return;
        }
        if (node.nodeType !== Node.ELEMENT_NODE && node.nodeType !== Node.DOCUMENT_NODE && node.nodeType !== Node.DOCUMENT_FRAGMENT_NODE) return;
        if (node.shadowRoot) walk(node.shadowRoot);
        const children = node.childNodes ? Array.from(node.childNodes) : [];
        for (const child of children) walk(child);
      }
      walk(root || document.body || document.documentElement);
      return text;
    }
    return priceRegex.test(collectText(document.body || document.documentElement));
    """
    try:
        return bool(driver.execute_script(script))
    except Exception:
        return False



def get_booking_app_state(driver: webdriver.Chrome) -> Dict[str, object]:
    """Inspect whether the React/Selfbook booking app has actually hydrated."""
    script = r"""
    const priceRegex = /([$€£¥₹₩₪₫₱฿₦₵₡₲₴₺₽]|USD|CAD|AUD|EUR|GBP|kr\.?)\s*([0-9][0-9,]*)/i;
    const roomTitleRegex = /(king|queen|suite|studio|home|ocean|city|skyline|balcony|connecting|two\s+queens|two\s+kings|one\s+bedroom)/i;
    const blockedRegex = /(price\s+is\s+subject\s+to\s+change|must\s+be\s+18|rooms?\s+left|select\s+room|available\s+rates?|avg\s*\/?\s*night|average\s+size|non[-\s]?refundable|flexible\s+cancellation|all\s+rates\s+include|amenity\s+fee|per\s+night)/i;

    function cleanText(value) {
      return (value || '').replace(/\s+/g, ' ').trim();
    }

    function collectElements(root) {
      const out = [];
      const seen = new Set();
      function walk(node) {
        if (!node || seen.has(node)) return;
        seen.add(node);
        if (node.nodeType === Node.ELEMENT_NODE) {
          out.push(node);
          if (node.shadowRoot) walk(node.shadowRoot);
        }
        const children = node.children ? Array.from(node.children) : [];
        for (const child of children) walk(child);
      }
      walk(root || document.body || document.documentElement);
      return out;
    }

    const root = document.querySelector('#root');
    const bodyText = cleanText(document.body ? (document.body.innerText || document.body.textContent || '') : '');
    const elements = collectElements(document.body || document.documentElement);
    const titleNodes = elements.filter(el => {
      const tagName = String(el.tagName || '').toUpperCase();
      const className = String(el.className || '');
      const role = el.getAttribute ? el.getAttribute('role') : '';
      const isTitleNode = /^(H1|H2|H3|H4|H5|H6)$/.test(tagName) || role === 'heading' || /card__title|card_title/i.test(className);
      if (!isTitleNode) return false;
      const text = cleanText(el.innerText || el.textContent || '');
      return text.length >= 4 && text.length <= 90 && roomTitleRegex.test(text) && !blockedRegex.test(text) && !priceRegex.test(text);
    });
    const cards = elements.filter(el => {
      const className = String(el.className || '');
      const dataScope = el.getAttribute ? el.getAttribute('data-scope') : '';
      const dataPart = el.getAttribute ? el.getAttribute('data-part') : '';
      return (dataScope === 'carousel' && dataPart === 'item') || /chakra-card__root|chakra-card/i.test(className);
    });

    return {
      url: String(location.href || ''),
      readyState: String(document.readyState || ''),
      rootExists: !!root,
      rootChildCount: root ? root.children.length : 0,
      bodyTextLength: bodyText.length,
      bodyPreview: bodyText.slice(0, 700),
      titleCount: titleNodes.length,
      cardCount: cards.length,
      priceTextFound: priceRegex.test(bodyText),
      titlePreview: titleNodes.slice(0, 8).map(el => cleanText(el.innerText || el.textContent || '')),
    };
    """
    try:
        state = driver.execute_script(script)
        return state if isinstance(state, dict) else {}
    except Exception as exc:
        return {"error": str(exc)}


def wait_for_booking_app_ready(driver: webdriver.Chrome, max_seconds: float = 18.0) -> Dict[str, object]:
    """
    Wait for the React booking app to hydrate, not just for <body> to exist.

    The failed long-date screenshot shows <body> with an empty #root and only Chakra
    portal/select nodes. In that state Selenium sees body_seen=True, but there is no
    bookable room DOM to parse. This function waits for real room card titles/prices.
    """
    start_time = time.monotonic()
    states: List[Dict[str, object]] = []
    last_state: Dict[str, object] = {}

    while time.monotonic() - start_time <= max_seconds:
        try:
            driver.switch_to.default_content()
        except Exception:
            pass
        state = get_booking_app_state(driver)
        last_state = state
        states.append(state)

        title_count = int(state.get("titleCount", 0) or 0)
        card_count = int(state.get("cardCount", 0) or 0)
        price_found = bool(state.get("priceTextFound", False))
        root_child_count = int(state.get("rootChildCount", 0) or 0)
        body_text_length = int(state.get("bodyTextLength", 0) or 0)

        if title_count >= 1 and (card_count >= 1 or price_found):
            break
        if root_child_count > 0 and body_text_length > 1200 and (title_count >= 1 or price_found):
            break

        # A small scroll/clickless nudge helps Chakra carousel content mount after the app shell hydrates.
        try:
            scroll_booking_page_once(driver, len(states))
        except Exception:
            pass
        time.sleep(0.75)

    return {
        "elapsed_seconds": round(time.monotonic() - start_time, 2),
        "last_state": last_state,
        "samples": states[-4:],
    }


def reload_if_booking_root_is_empty(driver: webdriver.Chrome, wait_seconds: int, app_ready_result: Dict[str, object]) -> Dict[str, object]:
    """Reload once when the booking app shell is stuck with an empty #root."""
    last_state = app_ready_result.get("last_state", {}) if isinstance(app_ready_result, dict) else {}
    root_exists = bool(last_state.get("rootExists", False))
    root_child_count = int(last_state.get("rootChildCount", 0) or 0)
    title_count = int(last_state.get("titleCount", 0) or 0)
    price_found = bool(last_state.get("priceTextFound", False))

    if root_exists and root_child_count == 0 and title_count == 0 and not price_found:
        try:
            driver.refresh()
        except TimeoutException:
            pass
        except Exception:
            pass
        try:
            WebDriverWait(driver, max(5, int(wait_seconds))).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
        except TimeoutException:
            pass
        retry_ready = wait_for_booking_app_ready(driver, max_seconds=max(12.0, min(float(wait_seconds), 35.0)))
        retry_ready["reloaded_empty_root"] = True
        return retry_ready

    app_ready_result["reloaded_empty_root"] = False
    return app_ready_result

def count_current_context_iframes(driver: webdriver.Chrome) -> int:
    try:
        return len(driver.find_elements(By.CSS_SELECTOR, "iframe"))
    except Exception:
        return 0


def collect_rooms_from_all_browser_contexts(driver: webdriver.Chrome, max_depth: int = 4) -> Dict[str, object]:
    """
    Parse prices from the top document and nested iframes.

    1 Hotels booking uses the Selfbook script. In headless Chrome the top page can
    look loaded while the actual rate UI is inside an iframe or an open shadow DOM.
    Scraping only the default document can therefore produce body_seen=True but
    seen_price_text=False.
    """
    raw_rooms: List[Dict] = []
    contexts_checked = 0
    iframe_count = 0
    frame_errors: List[str] = []
    seen_price_text = False

    def visit(depth: int) -> None:
        nonlocal contexts_checked, iframe_count, seen_price_text
        contexts_checked += 1

        try:
            if current_context_has_price_text(driver):
                seen_price_text = True
        except Exception:
            pass

        try:
            raw_rooms.extend(parse_rooms_with_browser_dom(driver))
        except Exception as exc:
            frame_errors.append(f"parse depth {depth}: {exc}")

        if depth >= max_depth:
            return

        try:
            frames = driver.find_elements(By.CSS_SELECTOR, "iframe")
        except Exception as exc:
            frame_errors.append(f"iframe lookup depth {depth}: {exc}")
            return

        iframe_count += len(frames)
        for index in range(len(frames)):
            try:
                frames = driver.find_elements(By.CSS_SELECTOR, "iframe")
                driver.switch_to.frame(frames[index])
                visit(depth + 1)
                driver.switch_to.parent_frame()
            except Exception as exc:
                frame_errors.append(f"iframe depth {depth} index {index}: {exc}")
                try:
                    driver.switch_to.parent_frame()
                except Exception:
                    try:
                        driver.switch_to.default_content()
                    except Exception:
                        pass

    try:
        driver.switch_to.default_content()
    except Exception:
        pass
    visit(0)
    try:
        driver.switch_to.default_content()
    except Exception:
        pass

    return {
        "raw_rooms": raw_rooms,
        "rooms": dedupe_rooms(raw_rooms),
        "contexts_checked": contexts_checked,
        "iframe_count": iframe_count,
        "frame_errors": frame_errors[:8],
        "seen_price_text": seen_price_text,
    }


def collect_page_sources_from_all_contexts(driver: webdriver.Chrome, max_depth: int = 3) -> List[str]:
    sources: List[str] = []

    def visit(depth: int) -> None:
        try:
            sources.append(driver.page_source)
        except Exception:
            pass
        if depth >= max_depth:
            return
        try:
            frames = driver.find_elements(By.CSS_SELECTOR, "iframe")
        except Exception:
            return
        for index in range(len(frames)):
            try:
                frames = driver.find_elements(By.CSS_SELECTOR, "iframe")
                driver.switch_to.frame(frames[index])
                visit(depth + 1)
                driver.switch_to.parent_frame()
            except Exception:
                try:
                    driver.switch_to.parent_frame()
                except Exception:
                    try:
                        driver.switch_to.default_content()
                    except Exception:
                        pass

    try:
        driver.switch_to.default_content()
    except Exception:
        pass
    visit(0)
    try:
        driver.switch_to.default_content()
    except Exception:
        pass
    return sources

def parse_rooms_with_bs4(html_source: str) -> List[Dict]:
    """
    Conservative fallback parser for final page_source only.

    It intentionally reads room names only from heading/card title nodes. It does not
    infer room names from arbitrary text because policy copy and CTA labels can contain
    the word "room" and contaminate the result.
    """
    soup = BeautifulSoup(html_source, "html.parser")
    raw_rooms: List[Dict] = []

    title_nodes = []
    for selector in ["h1", "h2", "h3", "h4", "h5", "h6", '[role="heading"]']:
        title_nodes.extend(soup.select(selector))
    title_nodes.extend(
        node for node in soup.find_all(class_=lambda c: c and ("card__title" in str(c) or "card_title" in str(c)))
        if node not in title_nodes
    )

    for title in title_nodes:
        room_name = normalize_room_name(title.get_text(" ", strip=True))
        if not looks_like_room_name(room_name):
            continue

        card = title.find_parent(attrs={"data-scope": "carousel", "data-part": "item"})
        if card is None:
            card = title.find_parent(class_=lambda c: c and "chakra-card__root" in str(c))
        if card is None:
            card = title.find_parent(class_=lambda c: c and "chakra-card" in str(c))
        if card is None:
            card = title.find_parent(["article", "section"])
        if card is None:
            continue

        selling_prices: List[Dict[str, object]] = []
        all_prices: List[Dict[str, object]] = []
        for node in card.find_all(["p", "span", "label", "button", "a", "li"]):
            text = node.get_text(" ", strip=True)
            if not text or not PRICE_RE.search(text):
                continue
            if re.search(r"amenity|fee|tax|total|include|included|resort|destination|deposit|due now", text, re.I):
                continue
            if node.find_parent(["s", "strike", "del"]):
                continue
            style_value = " ".join(str(node.get(attr, "")) for attr in ["style", "class"])
            if re.search(r"line-through|strike|original", style_value, re.I):
                continue
            parsed_price = parse_price_match(text)
            if not parsed_price:
                continue
            price = int(parsed_price["amount"])
            if price <= 0 or price > 20000:
                continue
            all_prices.append(parsed_price)
            if len(text) <= 90 and not re.search(r"%\s*off|was|original|strike", text, re.I):
                selling_prices.append(parsed_price)

        candidate_prices = selling_prices or all_prices
        if candidate_prices:
            best_price = min(candidate_prices, key=lambda item: int(item["amount"]))
            raw_rooms.append(
                {
                    "room_name": room_name,
                    "current_selling": int(best_price["amount"]),
                    "currency_symbol": str(best_price.get("symbol") or "$"),
                    "all_detected_prices": sorted(set(int(item["amount"]) for item in all_prices)),
                }
            )

    return raw_rooms


def scroll_booking_page_once(driver: webdriver.Chrome, step_index: int) -> None:
    """Scroll enough to trigger lazy-loaded room cards and async price nodes."""
    try:
        driver.execute_script(
            """
            const height = Math.max(
              document.body ? document.body.scrollHeight : 0,
              document.documentElement ? document.documentElement.scrollHeight : 0
            );
            const positions = [0, 0.18, 0.36, 0.54, 0.72, 0.9, 1.0, 0.55, 0.25];
            const ratio = positions[step_index % positions.length];
            window.scrollTo({top: Math.floor(height * ratio), behavior: 'instant'});
            """
        )
    except Exception:
        pass


def warm_up_lazy_loaded_rates(driver: webdriver.Chrome) -> None:
    """Do one quick full-page scroll sweep before parsing prices."""
    for index in range(7):
        scroll_booking_page_once(driver, index)
        time.sleep(0.25)
    try:
        driver.execute_script("window.scrollTo(0, 0);")
    except Exception:
        pass


def warm_up_lazy_loaded_rates_all_contexts(driver: webdriver.Chrome, max_depth: int = 3) -> Dict[str, object]:
    """Scroll the top document and iframes to trigger lazy-loaded room/rate cards."""
    contexts_scrolled = 0
    frame_errors: List[str] = []

    def visit(depth: int) -> None:
        nonlocal contexts_scrolled
        contexts_scrolled += 1
        warm_up_lazy_loaded_rates(driver)

        if depth >= max_depth:
            return
        try:
            frames = driver.find_elements(By.CSS_SELECTOR, "iframe")
        except Exception as exc:
            frame_errors.append(f"iframe lookup depth {depth}: {exc}")
            return

        for index in range(len(frames)):
            try:
                frames = driver.find_elements(By.CSS_SELECTOR, "iframe")
                driver.switch_to.frame(frames[index])
                visit(depth + 1)
                driver.switch_to.parent_frame()
            except Exception as exc:
                frame_errors.append(f"iframe scroll depth {depth} index {index}: {exc}")
                try:
                    driver.switch_to.parent_frame()
                except Exception:
                    try:
                        driver.switch_to.default_content()
                    except Exception:
                        pass

    try:
        driver.switch_to.default_content()
    except Exception:
        pass
    visit(0)
    try:
        driver.switch_to.default_content()
    except Exception:
        pass

    return {"contexts_scrolled": contexts_scrolled, "frame_errors": frame_errors[:8]}


def room_fingerprint(rooms: List[Dict]) -> str:
    parts = []
    for room in rooms:
        parts.append(f"{normalize_room_name(str(room.get('room_name', ''))).lower()}={room.get('current_selling')}")
    return "|".join(sorted(parts))


def poll_rooms_after_page_open(
    driver: webdriver.Chrome,
    max_seconds: float = 10.0,
    min_seconds: float = 6.0,
) -> Dict[str, object]:
    """
    Poll the already-open booking page for live prices across top document,
    shadow DOM, and nested iframes.
    """
    start_time = time.monotonic()
    best_raw_rooms: List[Dict] = []
    best_rooms: List[Dict] = []
    best_fingerprint = ""
    stable_cycles = 0
    cycles = 0
    seen_price_text = False
    max_contexts_checked = 0
    max_iframe_count = 0
    frame_errors: List[str] = []

    max_seconds = max(3.0, float(max_seconds))
    min_seconds = min(max(1.0, float(min_seconds)), max_seconds)

    while time.monotonic() - start_time <= max_seconds:
        try:
            driver.switch_to.default_content()
        except Exception:
            pass
        scroll_booking_page_once(driver, cycles)
        time.sleep(0.55)

        context_result = collect_rooms_from_all_browser_contexts(driver)
        raw_rooms = list(context_result.get("raw_rooms", []))
        rooms = dedupe_rooms(raw_rooms)
        fingerprint = room_fingerprint(rooms)
        cycles += 1

        seen_price_text = seen_price_text or bool(context_result.get("seen_price_text", False)) or bool(rooms)
        max_contexts_checked = max(max_contexts_checked, int(context_result.get("contexts_checked", 0) or 0))
        max_iframe_count = max(max_iframe_count, int(context_result.get("iframe_count", 0) or 0))
        frame_errors.extend(str(x) for x in context_result.get("frame_errors", []) if x)

        if len(rooms) > len(best_rooms) or (len(rooms) == len(best_rooms) and fingerprint and fingerprint != best_fingerprint):
            best_raw_rooms = raw_rooms
            best_rooms = rooms
            best_fingerprint = fingerprint
            stable_cycles = 0
        elif fingerprint and fingerprint == best_fingerprint:
            stable_cycles += 1
        else:
            stable_cycles = 0

        elapsed = time.monotonic() - start_time
        if best_rooms and elapsed >= min_seconds and stable_cycles >= 5:
            break

    try:
        driver.switch_to.default_content()
        driver.execute_script("window.scrollTo(0, 0);")
    except Exception:
        pass

    return {
        "raw_rooms": best_raw_rooms,
        "rooms": best_rooms,
        "cycles": cycles,
        "seen_price_text": seen_price_text,
        "contexts_checked": max_contexts_checked,
        "iframe_count": max_iframe_count,
        "frame_errors": frame_errors[:8],
        "elapsed_seconds": round(time.monotonic() - start_time, 2),
    }

def scrape_1hotels_once(
    url: str,
    wait_seconds: int = 10,
    settle_seconds: float = 3.0,
    price_poll_seconds: float = 6.0,
    fallback_mode: bool = False,
) -> Dict:
    driver = None
    started_at = time.monotonic()
    get_timed_out = False
    body_seen = False

    try:
        driver = init_driver(fallback_mode=fallback_mode)
        page_load_timeout_seconds = max(10, int(wait_seconds))
        driver.set_page_load_timeout(page_load_timeout_seconds)

        try:
            driver.get(url)
        except TimeoutException:
            # With a 10s page-load timeout, the useful DOM may already exist. Continue
            # and poll for prices instead of treating this as a hard failure.
            get_timed_out = True

        try:
            WebDriverWait(driver, max(3, int(wait_seconds))).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
            body_seen = True
        except TimeoutException:
            body_seen = False

        # Wait for the booking app itself to hydrate. A successful <body> load is not
        # enough; the failed long-date case shows an empty #root with only Chakra
        # portal/select placeholders. Reload once if the root is stuck empty.
        app_ready_initial = wait_for_booking_app_ready(driver, max_seconds=max(10.0, min(float(wait_seconds), 35.0)))
        app_ready_result = reload_if_booking_root_is_empty(
            driver=driver,
            wait_seconds=wait_seconds,
            app_ready_result=app_ready_initial,
        )

        # After the page body and app shell appear, wait briefly before reading prices.
        # The rate rows are populated asynchronously after the cards render.
        price_settle_seconds = max(0.0, float(settle_seconds))
        time.sleep(price_settle_seconds)

        # Trigger lazy-loaded room cards before parsing. Without this, headless Chrome
        # can see the page shell but miss prices that render only after scrolling.
        warmup_result = warm_up_lazy_loaded_rates_all_contexts(driver)

        # Main mode uses the configured price polling window. Fallback gets a small
        # extra buffer because it only runs after primary returns no prices or fails.
        effective_price_poll_seconds = max(8.0, float(price_poll_seconds)) + (4.0 if fallback_mode else 0.0)
        minimum_price_poll_seconds = min(6.0 if not fallback_mode else 8.0, effective_price_poll_seconds)
        poll_result = poll_rooms_after_page_open(
            driver,
            max_seconds=effective_price_poll_seconds,
            min_seconds=minimum_price_poll_seconds,
        )
        raw_rooms = list(poll_result.get("raw_rooms", []))
        rooms = dedupe_rooms(raw_rooms)

        html_source = driver.page_source
        # Always merge the final page_source parse. The JavaScript DOM parser and
        # BeautifulSoup parser catch slightly different render shapes.
        bs4_rooms = dedupe_rooms(parse_rooms_with_bs4(html_source))
        if bs4_rooms:
            rooms = dedupe_rooms(raw_rooms + bs4_rooms)

        page_text = BeautifulSoup(html_source, "html.parser").get_text("\n", strip=True)
        return {
            "ok": True,
            "rooms": rooms,
            "raw_count": len(raw_rooms),
            "page_text_preview": page_text[:3500],
            "html_preview": html_source[:3500],
            "attempt_mode": "fallback" if fallback_mode else "primary",
            "get_timed_out": get_timed_out,
            "body_seen": body_seen,
            "page_load_timeout_seconds": page_load_timeout_seconds,
            "price_settle_seconds": price_settle_seconds,
            "price_poll_seconds": effective_price_poll_seconds,
            "minimum_price_poll_seconds": minimum_price_poll_seconds,
            "app_ready_elapsed_seconds": app_ready_result.get("elapsed_seconds", 0),
            "app_ready_last_state": app_ready_result.get("last_state", {}),
            "reloaded_empty_root": app_ready_result.get("reloaded_empty_root", False),
            "poll_cycles": poll_result.get("cycles", 0),
            "seen_price_text": poll_result.get("seen_price_text", False),
            "contexts_scrolled": warmup_result.get("contexts_scrolled", 0),
            "contexts_checked": poll_result.get("contexts_checked", 0),
            "iframe_count": poll_result.get("iframe_count", 0),
            "frame_errors": list(warmup_result.get("frame_errors", [])) + list(poll_result.get("frame_errors", [])),
            "poll_elapsed_seconds": poll_result.get("elapsed_seconds", 0),
            "total_elapsed_seconds": round(time.monotonic() - started_at, 2),
        }
    finally:
        if driver is not None:
            driver.quit()


def scrape_1hotels(
    url: str,
    wait_seconds: int = 10,
    settle_seconds: float = 3.0,
    price_poll_seconds: float = 6.0,
    retry_once: bool = True,
) -> Dict:
    attempts = [False, True] if retry_once else [False]
    history: List[Dict[str, object]] = []
    last_result: Optional[Dict] = None
    last_exception: Optional[Exception] = None

    for attempt_index, fallback_mode in enumerate(attempts, start=1):
        mode_name = "fallback" if fallback_mode else "primary"
        try:
            result = scrape_1hotels_once(
                url=url,
                wait_seconds=(int(wait_seconds) + 2) if fallback_mode else int(wait_seconds),
                settle_seconds=settle_seconds,
                price_poll_seconds=price_poll_seconds,
                fallback_mode=fallback_mode,
            )
            rooms = result.get("rooms", [])
            history.append(
                {
                    "attempt": attempt_index,
                    "mode": mode_name,
                    "status": "ok",
                    "rooms_count": len(rooms),
                    "get_timed_out": result.get("get_timed_out", False),
                    "body_seen": result.get("body_seen", False),
                    "page_load_timeout_seconds": result.get("page_load_timeout_seconds", 0),
                    "price_settle_seconds": result.get("price_settle_seconds", 0),
                    "price_poll_seconds": result.get("price_poll_seconds", 0),
                    "minimum_price_poll_seconds": result.get("minimum_price_poll_seconds", 0),
                    "app_ready_elapsed_seconds": result.get("app_ready_elapsed_seconds", 0),
                    "reloaded_empty_root": result.get("reloaded_empty_root", False),
                    "app_ready_last_state": result.get("app_ready_last_state", {}),
                    "poll_cycles": result.get("poll_cycles", 0),
                    "seen_price_text": result.get("seen_price_text", False),
                    "contexts_scrolled": result.get("contexts_scrolled", 0),
                    "contexts_checked": result.get("contexts_checked", 0),
                    "iframe_count": result.get("iframe_count", 0),
                    "frame_errors": result.get("frame_errors", []),
                    "poll_elapsed_seconds": result.get("poll_elapsed_seconds", 0),
                    "total_elapsed_seconds": result.get("total_elapsed_seconds", 0),
                }
            )
            result["retry_history"] = history
            last_result = result

            if rooms:
                return result

            if attempt_index < len(attempts):
                time.sleep(0.8)
                continue

            return result
        except Exception as exc:
            last_exception = exc
            history.append(
                {
                    "attempt": attempt_index,
                    "mode": mode_name,
                    "status": "failed",
                    "error": str(exc),
                }
            )
            if attempt_index < len(attempts):
                time.sleep(0.8)
                continue

    if last_result is not None:
        last_result["retry_history"] = history
        return last_result

    raise RuntimeError(
        "Primary search failed and fallback retry also failed. "
        f"Retry history: {history}. Last error: {last_exception}"
    ) from last_exception


ROOM_CATEGORY_ORDER = ["Hotel Rooms", "Homes", "Connecting"]


def get_room_category(room_name: str) -> str:
    """Classify room types for UI grouping and email sections."""
    cleaned_name = normalize_room_name(room_name).lower()
    if "connecting" in cleaned_name:
        return "Connecting"
    if "home" in cleaned_name:
        return "Homes"
    return "Hotel Rooms"


def group_rooms_by_category(rooms: List[Dict]) -> Dict[str, List[Dict]]:
    grouped: Dict[str, List[Dict]] = {category: [] for category in ROOM_CATEGORY_ORDER}
    for room in rooms:
        category = get_room_category(str(room.get("room_name", "")))
        grouped.setdefault(category, []).append(room)
    return grouped


def build_output_lines(rooms: List[Dict], discount_percent: float) -> List[str]:
    lines: List[str] = []
    for room in rooms:
        current = int(room["current_selling"])
        best = discount_price(current, discount_percent)
        room_name = room["room_name"]
        currency_symbol = str(room.get("currency_symbol") or "$")
        lines.append(
            f"▫{room_name} | Best offer: {format_money(best, currency_symbol)} per night. "
            f"(Currently selling: {format_money(current, currency_symbol)})"
        )
    return lines


def build_grouped_output_lines(rooms: List[Dict], discount_percent: float) -> Dict[str, List[str]]:
    grouped_rooms = group_rooms_by_category(rooms)
    return {
        category: build_output_lines(grouped_rooms.get(category, []), discount_percent)
        for category in ROOM_CATEGORY_ORDER
    }


def build_selection_label(room: Dict, discount_percent: float) -> str:
    current = int(room["current_selling"])
    best = discount_price(current, discount_percent)
    room_name = room["room_name"]
    currency_symbol = str(room.get("currency_symbol") or "$")
    label = (
        f"{room_name} | Best offer: {format_money(best, currency_symbol)} per night. "
        f"(Currently selling: {format_money(current, currency_symbol)})"
    )
    return escape_streamlit_label(label)


def build_output_dataframe(rooms: List[Dict], discount_percent: float) -> pd.DataFrame:
    rows = []
    for room in rooms:
        current = int(room["current_selling"])
        best = discount_price(current, discount_percent)
        rows.append(
            {
                "Room Type": room["room_name"],
                "Best offer": format_money(best, str(room.get("currency_symbol") or "$")),
                "Currently selling": format_money(current, str(room.get("currency_symbol") or "$")),
                "Discount % Off": f"{discount_percent:g}%",
                "Detected prices": ", ".join(format_money(x, str(room.get("currency_symbol") or "$")) for x in room.get("all_detected_prices", [])),
            }
        )
    return pd.DataFrame(rows)


def get_room_selection_key(index: int, room: Dict) -> str:
    return f"room_selected_{index}_{normalize_room_name(room['room_name']).lower()}"


def get_selected_rooms(rooms: List[Dict]) -> List[Dict]:
    selected_rooms = []
    for index, room in enumerate(rooms):
        key = get_room_selection_key(index, room)
        if st.session_state.get(key, False):
            selected_rooms.append(room)
    return selected_rooms


def get_selected_room_lines_by_category(rooms: List[Dict], discount_percent: float) -> Dict[str, List[str]]:
    return build_grouped_output_lines(get_selected_rooms(rooms), discount_percent)


def build_email_body(
    opening: str,
    ending: str,
    checkin: date,
    checkout: date,
    selected_room_lines_by_category: Dict[str, List[str]],
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

    has_selected_rooms = any(
        selected_room_lines_by_category.get(category)
        for category in ROOM_CATEGORY_ORDER
    )

    if has_selected_rooms:
        details.append("")
        for category in ROOM_CATEGORY_ORDER:
            category_lines = selected_room_lines_by_category.get(category, [])
            if not category_lines:
                continue
            details.append(category)
            details.extend(category_lines)
            details.append("")
        while details and details[-1] == "":
            details.pop()
    else:
        details.extend(["", "No room type selected."])

    parts.append("\n".join(details))

    if ending_clean:
        parts.append(ending_clean)

    return "\n\n".join(parts)


def render_copy_button(text_to_copy: str) -> None:
    escaped_text = html.escape(text_to_copy or "")
    components.html(
        f"""
        <div style="display:flex; align-items:center; gap:8px; height:42px;">
            <textarea id="email-copy-source" style="position:absolute; left:-9999px; top:-9999px;">{escaped_text}</textarea>
            <button
                id="copy-email-button"
                style="
                    width:100%;
                    height:38px;
                    border:1px solid #c9c9c9;
                    border-radius:0.5rem;
                    background:#ffffff;
                    color:#262730;
                    font-weight:700;
                    cursor:pointer;
                "
                onclick="
                    const source = document.getElementById('email-copy-source');
                    const button = document.getElementById('copy-email-button');
                    navigator.clipboard.writeText(source.value).then(function() {{
                        button.innerText = 'Copied';
                        setTimeout(function() {{ button.innerText = 'Copy'; }}, 1400);
                    }}).catch(function() {{
                        source.style.position = 'fixed';
                        source.style.left = '0';
                        source.style.top = '0';
                        source.focus();
                        source.select();
                        document.execCommand('copy');
                        source.style.position = 'absolute';
                        source.style.left = '-9999px';
                        source.style.top = '-9999px';
                        button.innerText = 'Copied';
                        setTimeout(function() {{ button.innerText = 'Copy'; }}, 1400);
                    }});
                "
            >Copy</button>
        </div>
        """,
        height=46,
    )


# ============================================================
# UI
# ============================================================
st.title("🏨 Starwood Hotel Rateshop")
st.caption("Secure Streamlit Secrets login, hotel-code dropdown, dynamic date-based URL, selectable room quotes, and email-ready output.")

st.markdown(
    """
    <style>
    div[data-testid="stCheckbox"] label p {
        font-size: 1.04rem !important;
        font-weight: 700 !important;
        line-height: 1.35 !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

with st.sidebar:
    st.header("⚙️ Search Settings")
    st.caption(f"App version: {APP_VERSION}")

    if st.button("LOGOUT"):
        st.session_state.authenticated = False
        st.session_state.pop("authenticated_user_name", None)
        st.rerun()

    hotel_key = st.selectbox(
        "Hotel dropdown menu",
        options=list(HOTEL_CODE_MAP.keys()),
        index=list(HOTEL_CODE_MAP.keys()).index(DEFAULT_HOTEL_KEY),
    )
    hotel_code = get_hotel_code(hotel_key)
    selected_currency_symbol = get_hotel_currency_symbol(hotel_key)
    st.text_input("Hotel code", value=hotel_code, disabled=True)
    st.text_input("Currency symbol", value=selected_currency_symbol, disabled=True)

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
    search_clicked = st.button("SEARCH", type="primary", use_container_width=True)
    st.checkbox("Rates include tax in email quote", key="rates_include_tax")
    currency = st.selectbox("Currency", options=["USD"], index=0)
    sort = st.selectbox("Sort", options=["low", "high"], index=0)
    group_code = st.text_input("Group Code", value="")
    promo_code = st.text_input("Promo Code", value="")
    wait_seconds = st.slider("Page open timeout seconds", 8, 20, 10, 1)

    with st.expander("Chrome runtime check", expanded=False):
        runtime = get_chrome_runtime()
        st.write("Chromium:", runtime.get("chromium_binary") or "not found")
        st.write("Chromedriver:", runtime.get("chromedriver_binary") or "not found")
        st.caption(str(runtime.get("chromium_version") or ""))
        st.caption(str(runtime.get("chromedriver_version") or ""))

if checkout <= checkin:
    st.error("Check-out date must be later than Check-in date.")
    st.stop()


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

st.text_area("Dynamic URL", value=target_url, height=88, disabled=True)

email_clicked = False

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
    room_nights_for_search = max((checkout - checkin).days, 1)
    is_long_date_search = room_nights_for_search > 3

    if is_long_date_search:
        adaptive_wait_seconds = int(min(35, max(int(wait_seconds) + 12, 24)))
        price_settle_seconds = 6.0
        price_poll_seconds = 22.0
    else:
        adaptive_wait_seconds = int(min(20, max(int(wait_seconds), 10)))
        price_settle_seconds = 3.0
        price_poll_seconds = 10.0

    with st.spinner(
        f"Opening booking page for up to {adaptive_wait_seconds}s, waiting {price_settle_seconds:g}s for async prices, "
        f"then scrolling and polling live prices for at least 6s / up to {price_poll_seconds:g}s. "
        f"Fallback adds a short second attempt only if no price is found."
    ):
        try:
            result = scrape_1hotels(
                target_url,
                wait_seconds=adaptive_wait_seconds,
                settle_seconds=price_settle_seconds,
                price_poll_seconds=price_poll_seconds,
                retry_once=True,
            )
            rooms = apply_hotel_currency_symbol(result.get("rooms", []), hotel_key)
            retry_history = result.get("retry_history", [])
            st.session_state.last_error = ""

            used_fallback = any(item.get("mode") == "fallback" and item.get("status") == "ok" for item in retry_history)
            primary_failed_or_empty = bool(
                retry_history
                and (
                    retry_history[0].get("status") == "failed"
                    or int(retry_history[0].get("rooms_count", 0) or 0) == 0
                )
            )

            if not rooms:
                st.session_state.last_output_text = ""
                st.session_state.last_df = pd.DataFrame()
                st.session_state.last_rooms = []
                st.error("No room rates were parsed. The page structure may have changed, the rate component may not have loaded, or anti-bot verification may have been triggered.")
                with st.expander("Debug: page text preview"):
                    st.text(result.get("page_text_preview", "")[:3500])
                with st.expander("Debug: HTML preview"):
                    st.code(result.get("html_preview", "")[:3500], language="html")
                with st.expander("Debug: retry history"):
                    st.json(retry_history)
            else:
                if used_fallback and primary_failed_or_empty:
                    st.warning("The primary search attempt failed or returned no rooms. Fallback retry succeeded automatically.")
                output_lines = build_output_lines(rooms, discount_percent)
                st.session_state.last_output_text = "\n".join(output_lines)
                st.session_state.last_df = build_output_dataframe(rooms, discount_percent)
                st.session_state.last_rooms = rooms
                st.session_state.generated_email = ""
                for index, room in enumerate(rooms):
                    room_key = get_room_selection_key(index, room)
                    st.session_state[room_key] = False
                st.success(f"Search completed: parsed {len(rooms)} room type(s).")
                with st.expander("Debug: retry history", expanded=False):
                    st.json(retry_history)
        except Exception as exc:
            st.session_state.last_error = str(exc)
            st.error(f"Browser startup or runtime failed: {exc}")
            with st.expander("Debug: Chrome runtime diagnostics", expanded=True):
                st.json(get_chrome_runtime())
            st.warning(
                "Important: if the error still shows /home/appuser/.cache/selenium/chromedriver, "
                "Streamlit is still running an older app.py, or the app was not rebooted successfully."
            )

st.subheader("Room Type Selection")
rooms_for_selection = st.session_state.last_rooms
if rooms_for_selection:
    st.caption("Select the room type(s) you want to include in the email quote.")
    select_all_col, clear_all_col, spacer_col = st.columns([1, 1, 3])
    with select_all_col:
        if st.button("Select all room types", use_container_width=True):
            for index, room in enumerate(rooms_for_selection):
                room_key = get_room_selection_key(index, room)
                st.session_state[room_key] = True
            st.rerun()
    with clear_all_col:
        if st.button("Clear selections", use_container_width=True):
            for index, room in enumerate(rooms_for_selection):
                room_key = get_room_selection_key(index, room)
                st.session_state[room_key] = False
            st.rerun()

    grouped_selection_rooms = group_rooms_by_category(rooms_for_selection)
    room_index_by_identity = {id(room): index for index, room in enumerate(rooms_for_selection)}
    for category in ROOM_CATEGORY_ORDER:
        category_rooms = grouped_selection_rooms.get(category, [])
        if not category_rooms:
            continue
        st.markdown(f"### {category}")
        for room in category_rooms:
            index = room_index_by_identity[id(room)]
            line = build_selection_label(room, discount_percent)
            room_key = get_room_selection_key(index, room)
            st.checkbox(line, key=room_key)
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
        placeholder="Type the email opening here. Click Save Template to save it only in this browser.",
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


save_template_col, save_template_spacer = st.columns([1, 5])
with save_template_col:
    save_template_clicked = st.button("Save Template", type="secondary", use_container_width=True)

if save_template_clicked:
    render_local_storage_saver(
        opening=st.session_state.email_opening,
        ending=st.session_state.email_ending,
        rates_include_tax=bool(st.session_state.rates_include_tax),
    )

st.subheader("Generated Email")
email_button_col, email_button_spacer = st.columns([1, 5])
with email_button_col:
    email_clicked = st.button("EMAIL", type="primary", use_container_width=True)

if email_clicked or st.session_state.generated_email:
    selected_lines_by_category = get_selected_room_lines_by_category(st.session_state.last_rooms, discount_percent)
    st.session_state.generated_email = build_email_body(
        opening=st.session_state.email_opening,
        ending=st.session_state.email_ending,
        checkin=checkin,
        checkout=checkout,
        selected_room_lines_by_category=selected_lines_by_category,
        rates_include_tax=bool(st.session_state.rates_include_tax),
    )

st.text_area(
    "Email Output",
    value=st.session_state.generated_email,
    height=420,
    label_visibility="collapsed",
)
copy_button_col, copy_button_spacer = st.columns([1, 5])
with copy_button_col:
    render_copy_button(st.session_state.generated_email)

st.divider()
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
    st.info("Click SEARCH to load structured room-rate data here.")
