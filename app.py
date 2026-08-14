import base64
import hashlib
import hmac
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
APP_VERSION = "2026-08-14 Starwood Hotel Rateshop sage-commercial-ui-v1"

# ============================================================
# Hotel map: dropdown label -> booking-provider configuration
#
# provider="1hotels":
#   https://www.1hotels.com/book/{hotel_code}?...
#
# provider="baccarat":
#   https://www.baccarathotels.com/book?hotelCode={hotel_code}&...
# ============================================================
HOTEL_CODE_MAP: Dict[str, Dict[str, str]] = {
    "1SB": {"code": "60507", "currency_symbol": "$", "provider": "1hotels"},
    "1CP": {"code": "60735", "currency_symbol": "$", "provider": "1hotels"},
    "1BB": {"code": "66266", "currency_symbol": "$", "provider": "1hotels"},
    "1TY": {"code": "96185", "currency_symbol": "¥", "provider": "1hotels"},
    "1ML": {"code": "47157", "currency_symbol": "$", "provider": "1hotels"},
    "1MF": {"code": "40333", "currency_symbol": "£", "provider": "1hotels"},
    "1HNB": {"code": "5826", "currency_symbol": "$", "provider": "1hotels"},
    "1CPH": {"code": "41069", "currency_symbol": "kr.", "provider": "1hotels"},
    "1NV": {"code": "35903", "currency_symbol": "$", "provider": "1hotels"},
    "1SF": {"code": "36017", "currency_symbol": "$", "provider": "1hotels"},
    "1SE": {"code": "47314", "currency_symbol": "$", "provider": "1hotels"},
    "1TO": {"code": "31116", "currency_symbol": "$", "provider": "1hotels"},
    "1WH": {"code": "77961", "currency_symbol": "$", "provider": "1hotels"},
    "BAC": {
        "code": "62963",
        "currency_symbol": "$",
        "provider": "baccarat",
        "hotel_provider": "1",
        "client_id": "baccarat",
    },
}

DEFAULT_HOTEL_KEY = "1SB"
DEFAULT_CHECKIN = date.today()
DEFAULT_CHECKOUT = date.today() + timedelta(days=1)
DEFAULT_DISCOUNT_PERCENT = 10
ONE_HOTELS_BOOKING_URL = "https://www.1hotels.com/book/{hotel_code}"
BACCARAT_BOOKING_URL = "https://www.baccarathotels.com/book"

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

STREAMLIT_ENTERPRISE_CSS = """
<style>
:root {
    color-scheme: light;
    --rs-canvas: #E7ECE9;
    --rs-canvas-2: #E2E9E5;
    --rs-sidebar: #EEF2EF;
    --rs-panel: #F4F7F5;
    --rs-panel-strong: #F8FAF8;
    --rs-panel-muted: #EAF0EC;
    --rs-control: #F2F5F3;
    --rs-control-hover: #E5ECE8;
    --rs-line: #C9D5CE;
    --rs-line-soft: #D9E2DD;
    --rs-ink: #173229;
    --rs-ink-2: #3F574C;
    --rs-ink-3: #718278;
    --rs-accent: #2F6F53;
    --rs-accent-deep: #24533F;
    --rs-accent-soft: #DCE9E1;
    --rs-accent-softer: #E7F0EA;
    --rs-success: #2F7454;
    --rs-warning: #956728;
    --rs-danger: #A74E5F;
    --rs-shadow: 0 18px 48px rgba(31, 63, 49, .10);
    --rs-shadow-soft: 0 8px 24px rgba(31, 63, 49, .065);
    --rs-radius-sm: 9px;
    --rs-radius: 13px;
    --rs-radius-lg: 18px;
    --rs-font: Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}

html, body, .stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"] {
    font-family: var(--rs-font) !important;
    color-scheme: light !important;
    color: var(--rs-ink) !important;
    background: var(--rs-canvas) !important;
}

html, body { overflow-x: hidden !important; }
[data-testid="stMain"] { min-width: 0 !important; overflow-x: hidden !important; }

header[data-testid="stHeader"] {
    background: rgba(231, 236, 233, .93) !important;
    border-bottom: 1px solid rgba(60, 87, 74, .08) !important;
    backdrop-filter: blur(12px);
}
[data-testid="stToolbar"], [data-testid="stDecoration"] { color: var(--rs-ink-2) !important; }

.stMainBlockContainer,
section.main > div.block-container {
    width: 100% !important;
    max-width: 1280px !important;
    min-width: 0 !important;
    margin: 0 auto !important;
    padding: 1.35rem 1.65rem 4rem !important;
    box-sizing: border-box !important;
}

/* ---------- Typography ---------- */
h1, h2, h3, h4, h5, h6,
[data-testid="stMarkdownContainer"] h1,
[data-testid="stMarkdownContainer"] h2,
[data-testid="stMarkdownContainer"] h3,
[data-testid="stHeadingWithActionElements"] {
    color: var(--rs-ink) !important;
    letter-spacing: -.025em !important;
}
[data-testid="stCaptionContainer"], .stCaption, small { color: var(--rs-ink-3) !important; }
[data-testid="stMarkdownContainer"] p { color: var(--rs-ink-2); }
a { color: var(--rs-accent) !important; }
a:hover { color: var(--rs-accent-deep) !important; }

/* ---------- Enterprise header ---------- */
.rs-hero {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1.3rem;
    margin: .1rem 0 1rem;
    padding: 1.25rem 1.4rem;
    border: 1px solid var(--rs-line);
    border-radius: var(--rs-radius-lg);
    background:
        radial-gradient(circle at 92% 15%, rgba(72, 135, 102, .12), transparent 31%),
        linear-gradient(135deg, #F5F8F6 0%, #EBF1ED 100%);
    box-shadow: var(--rs-shadow-soft);
}
.rs-hero-copy { min-width: 0; }
.rs-eyebrow,
.rs-section-eyebrow,
.rs-side-kicker {
    color: var(--rs-accent);
    font-size: .67rem;
    font-weight: 820;
    letter-spacing: .13em;
    text-transform: uppercase;
}
.rs-hero h1 {
    margin: .28rem 0 .32rem !important;
    font-size: clamp(1.8rem, 2.5vw, 2.45rem) !important;
    line-height: 1.06 !important;
    font-weight: 760 !important;
}
.rs-hero p {
    max-width: 760px;
    margin: 0 !important;
    color: var(--rs-ink-2) !important;
    font-size: .93rem;
    line-height: 1.55;
}
.rs-live-badge {
    flex: 0 0 auto;
    display: inline-flex;
    align-items: center;
    gap: .42rem;
    padding: .48rem .68rem;
    border: 1px solid #BDD3C6;
    border-radius: 999px;
    background: #E2EEE7;
    color: #285E47;
    font-size: .69rem;
    font-weight: 760;
    letter-spacing: .04em;
    white-space: nowrap;
}
.rs-live-dot {
    width: .43rem;
    height: .43rem;
    border-radius: 50%;
    background: #3E8A65;
    box-shadow: 0 0 0 4px rgba(62, 138, 101, .11);
}

/* ---------- Sidebar ---------- */
section[data-testid="stSidebar"] {
    width: 340px !important;
    min-width: 340px !important;
    background: linear-gradient(180deg, #F0F4F1 0%, #E9EFEB 100%) !important;
    border-right: 1px solid var(--rs-line) !important;
    box-shadow: 10px 0 30px rgba(31, 63, 49, .035) !important;
}
section[data-testid="stSidebar"] > div,
section[data-testid="stSidebar"] [data-testid="stSidebarContent"] { background: transparent !important; }
section[data-testid="stSidebar"] [data-testid="stSidebarContent"] {
    padding-top: 1rem !important;
    padding-left: 1rem !important;
    padding-right: 1rem !important;
}
.rs-sidebrand {
    margin: 0 0 .95rem;
    padding: 1rem;
    border: 1px solid var(--rs-line);
    border-radius: 15px;
    background: rgba(248, 250, 248, .74);
    box-shadow: var(--rs-shadow-soft);
}
.rs-sidebrand .name {
    margin-top: .28rem;
    color: var(--rs-ink);
    font-size: 1.08rem;
    font-weight: 760;
}
.rs-sidebrand .meta {
    margin-top: .25rem;
    color: var(--rs-ink-3);
    font-size: .72rem;
    line-height: 1.45;
}
.rs-side-section {
    margin: 1rem 0 .7rem;
    padding-top: .9rem;
    border-top: 1px solid var(--rs-line-soft);
}
.rs-side-section.first { margin-top: .25rem; padding-top: 0; border-top: 0; }
.rs-side-section-title {
    color: var(--rs-ink);
    font-size: .88rem;
    font-weight: 750;
}
.rs-side-section-note {
    margin-top: .2rem;
    color: var(--rs-ink-3);
    font-size: .71rem;
    line-height: 1.4;
}

/* Explicit label contrast, including sidebar widgets. */
[data-testid="stWidgetLabel"] p,
[data-testid="stWidgetLabel"] span,
section[data-testid="stSidebar"] label,
section[data-testid="stSidebar"] label p,
section[data-testid="stSidebar"] label span {
    color: var(--rs-ink-2) !important;
    -webkit-text-fill-color: var(--rs-ink-2) !important;
    opacity: 1 !important;
    font-weight: 670 !important;
}
[data-testid="stWidgetLabel"] p { font-size: .79rem !important; }

/* ---------- Native controls ---------- */
[data-testid="stTextInput"] input,
[data-testid="stDateInput"] input,
[data-testid="stTextArea"] textarea,
[data-testid="stNumberInput"] input,
[data-testid="stSelectbox"] [data-baseweb="select"] > div,
[data-baseweb="base-input"],
[data-baseweb="input"] > div {
    color: var(--rs-ink) !important;
    -webkit-text-fill-color: var(--rs-ink) !important;
    background: var(--rs-control) !important;
    border-color: var(--rs-line) !important;
    border-radius: var(--rs-radius-sm) !important;
    box-shadow: none !important;
}
[data-testid="stTextInput"] input,
[data-testid="stDateInput"] input,
[data-testid="stNumberInput"] input { min-height: 2.55rem !important; }
[data-testid="stTextArea"] textarea { line-height: 1.5 !important; }

[data-testid="stTextInput"] input:focus,
[data-testid="stDateInput"] input:focus,
[data-testid="stTextArea"] textarea:focus,
[data-testid="stNumberInput"] input:focus,
[data-testid="stSelectbox"] [data-baseweb="select"] > div:focus-within,
[data-baseweb="input"] > div:focus-within {
    border-color: var(--rs-accent) !important;
    box-shadow: 0 0 0 3px rgba(47, 111, 83, .11) !important;
}

/* Streamlit number-input steppers must not inherit red/black theme buttons. */
[data-testid="stNumberInput"] button,
[data-testid="stNumberInput"] button[kind],
section[data-testid="stSidebar"] [data-testid="stNumberInput"] button {
    min-height: 0 !important;
    height: 100% !important;
    border: 0 !important;
    border-left: 1px solid var(--rs-line) !important;
    border-radius: 0 !important;
    background: #E4EBE7 !important;
    color: #315647 !important;
    -webkit-text-fill-color: #315647 !important;
    box-shadow: none !important;
    transform: none !important;
}
[data-testid="stNumberInput"] button:hover {
    background: #D8E3DC !important;
    color: var(--rs-accent-deep) !important;
}
[data-testid="stNumberInput"] button svg,
[data-testid="stNumberInput"] button * {
    color: #315647 !important;
    fill: currentColor !important;
    stroke: currentColor !important;
}

input:disabled, textarea:disabled,
[data-testid="stTextInput"] input:disabled,
[data-testid="stTextArea"] textarea:disabled {
    color: #63766D !important;
    -webkit-text-fill-color: #63766D !important;
    opacity: 1 !important;
    background: #E4EAE7 !important;
}
::placeholder { color: #87958E !important; opacity: 1 !important; }

/* Dropdown portal and date calendar. */
[data-baseweb="popover"],
[data-baseweb="menu"],
ul[role="listbox"],
[role="listbox"] {
    background: #F4F7F5 !important;
    color: var(--rs-ink) !important;
    border-color: var(--rs-line) !important;
    border-radius: 11px !important;
    box-shadow: var(--rs-shadow) !important;
}
[role="option"],
[data-baseweb="menu"] li {
    color: var(--rs-ink) !important;
    -webkit-text-fill-color: var(--rs-ink) !important;
    background: transparent !important;
}
[role="option"]:hover,
[role="option"][aria-selected="true"],
[data-baseweb="menu"] li:hover {
    color: var(--rs-accent-deep) !important;
    background: var(--rs-accent-soft) !important;
}
[data-baseweb="calendar"] {
    color: var(--rs-ink) !important;
    background: #F4F7F5 !important;
    border: 1px solid var(--rs-line) !important;
    box-shadow: var(--rs-shadow) !important;
}
[data-baseweb="calendar"] button {
    color: var(--rs-ink) !important;
    background: transparent !important;
}
[data-baseweb="calendar"] [aria-selected="true"] {
    color: #F7FAF8 !important;
    background: var(--rs-accent) !important;
}

/* ---------- Buttons ---------- */
.stButton > button,
.stDownloadButton > button,
[data-testid="stFormSubmitButton"] > button {
    min-height: 2.55rem !important;
    border: 1px solid var(--rs-line) !important;
    border-radius: 10px !important;
    background: linear-gradient(180deg, #F5F8F6, #E7ECE9) !important;
    color: var(--rs-ink) !important;
    -webkit-text-fill-color: var(--rs-ink) !important;
    font-weight: 720 !important;
    box-shadow: 0 4px 12px rgba(31, 63, 49, .055) !important;
    transition: .16s ease !important;
}
.stButton > button:hover,
.stDownloadButton > button:hover,
[data-testid="stFormSubmitButton"] > button:hover {
    border-color: #AFC4B8 !important;
    background: #E1E9E4 !important;
    color: var(--rs-accent-deep) !important;
    transform: translateY(-1px);
}
.stButton > button[kind="primary"],
[data-testid="stFormSubmitButton"] > button[kind="primary"] {
    border-color: var(--rs-accent-deep) !important;
    background: linear-gradient(135deg, #34785A 0%, #285F47 100%) !important;
    color: #F8FAF9 !important;
    -webkit-text-fill-color: #F8FAF9 !important;
    box-shadow: 0 9px 22px rgba(47, 111, 83, .18) !important;
}
.stButton > button[kind="primary"] *,
[data-testid="stFormSubmitButton"] > button[kind="primary"] * {
    color: #F8FAF9 !important;
    -webkit-text-fill-color: #F8FAF9 !important;
}
.stButton > button[kind="primary"]:hover,
[data-testid="stFormSubmitButton"] > button[kind="primary"]:hover {
    background: linear-gradient(135deg, #2E6C51 0%, #234F3C 100%) !important;
}
.st-key-rs_sign_out button {
    min-height: 2.15rem !important;
    background: transparent !important;
    box-shadow: none !important;
    color: var(--rs-ink-2) !important;
    font-size: .76rem !important;
}

/* ---------- Checkbox / slider ---------- */
[data-testid="stCheckbox"] label p {
    color: var(--rs-ink) !important;
    -webkit-text-fill-color: var(--rs-ink) !important;
    font-size: .88rem !important;
    font-weight: 640 !important;
    line-height: 1.38 !important;
}
[data-testid="stCheckbox"] input { accent-color: var(--rs-accent) !important; }
[data-baseweb="checkbox"] > div:first-child { border-color: #9DB3A7 !important; }
[data-testid="stSlider"] [role="slider"] {
    background: var(--rs-accent) !important;
    border-color: var(--rs-accent) !important;
}
[data-testid="stSlider"] div[data-baseweb="slider"] > div > div { background: rgba(47, 111, 83, .25) !important; }

/* ---------- Forms / panels / expanders ---------- */
[data-testid="stForm"],
[data-testid="stVerticalBlockBorderWrapper"] {
    border-color: var(--rs-line) !important;
    border-radius: var(--rs-radius) !important;
    background: rgba(244, 247, 245, .90) !important;
    box-shadow: none !important;
}
[data-testid="stExpander"] {
    overflow: hidden;
    border: 1px solid var(--rs-line) !important;
    border-radius: 11px !important;
    background: rgba(244, 247, 245, .76) !important;
    box-shadow: none !important;
}
[data-testid="stExpander"] summary,
[data-testid="stExpander"] summary p,
[data-testid="stExpander"] summary span {
    color: var(--rs-ink-2) !important;
    -webkit-text-fill-color: var(--rs-ink-2) !important;
    font-weight: 660 !important;
}
[data-testid="stExpander"] summary:hover { background: #E6EDE9 !important; }
hr { border-color: var(--rs-line-soft) !important; }

/* ---------- Alerts / status ---------- */
[data-testid="stAlert"],
[data-baseweb="notification"] {
    color: var(--rs-ink) !important;
    background: #E5EDE8 !important;
    border: 1px solid #C6D6CC !important;
    border-radius: 11px !important;
    box-shadow: none !important;
}
[data-testid="stAlert"] > div,
[data-baseweb="notification"] > div { background: transparent !important; }
[data-testid="stAlert"] p,
[data-testid="stAlert"] span,
[data-baseweb="notification"] p,
[data-baseweb="notification"] span { color: var(--rs-ink) !important; }
.stSuccess { background: #E1ECE5 !important; border-color: #BFD4C6 !important; }
.stWarning { background: #F1EBDD !important; border-color: #DCCDAE !important; }
.stError { background: #F3E3E6 !important; border-color: #DCBEC5 !important; }

.rs-status,
.rs-empty-state {
    margin: .45rem 0 .9rem;
    padding: .85rem .95rem;
    border: 1px solid var(--rs-line);
    border-radius: 11px;
    background: #E9EFEB;
    color: var(--rs-ink-2);
    font-size: .84rem;
    line-height: 1.45;
}
.rs-status strong, .rs-empty-state strong { color: var(--rs-ink); }
.rs-status-success { border-color: #BFD3C6; background: #E2ECE6; }

/* ---------- Section headers ---------- */
.rs-section-head {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 1rem;
    margin: 1.55rem 0 .65rem;
}
.rs-section-title {
    margin-top: .2rem;
    color: var(--rs-ink);
    font-size: 1.2rem;
    font-weight: 760;
    letter-spacing: -.02em;
}
.rs-section-note {
    max-width: 580px;
    color: var(--rs-ink-3);
    font-size: .77rem;
    line-height: 1.45;
    text-align: right;
}
.rs-category-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin: 1rem 0 .45rem;
    color: var(--rs-ink);
    font-size: .91rem;
    font-weight: 740;
}
.rs-category-count {
    color: var(--rs-ink-3);
    font-size: .7rem;
    font-weight: 640;
}
.rs-room-pricing {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: .45rem;
    margin: -.2rem 0 .35rem 2rem;
}
.rs-room-stat {
    min-width: 0;
    padding: .52rem .62rem;
    border: 1px solid var(--rs-line-soft);
    border-radius: 9px;
    background: #EDF2EF;
}
.rs-room-stat .label {
    color: var(--rs-ink-3);
    font-size: .62rem;
    font-weight: 720;
    letter-spacing: .055em;
    text-transform: uppercase;
}
.rs-room-stat .value {
    margin-top: .12rem;
    color: var(--rs-ink);
    font-size: .88rem;
    font-weight: 740;
}

/* ---------- Search context cards ---------- */
.rs-summary-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(185px, 1fr));
    gap: .65rem;
    margin: .15rem 0 .85rem;
}
.rs-summary-card {
    min-width: 0;
    padding: .78rem .88rem;
    border: 1px solid var(--rs-line);
    border-radius: 12px;
    background: rgba(244, 247, 245, .92);
    box-shadow: 0 4px 13px rgba(31, 63, 49, .04);
}
.rs-summary-card .label {
    color: var(--rs-ink-3);
    font-size: .64rem;
    font-weight: 760;
    letter-spacing: .07em;
    text-transform: uppercase;
}
.rs-summary-card .value {
    margin-top: .21rem;
    color: var(--rs-ink);
    font-size: 1.04rem;
    font-weight: 760;
    line-height: 1.15;
}
.rs-summary-card .foot {
    margin-top: .18rem;
    color: var(--rs-ink-3);
    font-size: .69rem;
    line-height: 1.35;
    white-space: normal;
}

/* ---------- Data / code ---------- */
[data-testid="stDataFrame"], [data-testid="stTable"] {
    overflow: hidden !important;
    border: 1px solid var(--rs-line) !important;
    border-radius: 12px !important;
    background: #F2F5F3 !important;
    box-shadow: none !important;
}
[data-testid="stCodeBlock"], pre, code {
    color: #29483B !important;
    background: #E6ECE8 !important;
    border-color: var(--rs-line) !important;
}
[data-testid="stSpinner"] > div { border-top-color: var(--rs-accent) !important; }

/* ---------- Login ---------- */
.rs-login-brand {
    max-width: 620px;
    margin: 6vh auto 1rem;
    padding: 1.2rem 1.35rem;
    border: 1px solid var(--rs-line);
    border-radius: 16px;
    background: #F3F6F4;
    box-shadow: var(--rs-shadow-soft);
}
.rs-login-brand h1 {
    margin: .25rem 0 .35rem !important;
    font-size: 1.55rem !important;
    font-weight: 760 !important;
}
.rs-login-brand p { margin: 0 !important; color: var(--rs-ink-2) !important; }

/* ---------- Scrollbars ---------- */
*::-webkit-scrollbar { width: 10px; height: 10px; }
*::-webkit-scrollbar-track { background: transparent; }
*::-webkit-scrollbar-thumb {
    border: 3px solid transparent;
    border-radius: 999px;
    background: rgba(47, 111, 83, .25);
    background-clip: padding-box;
}
*::-webkit-scrollbar-thumb:hover { background: rgba(47, 111, 83, .42); background-clip: padding-box; }

/* ---------- Responsive ---------- */
@media (max-width: 1180px) {
    section[data-testid="stSidebar"] { width: 320px !important; min-width: 320px !important; }
    .stMainBlockContainer, section.main > div.block-container { padding-left: 1rem !important; padding-right: 1rem !important; }
    .rs-room-pricing { grid-template-columns: repeat(3, minmax(110px, 1fr)); }
}
@media (max-width: 980px) {
    .stMainBlockContainer [data-testid="stHorizontalBlock"] { flex-wrap: wrap !important; }
    .stMainBlockContainer [data-testid="column"] { min-width: min(300px, 100%) !important; flex: 1 1 300px !important; }
    .rs-hero { align-items: flex-start; }
    .rs-section-head { align-items: flex-start; flex-direction: column; gap: .25rem; }
    .rs-section-note { text-align: left; }
}
@media (max-width: 700px) {
    .stMainBlockContainer, section.main > div.block-container { padding: .9rem .7rem 3rem !important; }
    .rs-hero { flex-direction: column; padding: 1rem; border-radius: 14px; }
    .rs-summary-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .rs-room-pricing { grid-template-columns: 1fr; margin-left: 0; }
}
</style>

"""

st.markdown(STREAMLIT_ENTERPRISE_CSS, unsafe_allow_html=True)


# ============================================================
# Browser-local cache and authentication
#
# Streamlit Cloud -> App settings -> Secrets:
# user_name = "123"
# password = "456"
#
# Optional, strongly recommended:
# auth_token_secret = "a-long-random-secret-that-is-different-from-the-password"
# remember_login_days = 365
#
# The browser never stores the plaintext username/password. After a successful
# login, it stores only a signed, expiring token in localStorage. Changing the
# configured password or auth_token_secret immediately invalidates old tokens.
# ============================================================
def get_secret_value(key: str, default: str = "") -> str:
    try:
        value = st.secrets.get(key, default)
    except Exception:
        value = default
    return str(value)


def get_query_param_value(key: str, default: str = "") -> str:
    """Return one query parameter value as a string across Streamlit versions."""
    try:
        value = st.query_params.get(key, default)
    except Exception:
        return default
    if isinstance(value, list):
        return str(value[0]) if value else default
    return str(value) if value is not None else default


def default_ending_text() -> str:
    valid_until = date.today() + timedelta(days=3)
    return (
        f"This quote is valid until {valid_until.isoformat()}. Change in stay date will result in new pricing.\n"
        "Rates are fully pre-paid and non-refundable. 100% room, tax and resort fee are charged at time of booking.\n\n"
        "Please let us know which room type would you like to choose."
    )


def get_browser_storage_namespace() -> str:
    """Return the browser-local namespace used for saved email preferences."""
    return "starwood_rateshop_email_template_v1"


def get_auth_storage_namespace() -> str:
    """Return the browser-local key used for the persistent login token."""
    return "starwood_rateshop_auth_v1"


def get_remember_login_days() -> int:
    """Return the persistent login lifetime, constrained to a safe range."""
    raw_value = get_secret_value("remember_login_days", "365")
    try:
        days = int(raw_value)
    except (TypeError, ValueError):
        days = 365
    return max(1, min(days, 3650))


def get_auth_signing_secret(expected_user_name: str, expected_password: str) -> bytes:
    """
    Return the token signing secret.

    An explicit auth_token_secret is preferred. If it is not configured, derive
    a stable signing key from the configured login credentials, so changing the
    password invalidates all previously remembered logins.
    """
    configured_secret = get_secret_value("auth_token_secret", "").strip()
    token_secret = configured_secret or "starwood-rateshop-auth-v1-default-signing-salt"
    source = f"{token_secret}\0{expected_user_name}\0{expected_password}"
    return hashlib.sha256(source.encode("utf-8")).digest()


def base64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def base64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def create_auth_token(user_name: str, expected_password: str) -> str:
    """Create a signed browser token without storing the plaintext password."""
    now = int(time.time())
    payload = {
        "version": 1,
        "user_name": user_name,
        "issued_at": now,
        "expires_at": now + get_remember_login_days() * 24 * 60 * 60,
    }
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload_part = base64url_encode(payload_bytes)
    signature = hmac.new(
        get_auth_signing_secret(user_name, expected_password),
        payload_part.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{payload_part}.{base64url_encode(signature)}"


def validate_auth_token(token: str, expected_user_name: str, expected_password: str) -> Optional[Dict[str, object]]:
    """Validate token signature, username, version, and expiration."""
    if not token or "." not in token:
        return None

    try:
        payload_part, signature_part = token.split(".", 1)
        supplied_signature = base64url_decode(signature_part)
        expected_signature = hmac.new(
            get_auth_signing_secret(expected_user_name, expected_password),
            payload_part.encode("ascii"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(supplied_signature, expected_signature):
            return None

        payload = json.loads(base64url_decode(payload_part).decode("utf-8"))
        if not isinstance(payload, dict):
            return None
        if int(payload.get("version", 0) or 0) != 1:
            return None
        if str(payload.get("user_name", "")) != expected_user_name:
            return None

        now = int(time.time())
        issued_at = int(payload.get("issued_at", 0) or 0)
        expires_at = int(payload.get("expires_at", 0) or 0)
        if issued_at <= 0 or issued_at > now + 300:
            return None
        if expires_at <= now:
            return None
        if expires_at - issued_at > 3650 * 24 * 60 * 60:
            return None
        return payload
    except Exception:
        return None


def render_browser_storage_loader() -> None:
    """
    Load both persistent authentication and email preferences before login.

    The old version loaded email preferences only after login and then forced a
    full browser reload. That reload created a new Streamlit session and erased
    the just-written authenticated session_state, which is why the first login
    appeared to flash and required a second password entry.
    """
    template_namespace = get_browser_storage_namespace()
    auth_namespace = get_auth_storage_namespace()

    components.html(
        f"""
        <script>
        const templateNamespace = {json.dumps(template_namespace)};
        const authKey = {json.dumps(auth_namespace)};
        const parentWindow = window.parent;
        const params = new URLSearchParams(parentWindow.location.search);

        if (!params.has("browser_storage_loaded")) {{
            const opening = parentWindow.localStorage.getItem(templateNamespace + "_email_opening") || "";
            const ending = parentWindow.localStorage.getItem(templateNamespace + "_email_ending") || "";
            const tax = parentWindow.localStorage.getItem(templateNamespace + "_rates_include_tax") || "";
            const authToken = parentWindow.localStorage.getItem(authKey) || "";

            params.set("browser_storage_loaded", "1");
            params.set("browser_template_loaded", "1");
            params.set("browser_email_opening", opening);
            params.set("browser_email_ending", ending);
            params.set("browser_rates_include_tax", tax);
            if (authToken) {{
                params.set("browser_auth_token", authToken);
            }} else {{
                params.delete("browser_auth_token");
            }}

            const query = params.toString();
            const newUrl = parentWindow.location.pathname + (query ? "?" + query : "") + parentWindow.location.hash;
            parentWindow.history.replaceState(null, "", newUrl);
            parentWindow.location.reload();
        }}
        </script>
        """,
        height=0,
    )


def consume_browser_storage_from_query_params() -> None:
    """Initialize browser-backed preferences exactly once in the Streamlit session."""
    if get_query_param_value("browser_storage_loaded") != "1":
        st.stop()

    if "browser_template_consumed" not in st.session_state:
        browser_opening = get_query_param_value("browser_email_opening")
        browser_ending = get_query_param_value("browser_email_ending")
        browser_tax = get_query_param_value("browser_rates_include_tax")

        st.session_state.email_opening = browser_opening or ""
        st.session_state.email_ending = browser_ending or default_ending_text()
        st.session_state.rates_include_tax = browser_tax.lower() == "true"
        st.session_state.browser_template_consumed = True


def render_auth_token_saver_and_reload(token: str) -> None:
    """Persist the signed token, put it in the URL bridge, and reload once."""
    auth_namespace = get_auth_storage_namespace()
    components.html(
        f"""
        <script>
        const parentWindow = window.parent;
        const authKey = {json.dumps(auth_namespace)};
        const token = {json.dumps(token)};
        const params = new URLSearchParams(parentWindow.location.search);

        parentWindow.localStorage.setItem(authKey, token);
        params.set("browser_storage_loaded", "1");
        params.set("browser_auth_token", token);

        const query = params.toString();
        const newUrl = parentWindow.location.pathname + (query ? "?" + query : "") + parentWindow.location.hash;
        parentWindow.history.replaceState(null, "", newUrl);
        window.setTimeout(function() {{ parentWindow.location.reload(); }}, 80);
        </script>
        <div style="
            padding: 10px 12px;
            border-radius: 8px;
            border: 1px solid rgba(45,123,87,.24);
            background: #E7F0EA;
            color: #235B42;
            font-weight: 700;
            font-family: sans-serif;
        ">
            Login successful. Saving this browser as trusted...
        </div>
        """,
        height=54,
    )


def render_invalid_auth_token_clearer() -> None:
    """Delete an invalid/expired token without forcing another page reload."""
    auth_namespace = get_auth_storage_namespace()
    components.html(
        f"""
        <script>
        const parentWindow = window.parent;
        const params = new URLSearchParams(parentWindow.location.search);
        parentWindow.localStorage.removeItem({json.dumps(auth_namespace)});
        params.delete("browser_auth_token");
        const query = params.toString();
        const newUrl = parentWindow.location.pathname + (query ? "?" + query : "") + parentWindow.location.hash;
        parentWindow.history.replaceState(null, "", newUrl);
        </script>
        """,
        height=0,
    )


def render_logout_and_reload() -> None:
    """Clear the remembered login from this browser and reload to the login form."""
    auth_namespace = get_auth_storage_namespace()
    components.html(
        f"""
        <script>
        const parentWindow = window.parent;
        const params = new URLSearchParams(parentWindow.location.search);
        parentWindow.localStorage.removeItem({json.dumps(auth_namespace)});
        params.delete("browser_auth_token");
        params.set("browser_storage_loaded", "1");
        const query = params.toString();
        const newUrl = parentWindow.location.pathname + (query ? "?" + query : "") + parentWindow.location.hash;
        parentWindow.history.replaceState(null, "", newUrl);
        window.setTimeout(function() {{ parentWindow.location.reload(); }}, 80);
        </script>
        """,
        height=0,
    )


def render_local_storage_saver(opening: str, ending: str, rates_include_tax: bool) -> None:
    """Save the current email template into this browser's localStorage only."""
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
            border: 1px solid rgba(45,123,87,.24);
            background: #E7F0EA;
            color: #235B42;
            font-weight: 700;
            font-family: sans-serif;
        ">
            Template saved to this browser.
        </div>
        """,
        height=52,
    )


def login_required() -> None:
    expected_user_name = get_secret_value("user_name")
    expected_password = get_secret_value("password")

    if not expected_user_name or not expected_password:
        st.error(
            "Please configure the login credentials in Streamlit Secrets first:\n\n"
            'user_name = "123"\n'
            'password = "456"'
        )
        st.stop()

    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False

    if st.session_state.authenticated:
        return

    browser_token = get_query_param_value("browser_auth_token")
    token_payload = validate_auth_token(browser_token, expected_user_name, expected_password)
    if token_payload is not None:
        st.session_state.authenticated = True
        st.session_state.authenticated_user_name = expected_user_name
        st.session_state.authenticated_via_browser_token = True
        return

    if browser_token:
        render_invalid_auth_token_clearer()

    st.markdown(
        f"""
        <div class="rs-login-brand">
          <div class="rs-side-kicker">Secure Commercial Access</div>
          <h1>Starwood Hotel Rate Shop</h1>
          <p>Sign in to the live pricing workspace. This browser can be remembered for up to {get_remember_login_days()} day(s); plaintext credentials are never stored.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    with st.form("login_form", clear_on_submit=False):
        user_name = st.text_input("User Name")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("LOGIN", type="primary")

    if submitted:
        user_ok = hmac.compare_digest(str(user_name), expected_user_name)
        password_ok = hmac.compare_digest(str(password), expected_password)
        if user_ok and password_ok:
            token = create_auth_token(expected_user_name, expected_password)
            render_auth_token_saver_and_reload(token)
        else:
            st.error("Invalid username or password.")

    st.stop()


# Load browser state before authentication. This ordering prevents the email
# template localStorage bridge from destroying a newly authenticated session.
render_browser_storage_loader()
consume_browser_storage_from_query_params()
login_required()


# ============================================================
# Hotel config helpers
# ============================================================
def get_hotel_config(hotel_key: str) -> Dict[str, str]:
    """Return a copy of the selected hotel's booking configuration."""
    if hotel_key not in HOTEL_CODE_MAP:
        raise KeyError(f"Unknown hotel key: {hotel_key}")
    return dict(HOTEL_CODE_MAP[hotel_key])


def get_hotel_code(hotel_key: str) -> str:
    return str(get_hotel_config(hotel_key)["code"])


def get_hotel_provider(hotel_key: str) -> str:
    return str(get_hotel_config(hotel_key).get("provider") or "1hotels")


def get_hotel_currency_symbol(hotel_key: str) -> str:
    return str(get_hotel_config(hotel_key).get("currency_symbol") or "$")


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
    hotel_key: str,
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
    """Build the correct booking URL for the selected hotel/provider."""
    hotel_config = get_hotel_config(hotel_key)
    hotel_code = str(hotel_config["code"])
    provider = str(hotel_config.get("provider") or "1hotels").lower()

    if provider == "baccarat":
        # Baccarat uses the same booking-page UI/CSS, but its query-string contract
        # differs from 1 Hotels. Keep these parameter names aligned with the live
        # Baccarat booking link supplied for BAC hotel code 62963.
        params = {
            "currency": currency,
            "endDate": checkout.isoformat(),
            "exactMatchOnly": "false",
            "hotelCode": hotel_code,
            "hotelProvider": str(hotel_config.get("hotel_provider") or "1"),
            "numRooms": 1,
            "primaryLangId": language,
            "startDate": checkin.isoformat(),
            "adults": adults,
            "children": children,
            "clientId": str(hotel_config.get("client_id") or "baccarat"),
            "theme": "null",
        }
        return f"{BACCARAT_BOOKING_URL}?{urlencode(params)}"

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
    return f"{ONE_HOTELS_BOOKING_URL.format(hotel_code=hotel_code)}?{urlencode(params)}"


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
      // IMPORTANT: The correct displayed selling price in the current 1 Hotels UI is the
      // standalone paragraph node, for example:
      //   <p class="css-1oc1v88">$5,435</p>
      // The same rate row also contains crossed-out original prices such as <s>$8,439</s>,
      // discount badges such as "36% off", and package copy such as "$250 Credit".
      // Reading broad card text will therefore contaminate prices. Only accept exact,
      // visible paragraph price nodes first; use broader parsing only as a last resort.
      const exactPriceRegex = /^\s*([$€£¥₹₩₪₫₱฿₦₵₡₲₴₺₽]|USD|CAD|AUD|EUR|GBP|kr\.?)\s*([0-9][0-9,]*)\s*$/i;
      const allElements = collectElements(card);
      const allPrices = [];
      const sellingPrices = [];

      function isVisible(node) {
        if (!node) return false;
        const style = window.getComputedStyle ? window.getComputedStyle(node) : null;
        if (!style) return true;
        if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity || '1') === 0) return false;
        return true;
      }

      function isCrossedOut(node) {
        if (!node) return false;
        const style = window.getComputedStyle ? window.getComputedStyle(node) : null;
        const decoration = style ? String(style.textDecorationLine || style.textDecoration || '') : '';
        if (/line-through/i.test(decoration)) return true;
        if (node.closest && node.closest('s, strike, del')) return true;
        return false;
      }

      function pushExactPrice(node, target) {
        const text = elementText(node);
        const match = text.match(exactPriceRegex);
        if (!match) return false;
        if (!isVisible(node) || isCrossedOut(node)) return false;
        const value = parseInt(String(match[2] || '').replace(/,/g, ''), 10);
        if (!Number.isFinite(value) || value <= 0 || value > 20000) return false;
        target.push({value, symbol: match[1] || '$'});
        return true;
      }

      // Primary, user-confirmed selector from DevTools screenshot.
      for (const node of allElements) {
        const tagName = String(node.tagName || '').toUpperCase();
        const className = String(node.className || '');
        if (tagName === 'P' && /(^|\s)css-1oc1v88(\s|$)/.test(className)) {
          pushExactPrice(node, sellingPrices);
        }
      }

      // Secondary fallback: exact standalone <p>$123</p> nodes only. This still avoids
      // package descriptions like "Nonrefundable: $250 Credit" because those are not
      // exact price-only paragraphs.
      if (!sellingPrices.length) {
        for (const node of allElements) {
          const tagName = String(node.tagName || '').toUpperCase();
          if (tagName === 'P') pushExactPrice(node, sellingPrices);
        }
      }

      for (const item of sellingPrices) allPrices.push(item);

      // Last-resort fallback only if the site changes markup and no standalone p price
      // nodes are present. Keep this strict and exclude known contamination terms.
      if (!sellingPrices.length) {
        const priceNodes = allElements.filter(el => /^(P|SPAN)$/i.test(el.tagName || ''));
        for (const node of priceNodes) {
          const text = elementText(node);
          if (!text || text.length > 80) continue;
          const match = text.match(priceRegex);
          if (!match) continue;
          if (/amenity|fee|tax|total|include|included|resort|destination|deposit|due now|credit|transfer|breakfast|parking|spa|package/i.test(text)) continue;
          if (/%\s*off|was|original|strike/i.test(text)) continue;
          if (!isVisible(node) || isCrossedOut(node)) continue;
          const value = parseInt(String(match[2] || '').replace(/,/g, ''), 10);
          if (!Number.isFinite(value) || value <= 0 || value > 20000) continue;
          const item = {value, symbol: match[1] || '$'};
          allPrices.push(item);
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
        exact_price_re = re.compile(
            r"^\s*([$€£¥₹₩₪₫₱฿₦₵₡₲₴₺₽]|USD|CAD|AUD|EUR|GBP|kr\.?)\s*([0-9][0-9,]*)\s*$",
            re.I,
        )

        def node_is_crossed_out(node) -> bool:
            if node.find_parent(["s", "strike", "del"]):
                return True
            style_value = " ".join(str(node.get(attr, "")) for attr in ["style", "class"])
            return bool(re.search(r"line-through|strike|original", style_value, re.I))

        def parse_exact_node_price(node) -> Optional[Dict[str, object]]:
            text = node.get_text(" ", strip=True)
            match = exact_price_re.match(text or "")
            if not match or node_is_crossed_out(node):
                return None
            try:
                amount = int(match.group(2).replace(",", ""))
            except ValueError:
                return None
            if amount <= 0 or amount > 20000:
                return None
            return {"amount": amount, "symbol": match.group(1) or "$"}

        # Primary, user-confirmed selector from DevTools screenshot:
        # <p class="css-1oc1v88">$5,435</p>
        for node in card.find_all("p", class_=lambda c: c and "css-1oc1v88" in str(c).split()):
            parsed_price = parse_exact_node_price(node)
            if parsed_price:
                selling_prices.append(parsed_price)

        # Secondary fallback: exact standalone paragraph price nodes only.
        # This avoids polluted text like "$250 Credit", "All rates include $66 amenity fee",
        # and crossed-out original rates.
        if not selling_prices:
            for node in card.find_all("p"):
                parsed_price = parse_exact_node_price(node)
                if parsed_price:
                    selling_prices.append(parsed_price)

        all_prices.extend(selling_prices)

        # Last-resort fallback if the markup class changes. Keep this narrow.
        if not selling_prices:
            for node in card.find_all(["p", "span"]):
                text = node.get_text(" ", strip=True)
                if not text or len(text) > 80 or not PRICE_RE.search(text):
                    continue
                if re.search(
                    r"amenity|fee|tax|total|include|included|resort|destination|deposit|due now|credit|transfer|breakfast|parking|spa|package|%\s*off|was|original|strike",
                    text,
                    re.I,
                ):
                    continue
                if node_is_crossed_out(node):
                    continue
                parsed_price = parse_price_match(text)
                if not parsed_price:
                    continue
                price = int(parsed_price["amount"])
                if price <= 0 or price > 20000:
                    continue
                all_prices.append(parsed_price)
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


def style_output_dataframe(df: pd.DataFrame):
    """Apply the shared sage commercial palette to Streamlit's structured rate table."""
    if df is None or df.empty:
        return df
    return (
        df.style
        .set_properties(**{
            "background-color": "#F2F5F3",
            "color": "#294239",
            "border-color": "#D5E0D9",
        })
        .set_table_styles([
            {
                "selector": "th",
                "props": [
                    ("background-color", "#D9E7DE"),
                    ("color", "#274A3B"),
                    ("font-weight", "750"),
                    ("border-color", "#C8D8CE"),
                ],
            },
            {
                "selector": "td",
                "props": [("border-color", "#D5E0D9")],
            },
        ])
    )


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
        <style>
          html, body {{ margin:0; padding:0; background:transparent; font-family:Inter,Segoe UI,sans-serif; }}
        </style>
        <div style="display:flex; align-items:center; gap:8px; height:42px; background:transparent;">
            <textarea id="email-copy-source" style="position:absolute; left:-9999px; top:-9999px;">{escaped_text}</textarea>
            <button
                id="copy-email-button"
                style="
                    width:100%;
                    height:38px;
                    border:1px solid #C9D5CE;
                    border-radius:10px;
                    background:linear-gradient(180deg,#F2F5F3,#E5ECE8);
                    color:#173229;
                    font-weight:720;
                    box-shadow:0 4px 12px rgba(31,63,49,.055);
                    cursor:pointer;
                "
                onclick="
                    const source = document.getElementById('email-copy-source');
                    const button = document.getElementById('copy-email-button');
                    navigator.clipboard.writeText(source.value).then(function() {{
                        button.innerText = 'Copied to clipboard';
                        setTimeout(function() {{ button.innerText = 'Copy email'; }}, 1400);
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
                        button.innerText = 'Copied to clipboard';
                        setTimeout(function() {{ button.innerText = 'Copy email'; }}, 1400);
                    }});
                "
            >Copy email</button>
        </div>
        """,
        height=46,
    )


# ============================================================
# UI
# ============================================================
def render_section_heading(title: str, note: str = "", eyebrow: str = "") -> None:
    eyebrow_html = f'<div class="rs-section-eyebrow">{html.escape(eyebrow)}</div>' if eyebrow else ""
    note_html = f'<div class="rs-section-note">{html.escape(note)}</div>' if note else ""
    st.markdown(
        f"""
        <div class="rs-section-head">
          <div>
            {eyebrow_html}
            <div class="rs-section-title">{html.escape(title)}</div>
          </div>
          {note_html}
        </div>
        """,
        unsafe_allow_html=True,
    )


st.markdown(
    """
    <div class="rs-hero">
      <div class="rs-hero-copy">
        <div class="rs-eyebrow">Commercial Intelligence · Live Pricing</div>
        <h1>Rate Shop</h1>
        <p>Validate live room rates, structure a commercially disciplined offer, and prepare a client-ready quote without leaving the pricing workflow.</p>
      </div>
      <div class="rs-live-badge"><span class="rs-live-dot"></span> LIVE BOOKING ENGINE</div>
    </div>
    """,
    unsafe_allow_html=True,
)

with st.sidebar:
    st.markdown(
        """
        <div class="rs-sidebrand">
          <div class="rs-side-kicker">Commercial Tools</div>
          <div class="name">Rate Shop</div>
          <div class="meta">Live pricing and quote preparation workspace</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if st.button("Sign out", key="rs_sign_out", use_container_width=True):
        st.session_state.authenticated = False
        st.session_state.pop("authenticated_user_name", None)
        st.session_state.pop("authenticated_via_browser_token", None)
        render_logout_and_reload()
        st.stop()

    st.markdown(
        """
        <div class="rs-side-section first">
          <div class="rs-side-section-title">Search parameters</div>
          <div class="rs-side-section-note">Set the hotel, stay dates and party size used against the live booking engine.</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    hotel_key = st.selectbox(
        "Hotel",
        options=list(HOTEL_CODE_MAP.keys()),
        index=list(HOTEL_CODE_MAP.keys()).index(DEFAULT_HOTEL_KEY),
    )
    hotel_code = get_hotel_code(hotel_key)
    hotel_provider = get_hotel_provider(hotel_key)
    selected_currency_symbol = get_hotel_currency_symbol(hotel_key)

    checkin = st.date_input("Check-in", value=DEFAULT_CHECKIN)
    default_checkout = max(DEFAULT_CHECKOUT, checkin + timedelta(days=1))
    checkout = st.date_input("Check-out", value=default_checkout)

    adults = st.number_input("Adults", min_value=1, max_value=10, value=1, step=1)
    children = st.number_input("Children", min_value=0, max_value=10, value=0, step=1)

    st.markdown(
        """
        <div class="rs-side-section">
          <div class="rs-side-section-title">Quote rule</div>
          <div class="rs-side-section-note">Apply a controlled client offer to the live selling rate.</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    discount_percent = st.number_input(
        "Offer discount %",
        min_value=0.0,
        max_value=100.0,
        value=float(DEFAULT_DISCOUNT_PERCENT),
        step=1.0,
    )
    st.checkbox("Rates include tax in client email", key="rates_include_tax")

    with st.expander("Advanced search options", expanded=False):
        st.caption(
            f"Property {hotel_key} · code {hotel_code} · {hotel_provider.title()} · display {selected_currency_symbol}"
        )
        currency = st.selectbox("Booking currency", options=["USD"], index=0)
        sort = st.selectbox("Rate sort", options=["low", "high"], index=0)
        group_code = st.text_input("Group code", value="")
        promo_code = st.text_input("Promo code", value="")
        wait_seconds = st.slider("Search timeout seconds", 8, 20, 10, 1)

    search_clicked = st.button("Search live rates", type="primary", use_container_width=True)
    st.caption("Rates are retrieved from the live booking flow and should be revalidated before final client commitment.")

    with st.expander("System details", expanded=False):
        st.caption(f"App version: {APP_VERSION}")
        runtime = get_chrome_runtime()
        st.write("Chromium", runtime.get("chromium_binary") or "not found")
        st.write("Chromedriver", runtime.get("chromedriver_binary") or "not found")
        st.caption(str(runtime.get("chromium_version") or ""))
        st.caption(str(runtime.get("chromedriver_version") or ""))

if checkout <= checkin:
    st.error("Check-out date must be later than check-in date.")
    st.stop()


target_url = build_booking_url(
    hotel_key=hotel_key,
    checkin=checkin,
    checkout=checkout,
    adults=int(adults),
    children=int(children),
    currency=currency,
    group_code=group_code,
    promo_code=promo_code,
    sort=sort,
)

stay_nights = max((checkout - checkin).days, 1)
party_size = int(adults) + int(children)
st.markdown(
    f"""
    <div class="rs-summary-grid">
      <div class="rs-summary-card">
        <div class="label">Property</div>
        <div class="value">{html.escape(hotel_key)}</div>
        <div class="foot">{html.escape(hotel_provider.title())} · hotel code {html.escape(hotel_code)}</div>
      </div>
      <div class="rs-summary-card">
        <div class="label">Stay</div>
        <div class="value">{stay_nights} night{'s' if stay_nights != 1 else ''}</div>
        <div class="foot">{checkin.isoformat()} → {checkout.isoformat()}</div>
      </div>
      <div class="rs-summary-card">
        <div class="label">Guests</div>
        <div class="value">{party_size}</div>
        <div class="foot">{int(adults)} adult{'s' if int(adults) != 1 else ''} · {int(children)} child{'ren' if int(children) != 1 else ''}</div>
      </div>
      <div class="rs-summary-card">
        <div class="label">Client Offer</div>
        <div class="value">{float(discount_percent):g}% off</div>
        <div class="foot">Displayed in {html.escape(selected_currency_symbol)}</div>
      </div>
    </div>
    """,
    unsafe_allow_html=True,
)

with st.expander("Search context & booking URL", expanded=False):
    st.caption(
        f"Booking provider: {hotel_provider.title()} · property code: {hotel_code} · sort: {sort} · booking currency: {currency}"
    )
    st.code(target_url, language=None)

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
if "last_retrieval_note" not in st.session_state:
    st.session_state.last_retrieval_note = ""

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

    with st.spinner("Retrieving live inventory and validating current selling rates…"):
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

            used_fallback = any(
                item.get("mode") == "fallback" and item.get("status") == "ok"
                for item in retry_history
            )
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
                st.session_state.last_retrieval_note = ""
                st.error(
                    "No live room rates were returned. The booking page may have changed, availability may not have loaded, or automated verification may have interrupted the request."
                )
                with st.expander("Technical diagnostics", expanded=False):
                    st.text(result.get("page_text_preview", "")[:3500])
                    st.code(result.get("html_preview", "")[:3500], language="html")
                    st.json(retry_history)
            else:
                output_lines = build_output_lines(rooms, discount_percent)
                st.session_state.last_output_text = "\n".join(output_lines)
                st.session_state.last_df = build_output_dataframe(rooms, discount_percent)
                st.session_state.last_rooms = rooms
                st.session_state.generated_email = ""
                st.session_state.last_retrieval_note = (
                    "Secondary retrieval pass used after the first pass returned no usable rate rows."
                    if used_fallback and primary_failed_or_empty
                    else "Primary live-rate retrieval completed successfully."
                )
                for index, room in enumerate(rooms):
                    room_key = get_room_selection_key(index, room)
                    st.session_state[room_key] = False
        except Exception as exc:
            st.session_state.last_error = str(exc)
            st.session_state.last_retrieval_note = ""
            st.error(f"The live-rate service could not complete the search: {exc}")
            with st.expander("Technical diagnostics", expanded=False):
                st.json(get_chrome_runtime())

rooms_for_selection = st.session_state.last_rooms
render_section_heading(
    "Room selection & offer builder",
    "Choose only the room types that should appear in the client-facing quote.",
    "Live Inventory",
)

if rooms_for_selection:
    selected_count = len(get_selected_rooms(rooms_for_selection))
    retrieval_note = html.escape(st.session_state.last_retrieval_note or "Live rates loaded.")
    st.markdown(
        f"""
        <div class="rs-status rs-status-success">
          <strong>{len(rooms_for_selection)} live room type(s) loaded.</strong>
          {selected_count} currently selected for the client quote. {retrieval_note}
        </div>
        """,
        unsafe_allow_html=True,
    )

    select_all_col, clear_all_col, spacer_col = st.columns([1.15, 1.15, 4.7], gap="small")
    with select_all_col:
        if st.button("Select all", use_container_width=True):
            for index, room in enumerate(rooms_for_selection):
                room_key = get_room_selection_key(index, room)
                st.session_state[room_key] = True
            st.rerun()
    with clear_all_col:
        if st.button("Clear selection", use_container_width=True):
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
        st.markdown(
            f'<div class="rs-category-head"><span>{html.escape(category)}</span><span class="rs-category-count">{len(category_rooms)} option(s)</span></div>',
            unsafe_allow_html=True,
        )
        for room in category_rooms:
            index = room_index_by_identity[id(room)]
            room_key = get_room_selection_key(index, room)
            current = int(room["current_selling"])
            offer = discount_price(current, discount_percent)
            saving = max(current - offer, 0)
            currency_symbol = str(room.get("currency_symbol") or selected_currency_symbol or "$")
            room_name = normalize_room_name(str(room.get("room_name", "Room")))
            with st.container(border=True):
                st.checkbox(room_name, key=room_key)
                st.markdown(
                    f"""
                    <div class="rs-room-pricing">
                      <div class="rs-room-stat">
                        <div class="label">Live selling rate</div>
                        <div class="value">{html.escape(format_money(current, currency_symbol))}</div>
                      </div>
                      <div class="rs-room-stat">
                        <div class="label">Client offer</div>
                        <div class="value">{html.escape(format_money(offer, currency_symbol))}</div>
                      </div>
                      <div class="rs-room-stat">
                        <div class="label">Offer saving</div>
                        <div class="value">{html.escape(format_money(saving, currency_symbol))} · {float(discount_percent):g}%</div>
                      </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
else:
    st.markdown(
        """
        <div class="rs-empty-state">
          <strong>No live rate result loaded.</strong> Set the search parameters in the left panel and select <strong>Search live rates</strong> to begin.
        </div>
        """,
        unsafe_allow_html=True,
    )

render_section_heading(
    "Client quote composer",
    "Keep the message concise, commercially consistent and ready for direct client use.",
    "Client Communication",
)

with st.container(border=True):
    email_left, email_right = st.columns([1, 1], gap="large")
    with email_left:
        st.markdown("**Opening message**")
        st.text_area(
            "Opening",
            key="email_opening",
            height=145,
            placeholder="Add a short personal opening for the client.",
            label_visibility="collapsed",
        )

    with email_right:
        st.markdown("**Closing and commercial terms**")
        st.text_area(
            "Ending",
            key="email_ending",
            height=145,
            label_visibility="collapsed",
        )

    save_template_col, save_template_spacer = st.columns([1.35, 4.65])
    with save_template_col:
        save_template_clicked = st.button("Save quote template", type="secondary", use_container_width=True)

    if save_template_clicked:
        render_local_storage_saver(
            opening=st.session_state.email_opening,
            ending=st.session_state.email_ending,
            rates_include_tax=bool(st.session_state.rates_include_tax),
        )

st.markdown("#### Generated client email")
st.caption("Generate after selecting room types. The output can be copied directly into your client email workflow.")

generate_col, spacer_col = st.columns([1.5, 4.5])
with generate_col:
    email_clicked = st.button("Generate client email", type="primary", use_container_width=True)

if email_clicked or st.session_state.generated_email:
    selected_lines_by_category = get_selected_room_lines_by_category(
        st.session_state.last_rooms,
        discount_percent,
    )
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
    height=330,
    label_visibility="collapsed",
)
copy_button_col, copy_button_spacer = st.columns([1.25, 4.75])
with copy_button_col:
    render_copy_button(st.session_state.generated_email)

render_section_heading(
    "Live rate result",
    "Commercial view first; raw detected price traces remain available for audit.",
    "Rate Detail",
)

if not st.session_state.last_df.empty:
    commercial_df = st.session_state.last_df.drop(columns=["Detected prices"], errors="ignore")
    st.dataframe(style_output_dataframe(commercial_df), use_container_width=True, hide_index=True)

    with st.expander("Rate detection audit", expanded=False):
        audit_columns = [column for column in ["Room Type", "Detected prices"] if column in st.session_state.last_df.columns]
        if audit_columns:
            st.dataframe(st.session_state.last_df[audit_columns], use_container_width=True, hide_index=True)
        st.caption("Audit values show source price candidates detected by the parser and are not client-facing output.")

    csv_data = st.session_state.last_df.to_csv(index=False).encode("utf-8-sig")
    download_col, download_spacer = st.columns([1.2, 4.8])
    with download_col:
        st.download_button(
            label="Download CSV",
            data=csv_data,
            file_name=f"starwood_{hotel_key}_{checkin}_{checkout}.csv",
            mime="text/csv",
            use_container_width=True,
        )
else:
    st.markdown(
        """
        <div class="rs-empty-state">
          <strong>No structured result yet.</strong> The commercial rate table will appear after a successful live-rate search.
        </div>
        """,
        unsafe_allow_html=True,
    )

