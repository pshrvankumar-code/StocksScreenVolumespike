#!/usr/bin/env python3
"""
Delivery Volume Breakout Screener for Nifty 500

This script scans the Nifty 500 universe for delivery-volume breakouts
near moving average support (20/50 SMA). It saves a 10-day history log,
sends an email report, and commits the history file back to the repo.

Notes:
- Requires GitHub Action secrets for email and (optionally) push token.
- Delivery % is fetched from NSE's public quote API; sites may change.

Author: GitHub Copilot (example)
"""

import os
import sys
import json
import time
import math
import subprocess
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional

import pandas as pd
import numpy as np
import requests
import yfinance as yf
import zipfile
import io
import tempfile

# ----------------------- Configuration -----------------------
# Max results to include in the email (changeable)
MAX_STOCKS_TO_REPORT = 10

# Tight proximity threshold to consider 'Support/Buying Zone' (±2%)
SMA_PROXIMITY_PCT = 0.02

# Delivery percentage threshold (>= 50%)
DELIVERY_PCT_THRESHOLD = 50.0

# Number of days of history to keep
HISTORY_DAYS = 10

# History file path (stored in repo)
HISTORY_FILE = "history_log.json"

# Default NSE CSV for Nifty 500 constituents (falls back to cached list)
NSE_NIFTY500_CSV = (
    "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"
)

# Allow disabling delivery % check via environment for cases when NSE API is rate-limiting or blocked
# Set environment variable DELIVERY_CHECK_ENABLED=0 to skip delivery checks and only use SMA support filter
DELIVERY_CHECK_ENABLED = str(os.environ.get('DELIVERY_CHECK_ENABLED', '1')).lower() in ('1', 'true', 'yes')
# Use bhavcopy as preferred delivery source when available
USE_BHAVCOPY = str(os.environ.get('USE_BHAVCOPY', '1')).lower() in ('1', 'true', 'yes')
BHAVCOPY_DATE = os.environ.get('BHAVCOPY_DATE')  # optional YYYY-MM-DD to pick specific bhavcopy
NSE_REPORTS_API_URL = 'https://www.nseindia.com/api/daily-reports?key={key}'
NSE_REPORTS_REFERER = 'https://www.nseindia.com/all-reports'

# ----------------------- Helper Functions -----------------------

def get_nifty500_tickers() -> List[str]:
    """Fetch the Nifty 500 tickers from NSE index CSV.

    Returns a list of plain tickers (without .NS suffix).
    Falls back to a hard-coded minimal list if fetch fails.
    """
    try:
        resp = requests.get(NSE_NIFTY500_CSV, timeout=20)
        resp.raise_for_status()
        df = pd.read_csv(pd.compat.StringIO(resp.text))
        # CSV usually has a column 'Symbol' or 'Company Name' depending on source
        if 'Symbol' in df.columns:
            symbols = df['Symbol'].astype(str).str.strip().tolist()
        else:
            # try first column
            symbols = df.iloc[:, 0].astype(str).str.strip().tolist()
        # Some symbols include .NS already; strip it
        symbols = [s.replace('.NS', '').strip() for s in symbols if s]
        return symbols
    except Exception:
        # Fallback: small set of liquid examples to avoid total failure
        return [
            'RELIANCE', 'TCS', 'HDFCBANK', 'INFY', 'ICICIBANK', 'HINDUNILVR',
            'KOTAKBANK', 'SBIN', 'BHARTIARTL', 'LT'
        ]


def fetch_price_data(ticker: str, period: str = '120d') -> Optional[pd.DataFrame]:
    """Fetch daily OHLCV for a ticker using yfinance.

    The function appends '.NS' to query NSE symbols.
    Returns None on failure.
    """
    try:
        t = yf.Ticker(f"{ticker}.NS")
        df = t.history(period=period, interval='1d', auto_adjust=False)
        if df is None or df.empty:
            return None
        df = df[['Open', 'High', 'Low', 'Close', 'Volume']].copy()
        return df
    except Exception:
        return None


def compute_smas(df: pd.DataFrame) -> pd.DataFrame:
    """Compute 20-day and 50-day SMAs and return the augmented DataFrame."""
    df = df.copy()
    df['SMA20'] = df['Close'].rolling(window=20, min_periods=10).mean()
    df['SMA50'] = df['Close'].rolling(window=50, min_periods=30).mean()
    return df


def is_in_support_zone(current_price: float, sma: float, pct_threshold: float) -> bool:
    """Return True if current_price is within ±pct_threshold of sma."""
    if sma is None or math.isnan(sma) or sma <= 0:
        return False
    low = sma * (1 - pct_threshold)
    high = sma * (1 + pct_threshold)
    return low <= current_price <= high


def fetch_delivery_percentage_nse(ticker: str) -> Optional[float]:
    """Attempt to fetch delivery % from NSE's public quote API.

    This function uses a requests.Session to obtain cookies/headers and
    then calls the JSON API that powers the NSE equity quote pages.

    Returns delivery percentage as float (0-100), or None if not available.
    This version includes basic retries and sanitization of returned values.
    """
    symbol = ticker.upper()
    base = 'https://www.nseindia.com'
    api_url = f"{base}/api/quote-equity?symbol={symbol}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (compatible; ScreenerBot/1.0; +https://github.com)',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept': 'application/json, text/plain, */*',
    }

    def sanitize_value(val) -> Optional[float]:
        try:
            if val is None:
                return None
            s = str(val).strip()
            # remove percent signs and commas
            s = s.replace('%', '').replace(',', '')
            # if empty after stripping
            if s == '':
                return None
            f = float(s)
            # crude validation
            if f < 0 or f > 1000:
                return None
            # If the API sometimes gives ratio like 0.6 instead of percent, handle that
            if f <= 1:
                f = f * 100
            return f
        except Exception:
            return None

    # Try a couple of times to avoid transient NSE rate limits
    for attempt in range(2):
        try:
            session = requests.Session()
            session.get(base, headers=headers, timeout=10)
            resp = session.get(api_url, headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            # Try explicit locations first
            candidates = []
            # direct possible keys
            for key in ['deliveryToTradedVolume', 'deliverytoTradedVolume', 'delivery']:
                if key in data:
                    candidates.append(data[key])
            # common nested places
            if isinstance(data, dict):
                if 'data' in data and isinstance(data['data'], dict) and 'deliveryToTradedVolume' in data['data']:
                    candidates.append(data['data']['deliveryToTradedVolume'])
                if 'records' in data and isinstance(data['records'], dict) and 'deliveryToTradedVolume' in data['records']:
                    candidates.append(data['records']['deliveryToTradedVolume'])

            # recursive search for keys containing 'delivery'
            def find_delivery(node):
                if isinstance(node, dict):
                    for k, v in node.items():
                        if 'delivery' in k.lower():
                            candidates.append(v)
                        else:
                            find_delivery(v)
                elif isinstance(node, list):
                    for item in node:
                        find_delivery(item)

            find_delivery(data)

            # sanitize candidates
            for c in candidates:
                val = sanitize_value(c)
                if val is not None:
                    if 0 <= val <= 100:
                        return val
            # nothing found this attempt
        except Exception as e:
            error_reason = str(e)[:150]
            print(f"NSE JSON fetch failed for {ticker} (attempt {attempt+1}): {error_reason}")
            time.sleep(1)
            continue
    return None


def download_and_parse_bhavcopy_for_date(date_obj) -> Dict[str, float]:
    """Download bhavcopy ZIP for given `date_obj` and return symbol->delivery_pct map.

    Expects NSE archives path like:
      https://archives.nseindia.com/content/historical/EQUITIES/{YYYY}/{MON}/{DDMONYYYYbhav.csv.zip}
    Returns empty dict on failure.
    """
    result = {}
    try:
        year = date_obj.strftime('%Y')
        mon = date_obj.strftime('%b').upper()
        day_str = date_obj.strftime('%d%b%Y').upper()  # e.g., 04JUN2026
        url = f"https://archives.nseindia.com/content/historical/EQUITIES/{year}/{mon}/{day_str}bhav.csv.zip"
        headers = {'User-Agent': 'Mozilla/5.0 (compatible; ScreenerBot/1.0)'}
        resp = requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()
        with tempfile.TemporaryDirectory() as td:
            z = zipfile.ZipFile(io.BytesIO(resp.content))
            # find first CSV inside
            csv_name = None
            for name in z.namelist():
                if name.lower().endswith('.csv'):
                    csv_name = name
                    break
            if csv_name is None:
                return {}
            with z.open(csv_name) as fh:
                # read into pandas
                df = pd.read_csv(fh)
                # Normalize column names
                cols = {c.strip(): c for c in df.columns}
                # expected columns: SYMBOL, TOTTRDQTY or TTLTRDQTY, DELIV_QTY or DELIVQTY
                sym_col = None
                tot_col = None
                deliv_col = None
                for c in df.columns:
                    cl = c.strip().upper()
                    if cl == 'SYMBOL':
                        sym_col = c
                    if cl in ('TOTTRDQTY', 'TTLTRDQTY', 'TOTTRDQTY '):
                        tot_col = c
                    if cl in ('DELIV_QTY', 'DELIVQTY', 'DELIV_QTY '):
                        deliv_col = c
                if sym_col is None or tot_col is None or deliv_col is None:
                    # try fuzzy matches
                    for c in df.columns:
                        uc = c.strip().upper()
                        if 'SYMBOL' in uc and sym_col is None:
                            sym_col = c
                        if 'TOT' in uc and 'QTY' in uc and tot_col is None:
                            tot_col = c
                        if 'DELIV' in uc and 'QTY' in uc and deliv_col is None:
                            deliv_col = c
                if sym_col is None or tot_col is None or deliv_col is None:
                    return {}
                for _, row in df.iterrows():
                    try:
                        sym = str(row[sym_col]).strip().upper()
                        tot = int(float(row[tot_col])) if not pd.isna(row[tot_col]) else 0
                        deliv = int(float(row[deliv_col])) if not pd.isna(row[deliv_col]) else 0
                        if tot > 0:
                            pct = (deliv / tot) * 100.0
                            result[sym] = pct
                    except Exception:
                        continue
        return result
    except Exception as e:
        print(f"Warning: failed to download/parse bhavcopy for {date_obj.date()}: {str(e)[:120]}")
        return {}


def determine_bhavcopy_date_from_yfinance(tickers: List[str]) -> Optional[str]:
    """Infer the bhavcopy date from yfinance last-trade date.

    Fetches price data for the first available ticker and returns the
    most recent trading date in YYYY-MM-DD format.
    Returns None if no data available.
    """
    for t in tickers[:5]:  # try first few tickers
        df = fetch_price_data(t, period='30d')
        if df is None or df.empty:
            continue
        try:
            # index is DatetimeIndex; get last (most recent) date
            last_date = df.index[-1].date().isoformat()
            return last_date
        except Exception:
            continue
    return None


def prepare_nse_reports_session() -> requests.Session:
    """Prepare a requests session for NSE reports endpoints with cookies and headers."""
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': NSE_REPORTS_REFERER,
    })
    try:
        session.get('https://www.nseindia.com', timeout=20)
        session.get(NSE_REPORTS_REFERER, timeout=20)
    except Exception:
        pass
    return session


def fetch_daily_reports_metadata(key: str, session: Optional[requests.Session] = None) -> Dict[str, Any]:
    """Fetch daily report metadata for a given segment key from NSE reports API."""
    session = session or prepare_nse_reports_session()
    try:
        resp = session.get(NSE_REPORTS_API_URL.format(key=key), timeout=20)
        resp.raise_for_status()
        return resp.json() if resp.text else {}
    except Exception as e:
        print(f"Warning: failed to fetch daily reports metadata for {key}: {e}")
        return {}


def choose_bhavcopy_report_item(metadata: Dict[str, Any], target_date: datetime) -> Optional[Dict[str, Any]]:
    """Choose the best bhavcopy report item from daily reports metadata."""
    if not metadata:
        return None
    date_key = target_date.strftime('%d-%b-%Y')
    candidates = []
    for item in metadata.get('PreviousDay', []):
        if item.get('tradingDate') != date_key:
            continue
        name = str(item.get('displayName', '')).lower()
        file_name = str(item.get('fileActlName', '')).lower()
        if 'bhavcopy' in name or 'bhav' in file_name or 'security deliverable' in name:
            score = 0
            if 'full bhavcopy' in name or 'security deliverable' in name:
                score += 20
            if 'bhavcopy (pr)' in name:
                score += 10
            if file_name.endswith('.csv'):
                score += 5
            if file_name.endswith('.zip'):
                score += 3
            if 'full bhavcopy' in name:
                score += 2
            candidates.append((score, item))
    if not candidates:
        return None
    return max(candidates, key=lambda x: x[0])[1]


def download_and_parse_bhavcopy_for_report_item(item: Dict[str, Any], session: Optional[requests.Session] = None) -> Dict[str, float]:
    """Download bhavcopy/meta file from NSE reports metadata and parse it into a delivery map."""
    session = session or prepare_nse_reports_session()
    file_path = item.get('filePath')
    file_name = item.get('fileActlName')
    if not file_path or not file_name:
        return {}
    download_url = f"{file_path}{file_name}"
    try:
        resp = session.get(download_url, timeout=30)
        resp.raise_for_status()
        content = resp.content
        # Direct CSV file
        if file_name.lower().endswith('.csv'):
            return parse_bhavcopy_csv_bytes(content)
        # ZIP wrapper around CSV
        if file_name.lower().endswith('.zip'):
            z = zipfile.ZipFile(io.BytesIO(content))
            for name in z.namelist():
                if name.lower().endswith('.csv'):
                    with z.open(name) as fh:
                        return parse_bhavcopy_csv_bytes(fh.read())
            return {}
        # unsupported file type
        return {}
    except Exception as e:
        print(f"Warning: failed to download/parse bhavcopy report item {download_url}: {e}")
        return {}


def parse_bhavcopy_csv_bytes(content: bytes) -> Dict[str, float]:
    """Parse CSV bytes and return a symbol->delivery% map."""
    try:
        df = pd.read_csv(io.BytesIO(content))
    except Exception:
        return {}
    # Normalize columns
    cols = {c.strip().upper(): c for c in df.columns}
    sym_col = None
    tot_col = None
    deliv_col = None
    per_col = None
    for c in df.columns:
        uc = c.strip().upper()
        if uc == 'SYMBOL':
            sym_col = c
        if uc in ('TTL_TRD_QNTY', 'TTL_TRD_QTY', 'TTLTRDQTY', 'TOTTRDQTY', 'TOTALTRDQTY', 'TOTAL_TRDQTY'):
            tot_col = c
        if uc in ('DELIV_QTY', 'DELIVQTY', 'DELIVERABLE_QTY', 'DELIVERABLEQTY'):
            deliv_col = c
        if uc in ('DELIV_PER', 'DELIVPER', 'DELIVERY_PERCENT', 'DELIVERY_PCT', 'DELIVERY_PERCENTAGE', 'DELIV_PCT'):
            per_col = c
    if sym_col is None or ((tot_col is None or deliv_col is None) and per_col is None):
        for c in df.columns:
            uc = c.strip().upper()
            if 'SYMBOL' in uc and sym_col is None:
                sym_col = c
            if ('TOT' in uc or 'TTL' in uc) and 'QTY' in uc and tot_col is None:
                tot_col = c
            if 'DELIV' in uc and 'QTY' in uc and deliv_col is None:
                deliv_col = c
            if ('DELIV' in uc or 'DELIVERY' in uc) and ('PER' in uc or 'PCT' in uc) and per_col is None:
                per_col = c
    if sym_col is None or ((tot_col is None or deliv_col is None) and per_col is None):
        return {}
    result = {}
    for _, row in df.iterrows():
        try:
            sym = str(row[sym_col]).strip().upper()
            if per_col is not None and not pd.isna(row[per_col]):
                result[sym] = float(row[per_col])
                continue
            tot = int(float(row[tot_col])) if tot_col is not None and not pd.isna(row[tot_col]) else 0
            deliv = int(float(row[deliv_col])) if deliv_col is not None and not pd.isna(row[deliv_col]) else 0
            if tot > 0:
                result[sym] = (deliv / tot) * 100.0
        except Exception:
            continue
    return result


def fetch_bhavcopy_map(preferred_date: Optional[str] = None) -> Dict[str, float]:
    """Attempt to fetch bhavcopy mapping for preferred_date (YYYY-MM-DD) or recent previous trading days.

    Preference order:
      1. If `preferred_date` provided and valid, try it.
      2. Try today.
      3. Walk back business days (skip weekends) up to `max_lookback_days`.

    Returns first successful symbol->delivery% map or empty dict.
    """
    max_lookback_days = 10
    dates_to_try = []
    # 1) preferred date if provided
    if preferred_date:
        try:
            d0 = datetime.fromisoformat(preferred_date)
            dates_to_try.append(d0)
        except Exception:
            pass

    # 2) today and previous business days
    today = datetime.now()
    d = today
    looked = 0
    while looked < max_lookback_days:
        # skip weekends
        if d.weekday() < 5:
            dates_to_try.append(d)
            looked += 1
        d = d - timedelta(days=1)

    tried = set()
    session = prepare_nse_reports_session()
    for d in dates_to_try:
        key = d.strftime('%Y-%m-%d')
        if key in tried:
            continue
        tried.add(key)
        # First try the reports API path (same flow as the NSE download page)
        metadata = fetch_daily_reports_metadata('CM', session=session)
        item = choose_bhavcopy_report_item(metadata, d)
        if item:
            parsed = download_and_parse_bhavcopy_for_report_item(item, session=session)
            if parsed:
                print(f"Loaded bhavcopy from NSE reports API for {d.date()} with {len(parsed)} entries")
                return parsed
        # Fallback to historic archive path for older-style bhavcopy files
        m = download_and_parse_bhavcopy_for_date(d)
        if m:
            print(f"Loaded bhavcopy from archive path for {d.date()} with {len(m)} entries")
            return m
    print('Bhavcopy is empty')
    return {}


def build_report(results: List[Dict[str, Any]]) -> str:
    """Build an HTML table report from the results list."""
    rows = []
    for r in results:
        delivery_display = (
            f"{r['delivery_pct']:.1f}" if isinstance(r.get('delivery_pct'), (int, float)) else 'N/A'
        )
        rows.append(
            f"<tr><td>{r['ticker']}</td><td>{r['price']:.2f}</td>"
            f"<td>{r['sma20']:.2f}</td><td>{r['sma50']:.2f}</td>"
            f"<td>{delivery_display}</td><td>{int(r['volume']):,}</td></tr>"
        )
    # If no rows, include a placeholder row so email clients show something useful
    if not rows:
        rows = ["<tr><td colspan=6 style='text-align:center'>No matching stocks found for this run.</td></tr>"]
    html = """
    <html>
    <body>
    <p>Delivery Volume Breakout Scan - {date}</p>
    <table border="1" cellpadding="4" cellspacing="0">
    <thead><tr><th>Ticker</th><th>Price</th><th>SMA20</th><th>SMA50</th><th>Delivery %</th><th>Volume</th></tr></thead>
    <tbody>{rows}</tbody>
    </table>
    </body>
    </html>
    """.replace('{rows}', '\n'.join(rows)).replace('{date}', datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))
    return html


def send_notification(report_html: str):
    """Send an email report containing the HTML table.

    Sensitive credentials are read from environment variables:
    - SENDER_EMAIL
    - SENDER_PASSWORD (app password)
    - RECEIVER_EMAIL
    - SMTP_SERVER (optional, defaults to smtp.gmail.com)
    - SMTP_PORT (optional, defaults to 587)

    To add Telegram integration later, use `requests.post` to send `report_html`
    to your bot endpoint and chat_id. Example placeholder is below.
    """
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    SMTP_SERVER = os.environ.get('SMTP_SERVER') or 'smtp.gmail.com'
    # Parse SMTP_PORT safely: allow empty string or missing secret
    smtp_port_raw = os.environ.get('SMTP_PORT')
    try:
        if smtp_port_raw is None or str(smtp_port_raw).strip() == '':
            SMTP_PORT = 587
        else:
            SMTP_PORT = int(smtp_port_raw)
    except Exception:
        print(f"Warning: invalid SMTP_PORT='{smtp_port_raw}', falling back to 587")
        SMTP_PORT = 587
    SENDER_EMAIL = os.environ.get('SENDER_EMAIL')
    SENDER_PASSWORD = os.environ.get('SENDER_PASSWORD')
    RECEIVER_EMAIL = os.environ.get('RECEIVER_EMAIL')

    if not (SENDER_EMAIL and SENDER_PASSWORD and RECEIVER_EMAIL):
        print('Email credentials not fully provided in environment; skipping email.')
        return

    msg = MIMEMultipart('alternative')
    msg['Subject'] = f"Delivery Breakout Scan - {datetime.now(timezone.utc).date()}"
    msg['From'] = SENDER_EMAIL
    msg['To'] = RECEIVER_EMAIL
    part = MIMEText(report_html, 'html')
    msg.attach(part)

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.sendmail(SENDER_EMAIL, [RECEIVER_EMAIL], msg.as_string())
        print('Email sent')
    except Exception as e:
        print('Failed to send email:', e)

    # --- Telegram placeholder (future) ---
    # TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
    # TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
    # if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
    #     url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    #     payload = {'chat_id': TELEGRAM_CHAT_ID, 'text': report_html, 'parse_mode': 'HTML'}
    #     requests.post(url, data=payload, timeout=10)


def load_history() -> List[Dict[str, Any]]:
    """Load history list from HISTORY_FILE; return empty list if missing."""
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
            if isinstance(data, list):
                return data
            return []
    except Exception:
        return []


def save_history(history: List[Dict[str, Any]]):
    """Save history list to HISTORY_FILE in pretty JSON format."""
    with open(HISTORY_FILE, 'w', encoding='utf-8') as fh:
        json.dump(history, fh, indent=2, ensure_ascii=False)


def append_and_prune_history(entry: Dict[str, Any]):
    """Append today's entry list and prune history to last HISTORY_DAYS runs."""
    history = load_history()
    history.insert(0, entry)  # newest first
    # Keep only exactly HISTORY_DAYS entries
    history = history[:HISTORY_DAYS]
    save_history(history)


def git_commit_and_push(history_file: str = HISTORY_FILE):
    """Stage, commit, and push the history file back to the origin using
    GITHUB_TOKEN (if available). This is designed to run inside GitHub Actions.
    """
    token = os.environ.get('GITHUB_TOKEN') or os.environ.get('INPUT_GITHUB_TOKEN')
    repo = os.environ.get('GITHUB_REPOSITORY')
    ref = os.environ.get('GITHUB_REF')  # e.g., refs/heads/main
    if not token or not repo or not ref:
        print('Missing GITHUB_TOKEN/REPOSITORY/REF; skipping git push.')
        return
    try:
        # Configure remote with token for auth
        remote_url = f'https://x-access-token:{token}@github.com/{repo}.git'
        subprocess.run(['git', 'remote', 'set-url', 'origin', remote_url], check=True)
        subprocess.run(['git', 'add', history_file], check=True)
        committer_name = os.environ.get('GIT_COMMITTER_NAME', 'github-actions[bot]')
        committer_email = os.environ.get('GIT_COMMITTER_EMAIL', '41898282+github-actions[bot]@users.noreply.github.com')
        subprocess.run(['git', 'config', 'user.name', committer_name], check=True)
        subprocess.run(['git', 'config', 'user.email', committer_email], check=True)
        # Commit only if there are changes
        res = subprocess.run(['git', 'status', '--porcelain'], capture_output=True, text=True)
        if res.stdout.strip() == '':
            print('No changes to commit')
            return
        subprocess.run(['git', 'commit', '-m', 'chore(history): update delivery-breakout history'], check=True)
        # Derive branch from ref
        branch = ref.split('/')[-1]
        subprocess.run(['git', 'push', 'origin', f'HEAD:{branch}'], check=True)
        print('Pushed history file to remote')
    except Exception as e:
        print('Failed to commit/push history file:', e)


def scan_universe():
    """Main scanning routine.

    Steps:
    - Get Nifty 500 tickers
    - For each ticker, fetch price data and delivery %
    - Compute SMAs and filter for support zone and delivery breakout
    - Build results, save history, send email, and push changes
    """
    tickers = get_nifty500_tickers()
    print(f'Found {len(tickers)} tickers to scan (limiting tests in debug).')

    candidates = []
    total_scanned = 0
    delivery_missing = 0
    delivery_below = 0
    passed_delivery = 0

    bhav_map = {}
    if USE_BHAVCOPY:
        # Infer bhavcopy date from yfinance if not provided
        bhav_date = BHAVCOPY_DATE
        if not bhav_date:
            bhav_date = determine_bhavcopy_date_from_yfinance(tickers)
            if bhav_date:
                print(f'Inferred bhavcopy date from yfinance: {bhav_date}')
        bhav_map = fetch_bhavcopy_map(bhav_date)

    for i, t in enumerate(tickers):
        total_scanned += 1
        # To keep GitHub Action runtime manageable, limit CPU/time per run by sampling
        if i and i % 100 == 0:
            print(f'Scanned {i} tickers...')
        df = fetch_price_data(t)
        if df is None or df.empty:
            continue
        df = compute_smas(df)
        latest = df.iloc[-1]
        price = float(latest['Close'])
        sma20 = float(latest['SMA20']) if not pd.isna(latest['SMA20']) else float('nan')
        sma50 = float(latest['SMA50']) if not pd.isna(latest['SMA50']) else float('nan')
        volume = int(latest['Volume']) if not pd.isna(latest['Volume']) else 0

        # NOTE: Support zone filter removed as requested. We now evaluate all tickers
        # for delivery breakout (or include all when DELIVERY_CHECK_ENABLED=0).

        # Delivery check (may be skipped via env)
        if not DELIVERY_CHECK_ENABLED:
            # Skipping delivery check; include candidate with delivery_pct=None
            passed_delivery += 1
            candidates.append({
                'ticker': t,
                'price': price,
                'sma20': sma20,
                'sma50': sma50,
                'delivery_pct': None,
                'delivery_source': None,
                'volume': volume,
            })
            continue

        # Try bhavcopy first when available
        delivery_pct = None
        delivery_source = None
        if bhav_map and t.upper() in bhav_map:
            delivery_pct = float(bhav_map[t.upper()])
            delivery_source = 'bhavcopy'
        else:
            delivery_pct = fetch_delivery_percentage_nse(t)
            if delivery_pct is not None:
                delivery_source = 'nse_json'
        if delivery_pct is None:
            delivery_missing += 1
            continue
        # accept only if delivery percent >= threshold
        if delivery_pct < DELIVERY_PCT_THRESHOLD:
            delivery_below += 1
            continue

        passed_delivery += 1
        candidates.append({
            'ticker': t,
            'price': price,
            'sma20': sma20,
            'sma50': sma50,
            'delivery_pct': delivery_pct,
            'delivery_source': delivery_source,
            'volume': volume,
        })

    # Sort by highest delivery % and limit results
    # Use -1 for missing delivery_pct so None values do not break sorting
    candidates.sort(key=lambda x: x['delivery_pct'] if x['delivery_pct'] is not None else -1, reverse=True)
    top = candidates[:MAX_STOCKS_TO_REPORT]

    # Print diagnostic summary to console so the workflow output is visible
    print(f"Total tickers scanned: {total_scanned}")
    print(f"Delivery data missing/skipped: {delivery_missing}")
    print(f"Delivery below threshold: {delivery_below}")
    print(f"Passed delivery filter: {passed_delivery}")
    print(f"Found {len(candidates)} matching stocks after filters.")

    if top:
        print(f"Top {len(top)} results:")
        for item in top:
            delivery_display = (
                f"{item['delivery_pct']:.1f}%" if isinstance(item.get('delivery_pct'), (int, float)) else 'N/A'
            )
            print(
                f"  {item['ticker']}: price={item['price']:.2f}, SMA20={item['sma20']:.2f}, "
                f"SMA50={item['sma50']:.2f}, delivery={delivery_display}, "
                f"volume={item['volume']:,}"
            )
    else:
        print('No matching stocks found for the current scan.')

    # Build HTML report and send
    print(f"Building report with {len(top)} rows")
    report_html = build_report(top)
    send_notification(report_html)

    # Append to history log
    entry = {'date': datetime.now(timezone.utc).isoformat(), 'results': top}
    append_and_prune_history(entry)

    # Commit and push history file back to repo (CI only)
    git_commit_and_push(HISTORY_FILE)


def main():
    scan_universe()


if __name__ == '__main__':
    main()
