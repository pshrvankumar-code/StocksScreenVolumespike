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
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

import pandas as pd
import numpy as np
import requests
import yfinance as yf

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
    Note: NSE public API endpoints may change or rate-limit; consider
    using an exchange data provider for production usage.
    """
    symbol = ticker.upper()
    base = 'https://www.nseindia.com'
    api_url = f"{base}/api/quote-equity?symbol={symbol}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (compatible; ScreenerBot/1.0; +https://github.com)',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept': 'application/json, text/plain, */*',
    }
    try:
        session = requests.Session()
        # Initial GET to set cookies
        session.get(base, headers=headers, timeout=10)
        resp = session.get(api_url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        # Try a few common paths that may contain delivery info
        keys = [
            ('deliveryToTradedVolume',),
            ('data', 'deliveryToTradedVolume'),
            ('records', 'deliveryToTradedVolume')
        ]
        # Flatten search across nested dicts
        def deep_get(d, path):
            obj = d
            for k in path:
                if isinstance(obj, dict) and k in obj:
                    obj = obj[k]
                else:
                    return None
            return obj

        for path in keys:
            val = deep_get(data, path)
            if val is not None:
                try:
                    # API sometimes returns string like '12.34' or a number
                    return float(val)
                except Exception:
                    continue

        # Other possible locations: inspect 'priceInfo' or nested payloads
        # Search entire JSON for keys containing 'delivery' as fallback
        def find_delivery(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    if 'delivery' in k.lower():
                        try:
                            return float(v)
                        except Exception:
                            pass
                    res = find_delivery(v)
                    if res is not None:
                        return res
            elif isinstance(node, list):
                for item in node:
                    res = find_delivery(item)
                    if res is not None:
                        return res
            return None

        found = find_delivery(data)
        if found is not None:
            return float(found)
        return None
    except Exception:
        return None


def build_report(results: List[Dict[str, Any]]) -> str:
    """Build an HTML table report from the results list."""
    rows = []
    for r in results:
        rows.append(
            f"<tr><td>{r['ticker']}</td><td>{r['price']:.2f}</td>"
            f"<td>{r['sma20']:.2f}</td><td>{r['sma50']:.2f}</td>"
            f"<td>{r['delivery_pct']:.1f}</td><td>{int(r['volume']):,}</td></tr>"
        )
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

    SMTP_SERVER = os.environ.get('SMTP_SERVER', 'smtp.gmail.com')
    SMTP_PORT = int(os.environ.get('SMTP_PORT', '587'))
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
    for i, t in enumerate(tickers):
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

        # Support zone check
        in_zone = (
            is_in_support_zone(price, sma20, SMA_PROXIMITY_PCT)
            or is_in_support_zone(price, sma50, SMA_PROXIMITY_PCT)
        )
        if not in_zone:
            continue

        # Fetch delivery percentage from NSE (may be None)
        delivery_pct = fetch_delivery_percentage_nse(t)
        if delivery_pct is None:
            continue
        if delivery_pct < DELIVERY_PCT_THRESHOLD:
            continue

        candidates.append({
            'ticker': t,
            'price': price,
            'sma20': sma20,
            'sma50': sma50,
            'delivery_pct': delivery_pct,
            'volume': volume,
        })

    # Sort by highest delivery % and limit results
    candidates.sort(key=lambda x: x['delivery_pct'], reverse=True)
    top = candidates[:MAX_STOCKS_TO_REPORT]

    # Build HTML report and send
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
