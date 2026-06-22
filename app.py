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
# Hotel map: dropdown label -> 1hotels booking hotel code
# Add more hotels here later, for example: "1BK": "xxxxx"
# ============================================================
HOTEL_CODE_MAP: Dict[str, str] = {
    "1SB": "60507",
}

DEFAULT_HOTEL_KEY = "1SB"
DEFAULT_CHECKIN = date(2026, 6, 22)
DEFAULT_CHECKOUT = date(2026, 6, 23)
DEFAULT_DISCOUNT_PERCENT = 10
BASE_BOOKING_URL = "https://www.1hotels.com/book/{hotel_code}"

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

st.set_page_config(page_title="酒店房价监控系统", layout="wide")


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


def login_required() -> bool:
    expected_user_name = get_secret_value("user_name")
    expected_password = get_secret_value("password")

    if not expected_user_name or not expected_password:
        st.error(
            "请先在 Streamlit Secrets 里配置登录账号：\n\n"
            "user_name = 123\n"
            "password = 456"
        )
        st.stop()

    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False

    if st.session_state.authenticated:
        return True

    st.title("🔐 1 Hotels 房价工具登录")
    with st.form("login_form", clear_on_submit=False):
        user_name = st.text_input("User Name")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("LOGIN", type="primary")

    if submitted:
        if user_name == expected_user_name and password == expected_password:
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("用户名或密码不正确。")

    st.stop()


login_required()


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
# Chrome / Chromedriver helpers for Streamlit Cloud
# Fixes: Service ~/.cache/selenium/chromedriver/... unexpectedly exited, status code 127
# Cause: Selenium Manager downloaded a driver that cannot run in the cloud container.
# Solution: install system chromium/chromium-driver via packages.txt and use /usr/bin paths.
# ============================================================
def first_existing_path(paths: List[str]) -> Optional[str]:
    for item in paths:
        if item and os.path.exists(item):
            return item
    return None


def first_executable(names: List[str]) -> Optional[str]:
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    return None


def run_version_command(binary_path: Optional[str]) -> str:
    if not binary_path:
        return "not found"
    try:
        completed = subprocess.run(
            [binary_path, "--version"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        output = (completed.stdout or completed.stderr or "").strip()
        return output or "version unavailable"
    except Exception as exc:
        return f"version check failed: {exc}"


@st.cache_resource(show_spinner=False)
def get_chrome_paths() -> Dict[str, Optional[str]]:
    chromium_binary = first_existing_path(
        [
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
        ]
    ) or first_executable(["chromium", "chromium-browser", "google-chrome", "google-chrome-stable"])

    chromedriver_binary = first_existing_path(
        [
            "/usr/bin/chromedriver",
            "/usr/lib/chromium/chromedriver",
            "/usr/lib/chromium-browser/chromedriver",
        ]
    ) or first_executable(["chromedriver"])

    return {
        "chromium_binary": chromium_binary,
        "chromedriver_binary": chromedriver_binary,
        "chromium_version": run_version_command(chromium_binary),
        "chromedriver_version": run_version_command(chromedriver_binary),
    }


def build_chrome_options() -> Options:
    paths = get_chrome_paths()
    chrome_options = Options()

    if paths.get("chromium_binary"):
        chrome_options.binary_location = str(paths["chromium_binary"])

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
    paths = get_chrome_paths()
    chrome_options = build_chrome_options()

    if paths.get("chromedriver_binary"):
        service = Service(executable_path=str(paths["chromedriver_binary"]))
        driver = webdriver.Chrome(service=service, options=chrome_options)
    else:
        # Fallback for local machines where Selenium Manager works.
        # On Streamlit Cloud this should not be used; install chromium-driver instead.
        driver = webdriver.Chrome(options=chrome_options)

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
    name = re.sub(r"\s+", " ", name or "").strip()
    return name


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
    """
    Primary parser for the current 1hotels Chakra UI booking page.
    The screenshots show:
      - room title: h3.chakra-card__title
      - selected/current rate price: p element with text like $620
      - previous/struck rate: s element with text like $858
    We intentionally avoid depending on generated CSS class names such as css-10c1v88.
    """
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
    """Fallback parser when JavaScript extraction returns nothing."""
    soup = BeautifulSoup(html_source, "html.parser")
    raw_rooms: List[Dict] = []

    title_tags = soup.find_all(["h1", "h2", "h3", "h4"])
    for title in title_tags:
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

        # Wait until at least one room-looking heading or price appears.
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


# ============================================================
# UI
# ============================================================
st.title("🏨 1 Hotels 实时房价爬虫控制台")
st.caption("支持 Streamlit Cloud Secrets 登录、酒店代码 dropdown、日期动态 URL、折扣 Best offer 输出。")

with st.sidebar:
    st.header("⚙️ Search Settings")
    if st.button("LOGOUT"):
        st.session_state.authenticated = False
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
    currency = st.selectbox("Currency", options=["USD"], index=0)
    sort = st.selectbox("Sort", options=["low", "high"], index=0)
    group_code = st.text_input("Group Code", value="")
    promo_code = st.text_input("Promo Code", value="")
    wait_seconds = st.slider("Browser wait seconds", 15, 75, 35, 5)

    with st.expander("Chrome runtime check"):
        chrome_paths = get_chrome_paths()
        st.write("Chromium:", chrome_paths.get("chromium_binary") or "not found")
        st.write("Chromedriver:", chrome_paths.get("chromedriver_binary") or "not found")
        st.caption(chrome_paths.get("chromium_version") or "")
        st.caption(chrome_paths.get("chromedriver_version") or "")

if checkout <= checkin:
    st.error("Check-out 日期必须晚于 Check-in 日期。")
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

url_col, button_col = st.columns([5, 1])
with url_col:
    st.text_area("Dynamic URL", value=target_url, height=88, disabled=True)
with button_col:
    st.write("")
    st.write("")
    search_clicked = st.button("SEARCH", type="primary", use_container_width=True)
    email_clicked = st.button("EMAIL", use_container_width=True)

st.markdown(
    "**Rates are fully pre-paid and non-refundable.** "
    f"**{discount_percent:g}% OFF**"
)

if email_clicked:
    st.info("EMAIL 按钮已预留：可以后续接 SMTP、SendGrid 或 Gmail API。")

if "last_output_text" not in st.session_state:
    st.session_state.last_output_text = ""
if "last_df" not in st.session_state:
    st.session_state.last_df = pd.DataFrame()
if "last_error" not in st.session_state:
    st.session_state.last_error = ""

if search_clicked:
    with st.spinner("正在启动无头浏览器并抓取官网动态价格，通常需要 20-45 秒..."):
        try:
            result = scrape_1hotels(target_url, wait_seconds=int(wait_seconds))
            rooms = result.get("rooms", [])
            st.session_state.last_error = ""

            if not rooms:
                st.session_state.last_output_text = ""
                st.session_state.last_df = pd.DataFrame()
                st.error("没有解析到房型价格。可能是页面结构变化、价格组件未加载、或触发了反爬/验证。")
                with st.expander("Debug: page text preview"):
                    st.text(result.get("page_text_preview", "")[:3500])
                with st.expander("Debug: HTML preview"):
                    st.code(result.get("html_preview", "")[:3500], language="html")
            else:
                output_lines = build_output_lines(rooms, discount_percent)
                st.session_state.last_output_text = "\n".join(output_lines)
                st.session_state.last_df = build_output_dataframe(rooms, discount_percent)
                st.success(f"抓取完成：解析到 {len(rooms)} 个房型。")
        except WebDriverException as exc:
            chrome_paths = get_chrome_paths()
            st.session_state.last_error = str(exc)
            st.error(f"浏览器启动或运行失败: {exc}")
            st.warning(
                "如果在 Streamlit Cloud 上运行，请确认 repo 根目录有 packages.txt，"
                "内容至少包含 chromium 和 chromium-driver，然后重启/重新部署 app。"
            )
            with st.expander("Debug: Chrome paths and versions"):
                st.json(chrome_paths)
        except Exception as exc:
            st.session_state.last_error = str(exc)
            st.error(f"运行中发生错误: {exc}")

left, right = st.columns([1.15, 1])
with left:
    st.subheader("Search Result Text Box")
    st.text_area(
        "Output",
        value=st.session_state.last_output_text,
        height=420,
        label_visibility="collapsed",
    )

with right:
    st.subheader("Structured Result")
    if not st.session_state.last_df.empty:
        st.dataframe(st.session_state.last_df, use_container_width=True, hide_index=True)
        csv_data = st.session_state.last_df.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            label="Download CSV",
            data=csv_data,
            file_name=f"1hotels_{hotel_key}_{checkin}_{checkout}.csv",
            mime="text/csv",
            use_container_width=True,
        )
    else:
        st.info("点击 SEARCH 后，房型和价格会显示在这里。")

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
