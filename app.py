import html
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
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
APP_VERSION = "2026-07-07 Starwood Hotel Rateshop cloud-chrome-stable-main-v3"

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

PRICE_RE = re.compile(r"(?P<symbol>[$€£¥₹₩₪₫₱฿₦₵₡₲₴₺₽]|USD|CAD|AUD|EUR|GBP)\s*(?P<amount>[0-9][0-9,]*)")

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


BROWSER_START_LOCK = threading.Lock()


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
        "tmp_disk": shell_output(["/bin/sh", "-lc", "df -h /tmp /dev/shm 2>&1 || true"]),
        "running_chrome": shell_output(["/bin/sh", "-lc", "ps aux | grep -Ei 'chrom[e|ium]|chromedriver' | grep -v grep || true"]),
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

    # The page shell loads quickly, while live prices arrive shortly after through JS.
    # Eager prevents driver.get() from hanging on tracking/image requests.
    chrome_options.page_load_strategy = "eager"

    # Fresh profile per browser attempt. This is important on Streamlit/Cloud Run because
    # a prior Chrome process can leave a locked or corrupt profile behind.
    user_data_dir = tempfile.mkdtemp(prefix="starwood_chrome_profile_", dir="/tmp")
    chrome_options.add_argument(f"--user-data-dir={user_data_dir}")
    chrome_options.add_argument("--profile-directory=Default")

    # Required/stability flags for Linux container environments.
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-setuid-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-software-rasterizer")
    chrome_options.add_argument("--window-size=1920,1400")
    chrome_options.add_argument("--lang=en-US,en")

    # IMPORTANT:
    # Do NOT use --single-process in modern Chromium 150 cloud containers.
    # Do NOT use a fixed --remote-debugging-port like 9222 because concurrent Streamlit
    # sessions can collide. Primary uses pipe; fallback uses a random port.
    if fallback_mode:
        chrome_options.add_argument("--remote-debugging-port=0")
        chrome_options.add_argument("--disable-features=VizDisplayCompositor,Translate,BackForwardCache,AcceptCHFrame,MediaRouter,OptimizationHints")
    else:
        chrome_options.add_argument("--remote-debugging-pipe")
        chrome_options.add_argument("--disable-features=Translate,BackForwardCache,AcceptCHFrame,MediaRouter,OptimizationHints")

    # Reduce background services and noisy browser features.
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-background-networking")
    chrome_options.add_argument("--disable-background-timer-throttling")
    chrome_options.add_argument("--disable-backgrounding-occluded-windows")
    chrome_options.add_argument("--disable-breakpad")
    chrome_options.add_argument("--disable-client-side-phishing-detection")
    chrome_options.add_argument("--disable-component-extensions-with-background-pages")
    chrome_options.add_argument("--disable-default-apps")
    chrome_options.add_argument("--disable-hang-monitor")
    chrome_options.add_argument("--disable-ipc-flooding-protection")
    chrome_options.add_argument("--disable-notifications")
    chrome_options.add_argument("--disable-popup-blocking")
    chrome_options.add_argument("--disable-prompt-on-repost")
    chrome_options.add_argument("--disable-sync")
    chrome_options.add_argument("--metrics-recording-only")
    chrome_options.add_argument("--mute-audio")
    chrome_options.add_argument("--no-first-run")
    chrome_options.add_argument("--no-default-browser-check")
    chrome_options.add_argument("--password-store=basic")
    chrome_options.add_argument("--use-mock-keychain")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/150.0.0.0 Safari/537.36"
    )

    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
    chrome_options.add_experimental_option("useAutomationExtension", False)
    chrome_options.add_experimental_option(
        "prefs",
        {
            "profile.default_content_setting_values.notifications": 2,
            "credentials_enable_service": False,
            "profile.password_manager_enabled": False,
        },
    )

    # Keep this on options so init_driver can clean it up after driver.quit().
    chrome_options._starwood_user_data_dir = user_data_dir  # type: ignore[attr-defined]
    return chrome_options

def read_file_tail(path: str, max_chars: int = 6000) -> str:
    try:
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8", errors="ignore") as handle:
                return handle.read()[-max_chars:]
    except Exception as exc:
        return f"Unable to read {path}: {exc}"
    return ""


def init_driver(fallback_mode: bool = False) -> webdriver.Chrome:
    runtime = validate_chrome_runtime()
    chromium_binary = str(runtime["chromium_binary"])
    chromedriver_binary = str(runtime["chromedriver_binary"])

    # CRITICAL: always pass Service(executable_path=...).
    # Do not call webdriver.Chrome(options=...), because that invokes Selenium Manager.
    chromedriver_log = f"/tmp/starwood_chromedriver_{'fallback' if fallback_mode else 'primary'}_{int(time.time() * 1000)}.log"
    service = Service(
        executable_path=chromedriver_binary,
        log_output=chromedriver_log,
        service_args=["--verbose"],
    )
    chrome_options = build_chrome_options(chromium_binary, fallback_mode=fallback_mode)
    user_data_dir = getattr(chrome_options, "_starwood_user_data_dir", "")

    try:
        driver = webdriver.Chrome(service=service, options=chrome_options)
        # The user observed that the page shell normally loads in about 7 seconds.
        # Keep this short, then poll the DOM for prices instead of waiting for full page load.
        driver.set_page_load_timeout(14 if fallback_mode else 11)
        driver.set_script_timeout(30)
        driver._starwood_user_data_dir = user_data_dir  # type: ignore[attr-defined]
        driver._starwood_chromedriver_log = chromedriver_log  # type: ignore[attr-defined]
        return driver
    except Exception as exc:
        log_tail = read_file_tail(chromedriver_log)
        try:
            if user_data_dir:
                shutil.rmtree(user_data_dir, ignore_errors=True)
        except Exception:
            pass
        raise RuntimeError(
            "ChromeDriver session creation failed. "
            f"fallback_mode={fallback_mode}; "
            f"chromium_binary={chromium_binary}; "
            f"chromedriver_binary={chromedriver_binary}; "
            f"chromedriver_log={chromedriver_log}; "
            f"chromedriver_log_tail={log_tail}; "
            f"original_error={exc}"
        ) from exc


def cleanup_driver(driver: Optional[webdriver.Chrome]) -> None:
    if driver is None:
        return
    user_data_dir = getattr(driver, "_starwood_user_data_dir", "")
    try:
        driver.quit()
    except Exception:
        pass
    try:
        if user_data_dir:
            shutil.rmtree(user_data_dir, ignore_errors=True)
    except Exception:
        pass


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
    script = r"""
    const priceRegex = /^([$€£¥₹₩₪₫₱฿₦₵₡₲₴₺₽]|USD|CAD|AUD|EUR|GBP)\s*[0-9][0-9,]*/;
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

        const match = text.match(/([$€£¥₹₩₪₫₱฿₦₵₡₲₴₺₽]|USD|CAD|AUD|EUR|GBP)\s*([0-9][0-9,]*)/);
        if (!match) continue;
        const symbol = match[1] || '$';
        const value = parseInt(match[2].replace(/,/g, ''), 10);
        if (!Number.isFinite(value) || value <= 0 || value > 20000) continue;

        allPrices.push({value, symbol});
        if (node.tagName.toLowerCase() === 'p') {
          pPrices.push({value, symbol});
        }
      }

      const candidatePrices = pPrices.length ? pPrices : allPrices;
      if (!candidatePrices.length) continue;

      const bestPrice = candidatePrices.reduce((best, item) => item.value < best.value ? item : best, candidatePrices[0]);

      rows.push({
        room_name: roomName,
        current_selling: bestPrice.value,
        currency_symbol: bestPrice.symbol || '$',
        all_detected_prices: Array.from(new Set(allPrices.map(item => item.value))).sort((a, b) => a - b),
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

        p_prices: List[Dict[str, object]] = []
        all_prices: List[Dict[str, object]] = []
        for node in card.find_all(["p", "span", "div", "label"]):
            text = node.get_text(" ", strip=True)
            if not text or not PRICE_RE.search(text):
                continue
            if re.search(r"amenity|fee|tax|total|include", text, re.I):
                continue
            parsed_price = parse_price_match(text)
            if not parsed_price:
                continue
            price = int(parsed_price["amount"])
            if price <= 0 or price > 20000:
                continue
            all_prices.append(parsed_price)
            if node.name == "p":
                p_prices.append(parsed_price)

        candidate_prices = p_prices or all_prices
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
    """Small scrolls trigger lazy-loaded room cards and price nodes without wasting time."""
    try:
        driver.execute_script(
            """
            const height = Math.max(
              document.body ? document.body.scrollHeight : 0,
              document.documentElement ? document.documentElement.scrollHeight : 0
            );
            const positions = [0, 0.28, 0.55, 0.82, 1.0, 0.35];
            const ratio = positions[step_index % positions.length];
            window.scrollTo(0, Math.floor(height * ratio));
            """
        )
    except Exception:
        pass


def poll_rooms_after_page_open(driver: webdriver.Chrome, max_seconds: float = 6.0) -> Dict[str, object]:
    """
    Poll the already-open booking page for live prices.

    The page shell can load first, then prices usually appear about 2 seconds later.
    This loop parses repeatedly and returns as soon as room prices are present and
    stable for a couple of quick cycles.
    """
    start_time = time.monotonic()
    best_raw_rooms: List[Dict] = []
    best_rooms: List[Dict] = []
    stable_cycles = 0
    last_count = 0
    cycles = 0

    while time.monotonic() - start_time <= max_seconds:
        scroll_booking_page_once(driver, cycles)
        time.sleep(0.45)

        raw_rooms = parse_rooms_with_browser_dom(driver)
        rooms = dedupe_rooms(raw_rooms)
        cycles += 1

        if len(rooms) > len(best_rooms):
            best_raw_rooms = raw_rooms
            best_rooms = rooms
            stable_cycles = 0
            last_count = len(rooms)
        elif rooms and len(rooms) == last_count:
            stable_cycles += 1
        else:
            last_count = len(rooms)

        # Prices often show up shortly after the shell. Once the count is stable,
        # stop immediately instead of waiting for the full timeout.
        if best_rooms and stable_cycles >= 2:
            break

    try:
        driver.execute_script("window.scrollTo(0, 0);")
    except Exception:
        pass

    return {
        "raw_rooms": best_raw_rooms,
        "rooms": best_rooms,
        "cycles": cycles,
        "elapsed_seconds": round(time.monotonic() - start_time, 2),
    }


def scrape_1hotels_once(
    url: str,
    wait_seconds: int = 10,
    settle_seconds: int = 0,
    fallback_mode: bool = False,
) -> Dict:
    driver = None
    started_at = time.monotonic()
    get_timed_out = False
    body_seen = False

    try:
        driver = init_driver(fallback_mode=fallback_mode)

        try:
            driver.get(url)
        except TimeoutException:
            # With a 10s page-load timeout, the useful DOM may already exist. Continue
            # and poll for prices instead of treating this as a hard failure.
            get_timed_out = True

        try:
            WebDriverWait(driver, max(3, min(int(wait_seconds), 10))).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
            body_seen = True
        except TimeoutException:
            body_seen = False

        # Main mode: page shell up to 10s, then about 6s of price polling.
        # Fallback adds a tiny buffer only; it should not become slower than the old version.
        price_poll_seconds = 8.0 if fallback_mode else 6.0
        poll_result = poll_rooms_after_page_open(driver, max_seconds=price_poll_seconds)
        raw_rooms = list(poll_result.get("raw_rooms", []))
        rooms = dedupe_rooms(raw_rooms)

        html_source = driver.page_source
        if not rooms:
            # One final cheap HTML parse in case prices are in page_source but DOM script missed them.
            rooms = dedupe_rooms(parse_rooms_with_bs4(html_source))

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
            "poll_cycles": poll_result.get("cycles", 0),
            "poll_elapsed_seconds": poll_result.get("elapsed_seconds", 0),
            "total_elapsed_seconds": round(time.monotonic() - started_at, 2),
        }
    finally:
        cleanup_driver(driver)


def scrape_1hotels(url: str, wait_seconds: int = 10, settle_seconds: int = 0, retry_once: bool = True) -> Dict:
    attempts = [False, True] if retry_once else [False]
    history: List[Dict[str, object]] = []
    last_result: Optional[Dict] = None
    last_exception: Optional[Exception] = None

    for attempt_index, fallback_mode in enumerate(attempts, start=1):
        mode_name = "fallback" if fallback_mode else "primary"
        try:
            # Selenium/Chromium is memory-heavy and profile-sensitive in cloud containers.
            # Lock startup/search per process to avoid concurrent Chrome crashes.
            with BROWSER_START_LOCK:
                result = scrape_1hotels_once(
                    url=url,
                    wait_seconds=12 if fallback_mode else wait_seconds,
                    settle_seconds=settle_seconds,
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
                    "poll_cycles": result.get("poll_cycles", 0),
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
    adaptive_wait_seconds = int(min(20, max(int(wait_seconds), 10)))
    with st.spinner(
        f"Opening booking page for up to {adaptive_wait_seconds}s, then polling live prices for 6s. "
        f"Fallback adds a short second attempt only if no price is found."
    ):
        try:
            result = scrape_1hotels(target_url, wait_seconds=adaptive_wait_seconds, retry_once=True)
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
                "Important: if Python still shows 3.14 or APP_VERSION is not cloud-chrome-stable-main-v3, "
                "the deployment is still running old files or the Python version file was not applied."
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
