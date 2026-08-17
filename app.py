import base64
import copy
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
APP_VERSION = "2026-08-17 Starwood Hotel Rateshop cache-fastpath-v3"

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

# Successful live results are cached in the Streamlit server process.
# This is deliberately short because hotel inventory/pricing is dynamic.
DEFAULT_RATE_CACHE_MINUTES = 5
MAX_RATE_CACHE_ENTRIES = 256


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

STREAMLIT_SAGE_CSS = """
<style>
:root {
    --rs-bg: #E6ECE8;
    --rs-surface: #F3F6F4;
    --rs-surface-2: #EDF2EE;
    --rs-surface-3: #E3EBE6;
    --rs-hover: #DCE8E0;
    --rs-border: rgba(50, 91, 72, .14);
    --rs-border-strong: rgba(50, 91, 72, .26);
    --rs-text: #18352B;
    --rs-muted: #63796F;
    --rs-faint: #879A91;
    --rs-primary: #2D7B57;
    --rs-primary-hover: #236445;
    --rs-primary-soft: rgba(45, 123, 87, .11);
    --rs-success: #2F8F61;
    --rs-warning: #B87927;
    --rs-danger: #C55266;
    --rs-shadow: 0 18px 50px rgba(37, 76, 58, .12);
    --rs-shadow-soft: 0 8px 26px rgba(37, 76, 58, .08);
    --rs-radius-sm: 9px;
    --rs-radius: 13px;
    --rs-radius-lg: 18px;
    --rs-font: Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}

html, body, [class*="css"] {
    font-family: var(--rs-font) !important;
}

html, body, .stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"] {
    color-scheme: light !important;
    background:
        radial-gradient(circle at 16% -8%, rgba(101, 187, 139, .17), transparent 31%),
        radial-gradient(circle at 94% 0%, rgba(184, 222, 199, .23), transparent 28%),
        var(--rs-bg) !important;
    color: var(--rs-text) !important;
}

header[data-testid="stHeader"] {
    background: rgba(230, 236, 232, .82) !important;
    backdrop-filter: blur(14px);
    border-bottom: 1px solid rgba(50, 91, 72, .06);
}

[data-testid="stToolbar"], [data-testid="stDecoration"] {
    color: var(--rs-muted) !important;
}

.stMainBlockContainer,
section.main > div.block-container {
    max-width: 1540px !important;
    padding-top: 1.35rem !important;
    padding-bottom: 4rem !important;
}

/* Main typography */
h1, h2, h3, h4, h5, h6,
[data-testid="stMarkdownContainer"] h1,
[data-testid="stMarkdownContainer"] h2,
[data-testid="stMarkdownContainer"] h3 {
    color: var(--rs-text) !important;
    letter-spacing: -.025em;
}

p, label, span, div, small,
[data-testid="stMarkdownContainer"] p,
[data-testid="stCaptionContainer"] {
    color: inherit;
}

[data-testid="stCaptionContainer"], .stCaption, small {
    color: var(--rs-muted) !important;
}

a { color: var(--rs-primary) !important; }
a:hover { color: var(--rs-primary-hover) !important; }

/* Hero */
.rs-hero {
    position: relative;
    overflow: hidden;
    margin: .15rem 0 1.05rem 0;
    padding: 1.45rem 1.65rem 1.35rem;
    border: 1px solid var(--rs-border);
    border-radius: 22px;
    background:
        radial-gradient(circle at 84% 8%, rgba(81, 151, 112, .15), transparent 30%),
        linear-gradient(135deg, rgba(248, 251, 249, .97), rgba(233, 241, 236, .94));
    box-shadow: var(--rs-shadow-soft);
}
.rs-hero::after {
    content: "";
    position: absolute;
    inset: auto -28px -54px auto;
    width: 180px;
    height: 180px;
    border-radius: 999px;
    border: 1px solid rgba(45, 123, 87, .10);
    box-shadow: 0 0 0 22px rgba(45, 123, 87, .025), 0 0 0 45px rgba(45, 123, 87, .018);
}
.rs-eyebrow {
    color: var(--rs-primary);
    font-size: .68rem;
    font-weight: 850;
    letter-spacing: .15em;
    text-transform: uppercase;
}
.rs-hero h1 {
    margin: .33rem 0 .38rem;
    font-size: clamp(1.7rem, 2.2vw, 2.5rem);
    line-height: 1.05;
    font-weight: 790;
}
.rs-hero p {
    max-width: 780px;
    margin: 0;
    color: var(--rs-muted) !important;
    font-size: .95rem;
    line-height: 1.55;
}

/* Sidebar */
section[data-testid="stSidebar"] {
    background: linear-gradient(180deg, rgba(248, 251, 249, .985), rgba(236, 243, 239, .985)) !important;
    border-right: 1px solid var(--rs-border) !important;
    box-shadow: 12px 0 32px rgba(37, 76, 58, .045);
}
section[data-testid="stSidebar"] > div {
    background: transparent !important;
}
section[data-testid="stSidebar"] [data-testid="stSidebarContent"] {
    padding-top: 1rem !important;
}
.rs-sidebrand {
    margin: .1rem 0 .8rem;
    padding: 1rem 1rem .9rem;
    border: 1px solid var(--rs-border);
    border-radius: 16px;
    background: rgba(255, 255, 255, .47);
    box-shadow: 0 7px 20px rgba(37, 76, 58, .055);
}
.rs-sidebrand .kicker {
    color: var(--rs-primary);
    font-size: .63rem;
    font-weight: 850;
    letter-spacing: .14em;
    text-transform: uppercase;
}
.rs-sidebrand .name {
    margin-top: .3rem;
    color: var(--rs-text);
    font-size: 1.15rem;
    font-weight: 780;
}
.rs-sidebrand .meta {
    margin-top: .18rem;
    color: var(--rs-muted);
    font-size: .73rem;
}

/* Inputs, dropdowns, date pickers, text areas */
[data-testid="stTextInput"] input,
[data-testid="stNumberInput"] input,
[data-testid="stDateInput"] input,
[data-testid="stTextArea"] textarea,
[data-baseweb="input"] > div,
[data-baseweb="select"] > div,
[data-baseweb="base-input"],
.stTextInput input,
.stTextArea textarea {
    color: var(--rs-text) !important;
    background: var(--rs-surface-2) !important;
    border-color: var(--rs-border-strong) !important;
    border-radius: var(--rs-radius-sm) !important;
    box-shadow: none !important;
}

[data-testid="stTextInput"] input:focus,
[data-testid="stNumberInput"] input:focus,
[data-testid="stDateInput"] input:focus,
[data-testid="stTextArea"] textarea:focus,
[data-baseweb="select"] > div:focus-within,
[data-baseweb="input"] > div:focus-within {
    border-color: var(--rs-primary) !important;
    box-shadow: 0 0 0 3px var(--rs-primary-soft) !important;
}

input:disabled, textarea:disabled,
[data-testid="stTextInput"] input:disabled,
[data-testid="stTextArea"] textarea:disabled {
    color: #5D7469 !important;
    -webkit-text-fill-color: #5D7469 !important;
    opacity: 1 !important;
    background: #E4EBE7 !important;
}

::placeholder {
    color: var(--rs-faint) !important;
    opacity: 1 !important;
}

/* BaseWeb dropdown portal, calendar and menus */
[data-baseweb="popover"],
[data-baseweb="menu"],
[data-baseweb="select"] ul,
[role="listbox"] {
    background: #F5F8F6 !important;
    color: var(--rs-text) !important;
    border: 1px solid var(--rs-border-strong) !important;
    border-radius: 12px !important;
    box-shadow: var(--rs-shadow) !important;
}
[role="option"], [data-baseweb="menu"] li {
    color: var(--rs-text) !important;
    background: transparent !important;
}
[role="option"]:hover,
[role="option"][aria-selected="true"],
[data-baseweb="menu"] li:hover {
    color: #17372B !important;
    background: #DCE9E1 !important;
}

[data-baseweb="calendar"] {
    background: #F5F8F6 !important;
    color: var(--rs-text) !important;
}
[data-baseweb="calendar"] button {
    color: var(--rs-text) !important;
}
[data-baseweb="calendar"] [aria-selected="true"] {
    background: var(--rs-primary) !important;
    color: white !important;
}

/* Buttons */
.stButton > button,
.stDownloadButton > button,
[data-testid="stFormSubmitButton"] > button,
button[kind="secondary"] {
    min-height: 2.55rem;
    border: 1px solid var(--rs-border-strong) !important;
    border-radius: 11px !important;
    background: linear-gradient(180deg, #F7FAF8, #E9F0EC) !important;
    color: var(--rs-text) !important;
    font-weight: 760 !important;
    box-shadow: 0 5px 14px rgba(37, 76, 58, .065) !important;
    transition: transform .16s ease, border-color .16s ease, box-shadow .16s ease, background .16s ease;
}
.stButton > button:hover,
.stDownloadButton > button:hover,
[data-testid="stFormSubmitButton"] > button:hover,
button[kind="secondary"]:hover {
    border-color: rgba(45, 123, 87, .38) !important;
    background: #E0EBE4 !important;
    color: var(--rs-primary-hover) !important;
    transform: translateY(-1px);
    box-shadow: 0 8px 18px rgba(37, 76, 58, .10) !important;
}
.stButton > button[kind="primary"],
[data-testid="stFormSubmitButton"] > button[kind="primary"],
button[kind="primary"] {
    border-color: #2D7B57 !important;
    background: linear-gradient(135deg, #2D7B57, #236445) !important;
    color: #FFFFFF !important;
    -webkit-text-fill-color: #FFFFFF !important;
    box-shadow: 0 9px 22px rgba(45, 123, 87, .22) !important;
}
.stButton > button[kind="primary"] *,
[data-testid="stFormSubmitButton"] > button[kind="primary"] *,
button[kind="primary"] * {
    color: #FFFFFF !important;
    -webkit-text-fill-color: #FFFFFF !important;
}
.stButton > button[kind="primary"]:hover,
[data-testid="stFormSubmitButton"] > button[kind="primary"]:hover,
button[kind="primary"]:hover {
    background: linear-gradient(135deg, #286F4F, #1F5A3E) !important;
    color: #FFFFFF !important;
}

/* Toggle / checkbox / slider */
[data-testid="stCheckbox"] label p {
    color: var(--rs-text) !important;
    font-size: .94rem !important;
    font-weight: 650 !important;
    line-height: 1.38 !important;
}
[data-testid="stCheckbox"] label { gap: .45rem !important; }
[data-testid="stCheckbox"] input { accent-color: var(--rs-primary) !important; }
[data-baseweb="checkbox"] > div:first-child {
    border-color: var(--rs-border-strong) !important;
}
[data-testid="stSlider"] [role="slider"] {
    background: var(--rs-primary) !important;
    border-color: var(--rs-primary) !important;
}
[data-testid="stSlider"] div[data-baseweb="slider"] > div > div {
    background: rgba(45, 123, 87, .22) !important;
}

/* Forms / expanders / cards */
[data-testid="stForm"] {
    border: 1px solid var(--rs-border) !important;
    border-radius: var(--rs-radius-lg) !important;
    background: rgba(243, 246, 244, .94) !important;
    box-shadow: var(--rs-shadow-soft) !important;
}
[data-testid="stExpander"] {
    overflow: hidden;
    border: 1px solid var(--rs-border) !important;
    border-radius: var(--rs-radius) !important;
    background: rgba(243, 246, 244, .78) !important;
    box-shadow: none !important;
}
[data-testid="stExpander"] summary:hover {
    background: var(--rs-hover) !important;
}
hr {
    border-color: rgba(50, 91, 72, .12) !important;
}

/* Status messages */
[data-testid="stAlert"] {
    border-radius: var(--rs-radius) !important;
    border-width: 1px !important;
    box-shadow: none !important;
}
[data-testid="stAlert"] p { color: var(--rs-text) !important; }
.stSuccess, [data-testid="stAlert"]:has([data-testid="stNotificationContentSuccess"]) {
    background: rgba(47, 143, 97, .09) !important;
    border-color: rgba(47, 143, 97, .24) !important;
}
.stWarning, [data-testid="stAlert"]:has([data-testid="stNotificationContentWarning"]) {
    background: rgba(184, 121, 39, .10) !important;
    border-color: rgba(184, 121, 39, .24) !important;
}
.stError, [data-testid="stAlert"]:has([data-testid="stNotificationContentError"]) {
    background: rgba(197, 82, 102, .09) !important;
    border-color: rgba(197, 82, 102, .24) !important;
}

/* Metrics and technical summary */
[data-testid="stMetric"] {
    padding: .82rem .95rem !important;
    border: 1px solid var(--rs-border) !important;
    border-radius: 14px !important;
    background: rgba(243, 246, 244, .92) !important;
    box-shadow: 0 5px 15px rgba(37, 76, 58, .045) !important;
}
[data-testid="stMetricLabel"] { color: var(--rs-muted) !important; }
[data-testid="stMetricValue"] { color: var(--rs-text) !important; font-weight: 780 !important; }

.rs-summary-grid {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: .72rem;
    margin: .15rem 0 .95rem;
}
.rs-summary-card {
    min-width: 0;
    padding: .86rem .95rem .82rem;
    border: 1px solid var(--rs-border);
    border-radius: 14px;
    background: rgba(243, 246, 244, .91);
    box-shadow: 0 5px 15px rgba(37, 76, 58, .045);
}
.rs-summary-card .label {
    color: var(--rs-muted);
    font-size: .69rem;
    font-weight: 780;
    letter-spacing: .055em;
    text-transform: uppercase;
}
.rs-summary-card .value {
    margin-top: .26rem;
    color: var(--rs-text);
    font-size: 1.15rem;
    font-weight: 790;
    line-height: 1.15;
}
.rs-summary-card .foot {
    margin-top: .2rem;
    color: var(--rs-muted);
    font-size: .72rem;
    line-height: 1.35;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}

/* Dataframe shell */
[data-testid="stDataFrame"],
[data-testid="stTable"] {
    overflow: hidden;
    border: 1px solid var(--rs-border) !important;
    border-radius: 14px !important;
    background: #F2F5F3 !important;
    box-shadow: var(--rs-shadow-soft) !important;
}
[data-testid="stDataFrame"] iframe,
[data-testid="stDataFrame"] canvas {
    border-radius: 12px !important;
}

/* Code, JSON and URL detail */
[data-testid="stCodeBlock"], pre, code {
    color: #254238 !important;
    background: #E8EFEB !important;
    border-color: var(--rs-border) !important;
}

/* Spinner */
[data-testid="stSpinner"] > div { border-top-color: var(--rs-primary) !important; }

/* Scrollbars */
*::-webkit-scrollbar { width: 10px; height: 10px; }
*::-webkit-scrollbar-track { background: transparent; }
*::-webkit-scrollbar-thumb {
    border: 3px solid transparent;
    border-radius: 999px;
    background: rgba(45, 123, 87, .26);
    background-clip: padding-box;
}
*::-webkit-scrollbar-thumb:hover { background: rgba(45, 123, 87, .42); background-clip: padding-box; }

/* Tighten section rhythm */
[data-testid="stVerticalBlock"] > div:has(> [data-testid="stHeadingWithActionElements"]) {
    margin-top: .2rem;
}


/* ==========================================================
   HARD LIGHT-CONTROL GUARD
   Streamlit/BaseWeb can render portal widgets outside the normal app tree.
   These rules deliberately remove browser/dark-theme black surfaces from
   Selectbox, DateInput calendar, NumberInput steppers and menu portals.
   ========================================================== */
:root,
html,
body,
.stApp,
[data-testid="stAppViewContainer"] {
    color-scheme: light only !important;
    --primary-color: #2D7B57 !important;
    --background-color: #E7ECE9 !important;
    --secondary-background-color: #F2F5F3 !important;
    --text-color: #18352B !important;
}

/* Selectbox: never allow the BaseWeb dark shell to leak through. */
[data-testid="stSelectbox"],
[data-testid="stSelectbox"] > div,
[data-testid="stSelectbox"] [data-baseweb="select"],
[data-testid="stSelectbox"] [data-baseweb="select"] > div,
[data-testid="stSelectbox"] [data-baseweb="value-container"],
[data-testid="stSelectbox"] [data-baseweb="select"] input,
[data-testid="stSelectbox"] [role="combobox"] {
    background-color: #F5F8F6 !important;
    color: #18352B !important;
    -webkit-text-fill-color: #18352B !important;
}
[data-testid="stSelectbox"] [data-baseweb="select"] > div {
    border-color: #B9CBC1 !important;
    box-shadow: none !important;
}
[data-testid="stSelectbox"] svg,
[data-testid="stSelectbox"] [data-baseweb="select"] svg {
    color: #49685A !important;
    fill: #49685A !important;
}

/* Dropdown/menu portals are attached near <body>, outside the sidebar. */
body > div[data-baseweb="popover"],
body > div[data-baseweb="popover"] > div,
[data-baseweb="popover"],
[data-baseweb="popover"] > div,
[data-baseweb="menu"],
[data-baseweb="menu"] > div,
ul[role="listbox"],
[role="listbox"] {
    background-color: #F8FAF9 !important;
    color: #18352B !important;
    border-color: #B9CBC1 !important;
}
[role="option"],
[role="option"] *,
[data-baseweb="menu"] li,
[data-baseweb="menu"] li * {
    color: #29483B !important;
    -webkit-text-fill-color: #29483B !important;
    background-color: transparent !important;
}
[role="option"]:hover,
[role="option"][aria-selected="true"],
[data-baseweb="menu"] li:hover {
    background-color: #E0ECE5 !important;
    color: #17372B !important;
}

/* Date input itself. */
[data-testid="stDateInput"],
[data-testid="stDateInput"] > div,
[data-testid="stDateInput"] [data-baseweb="input"],
[data-testid="stDateInput"] [data-baseweb="input"] > div,
[data-testid="stDateInput"] [data-baseweb="base-input"],
[data-testid="stDateInput"] input {
    background-color: #F5F8F6 !important;
    color: #18352B !important;
    -webkit-text-fill-color: #18352B !important;
}
[data-testid="stDateInput"] svg {
    color: #49685A !important;
    fill: #49685A !important;
}
[data-testid="stDateInput"] [data-baseweb="input"] > div,
[data-testid="stDateInput"] [data-baseweb="base-input"] {
    border-color: #B9CBC1 !important;
    outline: none !important;
    box-shadow: none !important;
}
[data-testid="stDateInput"]:focus-within [data-baseweb="input"] > div,
[data-testid="stDateInput"]:focus-within [data-baseweb="base-input"],
[data-testid="stDateInput"] [data-baseweb="input"] > div:focus-within {
    border-color: #2D7B57 !important;
    outline: none !important;
    box-shadow: 0 0 0 3px rgba(45, 123, 87, .11) !important;
}

/* Calendar: flatten all dark internal wrappers to one clean light surface. */
[data-baseweb="calendar"],
[data-baseweb="calendar"] > div,
[data-baseweb="calendar"] > div > div,
[data-baseweb="calendar"] [role="grid"],
[data-baseweb="calendar"] [role="row"],
[data-baseweb="calendar"] [role="columnheader"],
[data-baseweb="calendar"] [role="gridcell"] {
    background-color: #F8FAF9 !important;
    color: #29483B !important;
    border-color: #D4DFD9 !important;
}
[data-baseweb="calendar"] div {
    color: #29483B !important;
}
[data-baseweb="calendar"] [role="columnheader"],
[data-baseweb="calendar"] [role="columnheader"] * {
    color: #6A7F75 !important;
}
[data-baseweb="calendar"] button,
[data-baseweb="calendar"] button * {
    color: #29483B !important;
    -webkit-text-fill-color: #29483B !important;
}
[data-baseweb="calendar"] button {
    background-color: transparent !important;
    border-color: transparent !important;
    box-shadow: none !important;
}
[data-baseweb="calendar"] button:hover:not(:disabled) {
    background-color: #E3EEE7 !important;
    color: #17372B !important;
}
[data-baseweb="calendar"] button:disabled,
[data-baseweb="calendar"] button:disabled * {
    background-color: transparent !important;
    color: #A2B1A9 !important;
    -webkit-text-fill-color: #A2B1A9 !important;
    opacity: .62 !important;
}
[data-baseweb="calendar"] [aria-selected="true"],
[data-baseweb="calendar"] [aria-selected="true"] *,
[data-baseweb="calendar"] [aria-current="date"],
[data-baseweb="calendar"] [aria-current="date"] * {
    background-color: #2D7B57 !important;
    color: #FFFFFF !important;
    -webkit-text-fill-color: #FFFFFF !important;
    border-color: #2D7B57 !important;
}
/* The month/year header can be a nested select; keep it light as well. */
[data-baseweb="calendar"] [data-baseweb="select"],
[data-baseweb="calendar"] [data-baseweb="select"] > div,
[data-baseweb="calendar"] [role="combobox"] {
    background-color: #EEF4F0 !important;
    color: #29483B !important;
    border-color: #C5D4CC !important;
}
[data-baseweb="calendar"] svg {
    color: #49685A !important;
    fill: #49685A !important;
}

/* Number-input steppers. Streamlit may render these with palette colors from
   the active theme. Keep both + and - neutral Sage, including hover/focus. */
[data-testid="stNumberInput"] button,
[data-testid="stNumberInput"] button:hover,
[data-testid="stNumberInput"] button:focus,
[data-testid="stNumberInput"] button:active {
    background-color: #E8F0EB !important;
    color: #315546 !important;
    -webkit-text-fill-color: #315546 !important;
    border-color: #C3D2CA !important;
    box-shadow: none !important;
}
[data-testid="stNumberInput"] button svg {
    color: #315546 !important;
    fill: #315546 !important;
}

/* Checkbox box should stay Sage/neutral instead of charcoal. */
[data-testid="stCheckbox"] [data-baseweb="checkbox"] > div:first-child {
    background-color: #F8FAF9 !important;
    border-color: #AFC3B8 !important;
}
[data-testid="stCheckbox"] input:checked + div,
[data-testid="stCheckbox"] [aria-checked="true"] > div:first-child {
    background-color: #2D7B57 !important;
    border-color: #2D7B57 !important;
}

/* Last-resort protection for native form widgets under a dark OS preference. */
input,
textarea,
select,
button {
    color-scheme: light !important;
}

@media (max-width: 900px) {
    .rs-summary-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .stMainBlockContainer, section.main > div.block-container {
        padding-left: .85rem !important;
        padding-right: .85rem !important;
    }
    .rs-hero { padding: 1.15rem 1.15rem 1.05rem; border-radius: 17px; }
}

/* ==========================================================
   FINAL PORTAL OVERRIDES
   BaseWeb mounts select menus and the date picker in a portal under <body>.
   Some Streamlit builds also put charcoal fills on nested anonymous wrappers,
   so the selectors below intentionally flatten every nested portal surface.
   ========================================================== */
body [data-baseweb="popover"],
body [data-baseweb="popover"] > div,
body [data-baseweb="popover"] > div > div,
body [data-baseweb="popover"] section,
body [data-baseweb="popover"] ul,
body [data-baseweb="popover"] li {
    color-scheme: light !important;
    background: #F7FAF8 !important;
    background-color: #F7FAF8 !important;
    color: #1D3A2F !important;
    border-color: #C3D2CA !important;
}

/* Hotel select: the arrow area is an anonymous child wrapper in current Streamlit. */
[data-testid="stSelectbox"] [data-baseweb="select"],
[data-testid="stSelectbox"] [data-baseweb="select"] > div {
    background: #F5F8F6 !important;
    background-color: #F5F8F6 !important;
    color: #18352B !important;
    border-color: #AFC3B8 !important;
}
[data-testid="stSelectbox"] [data-baseweb="select"] > div > div,
[data-testid="stSelectbox"] [data-baseweb="select"] > div > div > div,
[data-testid="stSelectbox"] [data-baseweb="value-container"] {
    background: transparent !important;
    background-color: transparent !important;
    color: #18352B !important;
}
[data-testid="stSelectbox"] [data-baseweb="select"] svg,
[data-testid="stSelectbox"] [role="combobox"] svg {
    color: #49685A !important;
    fill: #49685A !important;
}

/* Date input shell: remove Streamlit theme error/red focus rings. */
[data-testid="stDateInput"],
[data-testid="stDateInput"] > div,
[data-testid="stDateInput"] [data-baseweb="input"],
[data-testid="stDateInput"] [data-baseweb="input"] > div,
[data-testid="stDateInput"] [data-baseweb="base-input"],
[data-testid="stDateInput"] [data-baseweb="base-input"] > div {
    background: #F5F8F6 !important;
    background-color: #F5F8F6 !important;
    border-color: #AFC3B8 !important;
    outline-color: transparent !important;
    box-shadow: none !important;
}
[data-testid="stDateInput"]:focus-within,
[data-testid="stDateInput"]:focus-within > div,
[data-testid="stDateInput"]:focus-within [data-baseweb="input"],
[data-testid="stDateInput"]:focus-within [data-baseweb="input"] > div,
[data-testid="stDateInput"]:focus-within [data-baseweb="base-input"] {
    border-color: #2D7B57 !important;
    outline: 0 !important;
    box-shadow: 0 0 0 3px rgba(45, 123, 87, .10) !important;
}

/* Calendar: flatten every anonymous wrapper so no charcoal rectangles survive. */
body [data-baseweb="popover"] [data-baseweb="calendar"],
body [data-baseweb="popover"] [data-baseweb="calendar"] > div,
body [data-baseweb="popover"] [data-baseweb="calendar"] div,
body [data-baseweb="popover"] [data-baseweb="calendar"] section,
body [data-baseweb="popover"] [data-baseweb="calendar"] header,
body [data-baseweb="popover"] [data-baseweb="calendar"] table,
body [data-baseweb="popover"] [data-baseweb="calendar"] thead,
body [data-baseweb="popover"] [data-baseweb="calendar"] tbody,
body [data-baseweb="popover"] [data-baseweb="calendar"] tr,
body [data-baseweb="popover"] [data-baseweb="calendar"] th,
body [data-baseweb="popover"] [data-baseweb="calendar"] td,
body [data-baseweb="popover"] [data-baseweb="calendar"] [role="grid"],
body [data-baseweb="popover"] [data-baseweb="calendar"] [role="row"],
body [data-baseweb="popover"] [data-baseweb="calendar"] [role="gridcell"],
body [data-baseweb="popover"] [data-baseweb="calendar"] [role="columnheader"] {
    color-scheme: light !important;
    background: transparent !important;
    background-color: transparent !important;
    color: #29483B !important;
    border-color: #D4DFD9 !important;
    box-shadow: none !important;
}
body [data-baseweb="popover"] [data-baseweb="calendar"] {
    background: #F7FAF8 !important;
    background-color: #F7FAF8 !important;
}
body [data-baseweb="popover"] [data-baseweb="calendar"] button,
body [data-baseweb="popover"] [data-baseweb="calendar"] button * {
    background: transparent !important;
    background-color: transparent !important;
    color: #29483B !important;
    -webkit-text-fill-color: #29483B !important;
    border-color: transparent !important;
    box-shadow: none !important;
}
body [data-baseweb="popover"] [data-baseweb="calendar"] button:hover:not(:disabled) {
    background: #E1ECE5 !important;
    background-color: #E1ECE5 !important;
    color: #17372B !important;
}
body [data-baseweb="popover"] [data-baseweb="calendar"] [aria-selected="true"],
body [data-baseweb="popover"] [data-baseweb="calendar"] [aria-selected="true"] *,
body [data-baseweb="popover"] [data-baseweb="calendar"] [aria-current="date"],
body [data-baseweb="popover"] [data-baseweb="calendar"] [aria-current="date"] * {
    background: #2D7B57 !important;
    background-color: #2D7B57 !important;
    color: #FFFFFF !important;
    -webkit-text-fill-color: #FFFFFF !important;
    border-color: #2D7B57 !important;
}
body [data-baseweb="popover"] [data-baseweb="calendar"] [aria-selected="true"]::before,
body [data-baseweb="popover"] [data-baseweb="calendar"] [aria-selected="true"]::after,
body [data-baseweb="popover"] [data-baseweb="calendar"] [aria-current="date"]::before,
body [data-baseweb="popover"] [data-baseweb="calendar"] [aria-current="date"]::after {
    background: #2D7B57 !important;
    background-color: #2D7B57 !important;
}
body [data-baseweb="popover"] [data-baseweb="calendar"] button:disabled,
body [data-baseweb="popover"] [data-baseweb="calendar"] button:disabled * {
    background: transparent !important;
    color: #A1B0A8 !important;
    -webkit-text-fill-color: #A1B0A8 !important;
}

/* Month / year selector inside the calendar. */
body [data-baseweb="popover"] [data-baseweb="calendar"] [data-baseweb="select"],
body [data-baseweb="popover"] [data-baseweb="calendar"] [data-baseweb="select"] > div,
body [data-baseweb="popover"] [data-baseweb="calendar"] [data-baseweb="select"] > div > div,
body [data-baseweb="popover"] [data-baseweb="calendar"] [role="combobox"] {
    background: #EEF4F0 !important;
    background-color: #EEF4F0 !important;
    color: #29483B !important;
    border-color: #C3D2CA !important;
}
</style>
"""

st.markdown(STREAMLIT_SAGE_CSS, unsafe_allow_html=True)



def render_portal_light_guard() -> None:
    """Force BaseWeb portal widgets to stay on the app's light Sage palette.

    Streamlit mounts select menus and calendars outside the normal app subtree.
    On some Streamlit/BaseWeb releases, anonymous wrappers carry inline dark
    backgrounds that are more specific than application CSS. This lightweight
    MutationObserver restyles only those portal controls after they are mounted.
    """
    components.html(
        r"""
        <script>
        (() => {
          const doc = window.parent && window.parent.document ? window.parent.document : document;
          const COLORS = {
            surface: '#F7FAF8',
            field: '#F5F8F6',
            soft: '#EEF4F0',
            hover: '#E1ECE5',
            border: '#B9CBC1',
            text: '#18352B',
            muted: '#49685A',
            disabled: '#A1B0A8',
            primary: '#2D7B57',
            white: '#FFFFFF'
          };

          const setImportant = (el, prop, value) => {
            if (el && el.style) el.style.setProperty(prop, value, 'important');
          };
          const paint = (el, bg, color = COLORS.text) => {
            if (!el || !el.style) return;
            setImportant(el, 'color-scheme', 'light');
            setImportant(el, 'background', bg);
            setImportant(el, 'background-color', bg);
            setImportant(el, 'color', color);
            setImportant(el, '-webkit-text-fill-color', color);
          };

          function styleSelectboxes() {
            doc.querySelectorAll('[data-testid="stSelectbox"] [data-baseweb="select"]').forEach(select => {
              paint(select, COLORS.field);
              const shell = select.firstElementChild;
              if (shell) {
                paint(shell, COLORS.field);
                setImportant(shell, 'border-color', COLORS.border);
                setImportant(shell, 'box-shadow', 'none');
              }
              select.querySelectorAll('div').forEach(div => {
                if (div !== shell) {
                  setImportant(div, 'background', 'transparent');
                  setImportant(div, 'background-color', 'transparent');
                  setImportant(div, 'color', COLORS.text);
                  setImportant(div, '-webkit-text-fill-color', COLORS.text);
                }
              });
              select.querySelectorAll('svg').forEach(svg => {
                setImportant(svg, 'color', COLORS.muted);
                setImportant(svg, 'fill', COLORS.muted);
              });
            });
          }

          function styleDateInputs() {
            doc.querySelectorAll('[data-testid="stDateInput"]').forEach(root => {
              root.querySelectorAll('[data-baseweb="input"], [data-baseweb="base-input"]').forEach(el => {
                paint(el, COLORS.field);
                setImportant(el, 'border-color', COLORS.border);
                setImportant(el, 'box-shadow', 'none');
                setImportant(el, 'outline', 'none');
              });
              root.querySelectorAll('input').forEach(input => paint(input, COLORS.field));
              root.querySelectorAll('svg').forEach(svg => {
                setImportant(svg, 'color', COLORS.muted);
                setImportant(svg, 'fill', COLORS.muted);
              });
            });
          }

          function styleCalendar(calendar) {
            paint(calendar, COLORS.surface);
            calendar.querySelectorAll('div, section, header, table, thead, tbody, tr, th, td').forEach(el => {
              setImportant(el, 'background', 'transparent');
              setImportant(el, 'background-color', 'transparent');
              setImportant(el, 'color', COLORS.text);
              setImportant(el, '-webkit-text-fill-color', COLORS.text);
              setImportant(el, 'box-shadow', 'none');
            });
            calendar.querySelectorAll('button').forEach(button => {
              const selected = button.getAttribute('aria-selected') === 'true' || button.getAttribute('aria-current') === 'date';
              const disabled = button.disabled || button.getAttribute('aria-disabled') === 'true';
              paint(button, selected ? COLORS.primary : 'transparent', selected ? COLORS.white : (disabled ? COLORS.disabled : COLORS.text));
              setImportant(button, 'border-color', selected ? COLORS.primary : 'transparent');
              setImportant(button, 'box-shadow', 'none');
              button.querySelectorAll('*').forEach(child => {
                setImportant(child, 'background', 'transparent');
                setImportant(child, 'background-color', 'transparent');
                setImportant(child, 'color', selected ? COLORS.white : (disabled ? COLORS.disabled : COLORS.text));
                setImportant(child, '-webkit-text-fill-color', selected ? COLORS.white : (disabled ? COLORS.disabled : COLORS.text));
              });
            });
            calendar.querySelectorAll('[aria-selected="true"], [aria-current="date"]').forEach(el => {
              paint(el, COLORS.primary, COLORS.white);
              setImportant(el, 'border-color', COLORS.primary);
            });
            calendar.querySelectorAll('[data-baseweb="select"]').forEach(select => {
              paint(select, COLORS.soft);
              select.querySelectorAll('div').forEach(div => {
                setImportant(div, 'background', 'transparent');
                setImportant(div, 'background-color', 'transparent');
                setImportant(div, 'color', COLORS.text);
              });
            });
          }

          function stylePortals() {
            doc.querySelectorAll('[data-baseweb="popover"]').forEach(popover => {
              paint(popover, COLORS.surface);
              setImportant(popover, 'border-color', COLORS.border);
              popover.querySelectorAll(':scope > div, :scope > div > div').forEach(el => paint(el, COLORS.surface));
              const calendar = popover.querySelector('[data-baseweb="calendar"]');
              if (calendar) {
                styleCalendar(calendar);
              } else {
                popover.querySelectorAll('[role="listbox"], [data-baseweb="menu"], ul').forEach(el => paint(el, COLORS.surface));
                popover.querySelectorAll('[role="option"], li').forEach(el => {
                  paint(el, el.getAttribute('aria-selected') === 'true' ? COLORS.hover : 'transparent', COLORS.text);
                });
              }
            });
          }

          function apply() {
            styleSelectboxes();
            styleDateInputs();
            stylePortals();
          }

          apply();
          let scheduled = false;
          const observer = new MutationObserver(() => {
            if (scheduled) return;
            scheduled = true;
            window.requestAnimationFrame(() => {
              scheduled = false;
              apply();
            });
          });
          observer.observe(doc.documentElement, {subtree: true, childList: true});

          // Re-apply on pointer/focus because BaseWeb may repaint inline styles
          // after a menu opens or a date receives focus.
          ['pointerdown', 'focusin', 'click'].forEach(evt => {
            doc.addEventListener(evt, () => window.setTimeout(apply, 0), true);
          });
        })();
        </script>
        """,
        height=0,
    )


render_portal_light_guard()


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

    st.title("🔐 Starwood Hotel Rateshop Login")
    st.caption(
        f"A successful login will be remembered in this browser for up to "
        f"{get_remember_login_days()} day(s). No plaintext password is stored."
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
    # Keep the temporary profile path attached to the Options object so the caller
    # can delete it after driver.quit(). Chrome itself does not remove --user-data-dir.
    setattr(chrome_options, "_starwood_user_data_dir", user_data_dir)
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
    # The booking UI does not need hotel photography to expose room names/prices.
    # Blocking images removes a large amount of bandwidth/decoding work while keeping
    # JavaScript, XHR/fetch and CSS enabled. This noticeably helps long/future searches.
    chrome_options.add_experimental_option(
        "prefs",
        {
            "profile.managed_default_content_settings.images": 2,
            "profile.default_content_setting_values.notifications": 2,
            "credentials_enable_service": False,
            "profile.password_manager_enabled": False,
        },
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
    profile_dir = str(getattr(chrome_options, "_starwood_user_data_dir", "") or "")
    try:
        driver = webdriver.Chrome(service=service, options=chrome_options)
    except Exception:
        if profile_dir:
            shutil.rmtree(profile_dir, ignore_errors=True)
        raise
    setattr(driver, "_starwood_user_data_dir", profile_dir)
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


def wait_for_booking_app_ready(
    driver: webdriver.Chrome,
    max_seconds: float = 14.0,
    empty_root_grace_seconds: float = 5.0,
) -> Dict[str, object]:
    """
    Wait for the React/Selfbook booking app to hydrate, not merely for <body>.

    The old implementation could spend the entire long-date timeout staring at an
    already-known empty #root before deciding to refresh. Here an empty root gets a
    short grace period; once it is clearly stuck we return early so the caller can
    refresh immediately instead of burning another 20-30 seconds.
    """
    start_time = time.monotonic()
    states: List[Dict[str, object]] = []
    last_state: Dict[str, object] = {}
    empty_root_started_at: Optional[float] = None

    max_seconds = max(4.0, float(max_seconds))
    empty_root_grace_seconds = max(2.5, min(float(empty_root_grace_seconds), max_seconds))

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
        root_exists = bool(state.get("rootExists", False))
        root_child_count = int(state.get("rootChildCount", 0) or 0)
        body_text_length = int(state.get("bodyTextLength", 0) or 0)

        if title_count >= 1 and (card_count >= 1 or price_found):
            break
        if root_child_count > 0 and body_text_length > 900 and (title_count >= 1 or price_found):
            break

        now = time.monotonic()
        if root_exists and root_child_count == 0 and title_count == 0 and not price_found:
            if empty_root_started_at is None:
                empty_root_started_at = now
            elif now - empty_root_started_at >= empty_root_grace_seconds:
                # Signal the caller to refresh now. Do not spend the whole long-date
                # timeout on a React root that has shown no sign of hydration.
                break
        else:
            empty_root_started_at = None

        try:
            scroll_booking_page_once(driver, len(states))
        except Exception:
            pass
        time.sleep(0.55)

    return {
        "elapsed_seconds": round(time.monotonic() - start_time, 2),
        "last_state": last_state,
        "samples": states[-4:],
        "empty_root_early_exit": bool(
            last_state.get("rootExists", False)
            and int(last_state.get("rootChildCount", 0) or 0) == 0
            and int(last_state.get("titleCount", 0) or 0) == 0
            and not bool(last_state.get("priceTextFound", False))
        ),
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
        retry_ready = wait_for_booking_app_ready(driver, max_seconds=max(8.0, min(float(wait_seconds), 16.0)), empty_root_grace_seconds=5.0)
        retry_ready["reloaded_empty_root"] = True
        return retry_ready

    app_ready_result["reloaded_empty_root"] = False
    return app_ready_result

def count_current_context_iframes(driver: webdriver.Chrome) -> int:
    try:
        return len(driver.find_elements(By.CSS_SELECTOR, "iframe"))
    except Exception:
        return 0


def collect_rooms_from_all_browser_contexts(driver: webdriver.Chrome, max_depth: int = 2) -> Dict[str, object]:
    """
    Parse prices from the current document and, only when necessary, nested iframes.

    Fast path: the current Selfbook build usually renders room cards in the top
    document. The previous code recursively scanned iframe trees up to depth 4 on
    every poll cycle and also walked the full DOM twice per context. That cost grows
    sharply on pages containing analytics/payment frames. We now parse once and only
    descend into frames if the top document did not yield rooms.
    """
    raw_rooms: List[Dict] = []
    contexts_checked = 0
    iframe_count = 0
    frame_errors: List[str] = []
    seen_price_text = False

    def visit(depth: int) -> bool:
        nonlocal contexts_checked, iframe_count, seen_price_text
        contexts_checked += 1

        parsed_here: List[Dict] = []
        try:
            parsed_here = parse_rooms_with_browser_dom(driver)
            raw_rooms.extend(parsed_here)
            if parsed_here:
                seen_price_text = True
        except Exception as exc:
            frame_errors.append(f"parse depth {depth}: {exc}")

        # Avoid another complete shadow-DOM text walk on successful contexts.
        if not parsed_here:
            try:
                if current_context_has_price_text(driver):
                    seen_price_text = True
            except Exception:
                pass

        if parsed_here:
            return True
        if depth >= max_depth:
            return False

        try:
            frames = driver.find_elements(By.CSS_SELECTOR, "iframe")
        except Exception as exc:
            frame_errors.append(f"iframe lookup depth {depth}: {exc}")
            return False

        iframe_count += len(frames)
        found_in_child = False
        for index in range(len(frames)):
            try:
                frames = driver.find_elements(By.CSS_SELECTOR, "iframe")
                if index >= len(frames):
                    break
                driver.switch_to.frame(frames[index])
                if visit(depth + 1):
                    found_in_child = True
                driver.switch_to.parent_frame()
                # One successful frame is enough for the current poll. Additional
                # room cards will be picked up on the next poll if the site changes.
                if found_in_child:
                    break
            except Exception as exc:
                frame_errors.append(f"iframe depth {depth} index {index}: {exc}")
                try:
                    driver.switch_to.parent_frame()
                except Exception:
                    try:
                        driver.switch_to.default_content()
                    except Exception:
                        pass
        return found_in_child

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
    """Do one compact full-page scroll sweep before parsing prices."""
    for index in range(4):
        scroll_booking_page_once(driver, index)
        time.sleep(0.18)
    try:
        driver.execute_script("window.scrollTo(0, 0);")
    except Exception:
        pass


def warm_up_lazy_loaded_rates_all_contexts(driver: webdriver.Chrome, max_depth: int = 2) -> Dict[str, object]:
    """Warm the top document first; touch frames only if top-level rooms are absent."""
    contexts_scrolled = 0
    frame_errors: List[str] = []

    try:
        driver.switch_to.default_content()
    except Exception:
        pass

    contexts_scrolled += 1
    warm_up_lazy_loaded_rates(driver)

    # Most current searches are top-document React. If rooms are already visible,
    # recursively scrolling analytics/payment iframes is pure overhead.
    try:
        top_rooms = dedupe_rooms(parse_rooms_with_browser_dom(driver))
    except Exception:
        top_rooms = []

    if top_rooms:
        return {"contexts_scrolled": contexts_scrolled, "frame_errors": frame_errors, "top_level_fast_path": True}

    def visit_frames(depth: int) -> bool:
        nonlocal contexts_scrolled
        if depth >= max_depth:
            return False
        try:
            frames = driver.find_elements(By.CSS_SELECTOR, "iframe")
        except Exception as exc:
            frame_errors.append(f"iframe lookup depth {depth}: {exc}")
            return False

        for index in range(len(frames)):
            try:
                frames = driver.find_elements(By.CSS_SELECTOR, "iframe")
                if index >= len(frames):
                    break
                driver.switch_to.frame(frames[index])
                contexts_scrolled += 1
                warm_up_lazy_loaded_rates(driver)
                try:
                    rooms_here = dedupe_rooms(parse_rooms_with_browser_dom(driver))
                except Exception:
                    rooms_here = []
                if rooms_here:
                    driver.switch_to.parent_frame()
                    return True
                if visit_frames(depth + 1):
                    driver.switch_to.parent_frame()
                    return True
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
        return False

    found_in_frame = visit_frames(0)
    try:
        driver.switch_to.default_content()
    except Exception:
        pass

    return {
        "contexts_scrolled": contexts_scrolled,
        "frame_errors": frame_errors[:8],
        "top_level_fast_path": False,
        "found_in_frame": found_in_frame,
    }

def room_fingerprint(rooms: List[Dict]) -> str:
    parts = []
    for room in rooms:
        parts.append(f"{normalize_room_name(str(room.get('room_name', ''))).lower()}={room.get('current_selling')}")
    return "|".join(sorted(parts))


def poll_rooms_after_page_open(
    driver: webdriver.Chrome,
    max_seconds: float = 10.0,
    min_seconds: float = 2.5,
) -> Dict[str, object]:
    """Poll the open booking page, using a top-document fast path before iframe fallbacks."""
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
        time.sleep(0.32)

        context_result = collect_rooms_from_all_browser_contexts(driver, max_depth=2)
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
        # Two stable reads are sufficient after the minimum window. The old five-cycle
        # requirement made every successful search pay several extra full-DOM walks.
        if best_rooms and elapsed >= min_seconds and stable_cycles >= 2:
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
        app_ready_initial = wait_for_booking_app_ready(
            driver,
            max_seconds=max(8.0, min(float(wait_seconds), 16.0)),
            empty_root_grace_seconds=5.0,
        )
        if fallback_mode:
            # The fallback browser is already the second attempt. Do not stack another
            # full refresh cycle inside it when #root is empty again.
            app_ready_result = dict(app_ready_initial)
            app_ready_result["reloaded_empty_root"] = False
        else:
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
        effective_price_poll_seconds = max(5.0, float(price_poll_seconds)) + (2.0 if fallback_mode else 0.0)
        minimum_price_poll_seconds = min(3.0 if not fallback_mode else 4.0, effective_price_poll_seconds)
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
            profile_dir = str(getattr(driver, "_starwood_user_data_dir", "") or "")
            try:
                driver.quit()
            finally:
                if profile_dir:
                    shutil.rmtree(profile_dir, ignore_errors=True)


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
            if fallback_mode:
                attempt_wait_seconds = max(10, min(12, int(wait_seconds)))
                attempt_settle_seconds = min(float(settle_seconds), 1.5)
                attempt_price_poll_seconds = min(float(price_poll_seconds), 6.0)
            else:
                attempt_wait_seconds = int(wait_seconds)
                attempt_settle_seconds = float(settle_seconds)
                attempt_price_poll_seconds = float(price_poll_seconds)

            result = scrape_1hotels_once(
                url=url,
                wait_seconds=attempt_wait_seconds,
                settle_seconds=attempt_settle_seconds,
                price_poll_seconds=attempt_price_poll_seconds,
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


@st.cache_resource(show_spinner=False)
def get_rate_cache_store() -> Dict[str, object]:
    """Return the process-local successful-rate cache shared across Streamlit reruns."""
    return {"entries": {}, "lock": threading.RLock()}


def make_rate_cache_key(url: str) -> str:
    """Versioned key so code deployments never reuse results from an older parser."""
    return hashlib.sha256(f"{APP_VERSION}\0{url}".encode("utf-8")).hexdigest()


def read_rate_cache(url: str, ttl_seconds: int) -> Optional[Dict]:
    ttl_seconds = max(1, int(ttl_seconds))
    store = get_rate_cache_store()
    entries = store["entries"]
    lock = store["lock"]
    key = make_rate_cache_key(url)

    with lock:
        entry = entries.get(key)
        if not isinstance(entry, dict):
            return None
        saved_at = float(entry.get("saved_at", 0.0) or 0.0)
        age_seconds = max(0.0, time.time() - saved_at)
        if age_seconds > ttl_seconds:
            entries.pop(key, None)
            return None
        result = copy.deepcopy(entry.get("result"))

    if not isinstance(result, dict):
        return None
    result["cache_status"] = "hit"
    result["cache_age_seconds"] = round(age_seconds, 1)
    result["cache_ttl_seconds"] = ttl_seconds
    return result


def write_rate_cache(url: str, result: Dict) -> None:
    """Cache only successful results containing at least one room; never cache failures."""
    if not isinstance(result, dict) or not result.get("rooms"):
        return

    store = get_rate_cache_store()
    entries = store["entries"]
    lock = store["lock"]
    key = make_rate_cache_key(url)
    now = time.time()

    clean_result = copy.deepcopy(result)
    clean_result.pop("cache_status", None)
    clean_result.pop("cache_age_seconds", None)
    clean_result.pop("cache_ttl_seconds", None)

    with lock:
        entries[key] = {"saved_at": now, "result": clean_result}
        if len(entries) > MAX_RATE_CACHE_ENTRIES:
            oldest_keys = sorted(
                entries,
                key=lambda item_key: float(entries[item_key].get("saved_at", 0.0) or 0.0),
            )[: max(1, len(entries) - MAX_RATE_CACHE_ENTRIES)]
            for old_key in oldest_keys:
                entries.pop(old_key, None)


def clear_rate_cache_for_url(url: str) -> bool:
    store = get_rate_cache_store()
    entries = store["entries"]
    lock = store["lock"]
    key = make_rate_cache_key(url)
    with lock:
        return entries.pop(key, None) is not None


def scrape_1hotels_with_cache(
    url: str,
    wait_seconds: int = 10,
    settle_seconds: float = 2.0,
    price_poll_seconds: float = 8.0,
    retry_once: bool = True,
    cache_ttl_seconds: int = DEFAULT_RATE_CACHE_MINUTES * 60,
    force_live_refresh: bool = False,
) -> Dict:
    """
    Return a recent successful result immediately, otherwise perform a live scrape.

    Cache semantics are intentionally conservative:
    - only positive room results are cached;
    - failures and empty parses are never cached;
    - Force live refresh bypasses and replaces the current URL's cache entry.
    """
    cache_ttl_seconds = max(1, int(cache_ttl_seconds))

    if not force_live_refresh:
        cached = read_rate_cache(url, cache_ttl_seconds)
        if cached is not None:
            return cached

    result = scrape_1hotels(
        url=url,
        wait_seconds=wait_seconds,
        settle_seconds=settle_seconds,
        price_poll_seconds=price_poll_seconds,
        retry_once=retry_once,
    )
    result["cache_status"] = "live"
    result["cache_age_seconds"] = 0.0
    result["cache_ttl_seconds"] = cache_ttl_seconds
    if result.get("rooms"):
        write_rate_cache(url, result)
    return result


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
        <div style="display:flex; align-items:center; gap:8px; height:42px;">
            <textarea id="email-copy-source" style="position:absolute; left:-9999px; top:-9999px;">{escaped_text}</textarea>
            <button
                id="copy-email-button"
                style="
                    width:100%;
                    height:38px;
                    border:1px solid rgba(50,91,72,.26);
                    border-radius:11px;
                    background:linear-gradient(180deg,#F7FAF8,#E9F0EC);
                    color:#18352B;
                    font-weight:760;
                    box-shadow:0 5px 14px rgba(37,76,58,.065);
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
st.markdown(
    """
    <div class="rs-hero">
      <div class="rs-eyebrow">Commercial Intelligence · Live Pricing</div>
      <h1>Rate Shop</h1>
      <p>Search live room inventory, shape a competitive offer, and turn selected room types into a client-ready quote from one focused workspace.</p>
    </div>
    """,
    unsafe_allow_html=True,
)

with st.sidebar:
    st.markdown(
        f"""
        <div class="rs-sidebrand">
          <div class="kicker">Commercial Tools</div>
          <div class="name">Rate Shop</div>
          <div class="meta">Live pricing workspace · {html.escape(APP_VERSION)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.markdown("#### Search Settings")

    if st.button("LOGOUT"):
        st.session_state.authenticated = False
        st.session_state.pop("authenticated_user_name", None)
        st.session_state.pop("authenticated_via_browser_token", None)
        render_logout_and_reload()
        st.stop()

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
    discount_percent = st.number_input(
        "Offer discount %",
        min_value=0.0,
        max_value=100.0,
        value=float(DEFAULT_DISCOUNT_PERCENT),
        step=1.0,
    )
    st.checkbox("Rates include tax in client email", key="rates_include_tax")

    with st.expander("Advanced search options", expanded=False):
        st.caption(f"Hotel code {hotel_code} · {hotel_provider} · display {selected_currency_symbol}")
        currency = st.selectbox("Booking currency", options=["USD"], index=0)
        sort = st.selectbox("Rate sort", options=["low", "high"], index=0)
        group_code = st.text_input("Group code", value="")
        promo_code = st.text_input("Promo code", value="")
        wait_seconds = st.slider("Search timeout seconds", 8, 20, 10, 1)
        rate_cache_minutes = st.slider(
            "Successful-rate cache minutes",
            min_value=1,
            max_value=30,
            value=DEFAULT_RATE_CACHE_MINUTES,
            step=1,
        )
        force_live_refresh = st.checkbox(
            "Force live refresh (ignore cache)",
            value=False,
            help="Use this when you must bypass a recent successful result and hit the booking site again.",
        )
        st.caption("Only successful room-rate results are cached. Empty/failed searches are never cached.")

    search_clicked = st.button("SEARCH LIVE RATES", type="primary", use_container_width=True)

    with st.expander("Browser runtime", expanded=False):
        runtime = get_chrome_runtime()
        st.write("Chromium:", runtime.get("chromium_binary") or "not found")
        st.write("Chromedriver:", runtime.get("chromedriver_binary") or "not found")
        st.caption(str(runtime.get("chromium_version") or ""))
        st.caption(str(runtime.get("chromedriver_version") or ""))

if checkout <= checkin:
    st.error("Check-out date must be later than Check-in date.")
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
        <div class="label">Hotel</div>
        <div class="value">{html.escape(hotel_key)}</div>
        <div class="foot">{html.escape(hotel_provider.title())} · code {html.escape(hotel_code)}</div>
      </div>
      <div class="rs-summary-card">
        <div class="label">Stay</div>
        <div class="value">{stay_nights} night{'s' if stay_nights != 1 else ''}</div>
        <div class="foot">{checkin.isoformat()} → {checkout.isoformat()}</div>
      </div>
      <div class="rs-summary-card">
        <div class="label">Guests</div>
        <div class="value">{party_size}</div>
        <div class="foot">{int(adults)} adult · {int(children)} child</div>
      </div>
      <div class="rs-summary-card">
        <div class="label">Offer</div>
        <div class="value">{float(discount_percent):g}% off</div>
        <div class="foot">Display currency {html.escape(selected_currency_symbol)}</div>
      </div>
    </div>
    """,
    unsafe_allow_html=True,
)

with st.expander("Booking URL & technical details", expanded=False):
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

if search_clicked:
    room_nights_for_search = max((checkout - checkin).days, 1)
    is_long_date_search = room_nights_for_search > 3

    if is_long_date_search:
        # Long stays need a little more backend time, but should not multiply every
        # Selenium wait into a 1-2 minute frozen-looking request.
        adaptive_wait_seconds = int(min(18, max(int(wait_seconds) + 4, 14)))
        price_settle_seconds = 2.0
        price_poll_seconds = 12.0
    else:
        adaptive_wait_seconds = int(min(16, max(int(wait_seconds), 10)))
        price_settle_seconds = 1.5
        price_poll_seconds = 8.0

    cache_ttl_seconds = int(rate_cache_minutes) * 60
    spinner_prefix = "Forcing live refresh" if force_live_refresh else f"Checking {int(rate_cache_minutes)}-minute cache, then live site if needed"
    with st.spinner(
        f"{spinner_prefix}. Live budget: page up to {adaptive_wait_seconds}s, "
        f"settle {price_settle_seconds:g}s, price poll up to {price_poll_seconds:g}s."
    ):
        try:
            result = scrape_1hotels_with_cache(
                target_url,
                wait_seconds=adaptive_wait_seconds,
                settle_seconds=price_settle_seconds,
                price_poll_seconds=price_poll_seconds,
                retry_once=True,
                cache_ttl_seconds=cache_ttl_seconds,
                force_live_refresh=bool(force_live_refresh),
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
                cache_status = str(result.get("cache_status") or "live")
                if cache_status == "hit":
                    cache_age = float(result.get("cache_age_seconds", 0.0) or 0.0)
                    st.success(
                        f"Loaded {len(rooms)} room type(s) from rate cache ({cache_age:.0f}s old). "
                        "No browser was launched for this search."
                    )
                else:
                    st.success(
                        f"Live search completed: parsed {len(rooms)} room type(s). "
                        f"Successful result cached for {int(rate_cache_minutes)} minute(s)."
                    )
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

st.subheader("Room Selection & Offer Builder")
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
st.subheader("Structured Rate Result")
if not st.session_state.last_df.empty:
    st.dataframe(style_output_dataframe(st.session_state.last_df), use_container_width=True, hide_index=True)
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
