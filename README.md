# Delivery Volume Breakout Screener

A production-ready Python automation tool and GitHub Actions workflow for scanning the **Nifty 500 index** to identify institutional accumulation setups based on delivery volume spikes near moving average support.

## Overview

This screener identifies high-delivery Nifty 500 stocks using moving averages and delivery volume data. It automates daily scans, emails results, and maintains a rolling 10-day history log.

### Current Screening Logic

- Scan the Nifty 500 ticker universe.
- Fetch recent OHLCV price data and compute 20-day and 50-day SMAs.
- Load delivery percentage from bhavcopy when available, with NSE reports API metadata as the preferred source.
- Fall back to NSE JSON delivery data when bhavcopy is not available.
- Skip any tickers with missing delivery data when delivery filtering is enabled.
- Require delivery percentage above the configured threshold (`DELIVERY_PCT_THRESHOLD`).
- Sort candidates by delivery percentage descending and keep the top configured results (`MAX_STOCKS_TO_REPORT`).

### Key Features

✅ **Nifty 500 Universe Scan** – Liquidity-filtered index universe  
✅ **Moving Average Support Detection** – 20/50 SMA proximity (±2%)  
✅ **Delivery Volume Filter** – ≥50% delivery percentage  
✅ **10-Day History Tracking** – JSON log auto-pruned each run  
✅ **Email Notifications** – HTML table reports to your inbox  
✅ **Git Commit Automation** – History file pushed to repo  
✅ **Telegram Extensibility** – Placeholder for future bot integration  
✅ **GitHub Actions Scheduled** – Mon–Fri at 16:30 IST (11:00 UTC)  

## Technical Architecture

### Core Components

| File | Purpose |
|------|---------|
| `screener.py` | Main scan logic, data fetching, filtering, history management |
| `requirements.txt` | Python dependencies (pandas, yfinance, requests, etc.) |
| `.github/workflows/stock_screener.yml` | GitHub Actions scheduler and CI/CD pipeline |
| `history_log.json` | Rolling 10-day results log (auto-generated) |

### Technical Stack

- **Data Source**: yfinance (Yahoo Finance) + NSE public API
- **Libraries**: pandas, numpy, requests
- **Scheduler**: GitHub Actions (cron-based)
- **Notification**: SMTP (Gmail) + Telegram (optional)
- **Version Control**: Git + GitHub Actions bot

## Setup Instructions

### 1. Clone Repository

```bash
git clone https://github.com/YOUR_USERNAME/StockwithVolumeSpike.git
cd StockwithVolumeSpike
```

### 2. Set Up Python Environment (Local Testing)

```bash
# Create virtual environment
python -m venv .venv

# Activate (Windows)
.venv\Scripts\activate

# Activate (macOS/Linux)
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 3. Configure GitHub Repository Secrets

Navigate to **Settings → Secrets and variables → Actions** and add:

**Required Secrets:**
- `SENDER_EMAIL` – Your Gmail address
- `SENDER_PASSWORD` – Gmail app password (not your regular password)
- `RECEIVER_EMAIL` – Where to send reports

**Optional Secrets:**
- `SMTP_SERVER` – `smtp.gmail.com` (default)
- `SMTP_PORT` – `587` (default)

#### Getting Gmail App Password

1. Enable 2-Factor Authentication on your Google Account
2. Go to **Google Account → Security → App passwords**
3. Select "Mail" and "Windows Computer"
4. Copy the 16-character app password
5. Paste as `SENDER_PASSWORD` secret

### 4. Test Locally (Optional)

```bash
# Set environment variables (Windows PowerShell)
$env:SENDER_EMAIL = "your-email@gmail.com"
$env:SENDER_PASSWORD = "your-app-password"
$env:RECEIVER_EMAIL = "recipient@example.com"

# Run screener
python screener.py

# Or (macOS/Linux)
export SENDER_EMAIL="your-email@gmail.com"
export SENDER_PASSWORD="your-app-password"
export RECEIVER_EMAIL="recipient@example.com"
python screener.py
```

### 5. Deploy to GitHub

```bash
git add .
git commit -m "Initial commit: delivery volume breakout screener"
git push origin main
```

The workflow will automatically run **Monday–Friday at 16:30 IST** (11:00 UTC).

## Configuration

### Adjusting Results Limit

Edit `screener.py` line 32:

```python
MAX_STOCKS_TO_REPORT = 10  # Change to desired number of results
```

### Changing SMA Proximity Threshold

Edit `screener.py` line 35:

```python
SMA_PROXIMITY_PCT = 0.02  # ±2% by default; adjust as needed
```

### Adjusting Delivery Threshold

Edit `screener.py` line 38:

```python
DELIVERY_PCT_THRESHOLD = 50.0  # Minimum delivery % to report
```

### Changing Scan Frequency

Edit `.github/workflows/stock_screener.yml` line 7:

```yaml
- cron: '0 11 * * 1-5'  # Cron expression: 11:00 UTC Mon-Fri
```

**Cron Cheatsheet:**
- `0 15 * * 1-5` → 15:00 UTC (20:30 IST) weekdays
- `30 9 * * *` → 09:30 UTC daily (15:00 IST)
- `0 11 * * 1,3,5` → 11:00 UTC Mon/Wed/Fri

## Output & History

### Email Report Format

| Column | Meaning |
|--------|---------|
| **Ticker** | NSE stock symbol |
| **Price** | Current closing price |
| **SMA20** | 20-day simple moving average |
| **SMA50** | 50-day simple moving average |
| **Delivery %** | Daily delivery % (institutional accumulation proxy) |
| **Volume** | Total traded volume |

### History File Structure

`history_log.json` maintains the last 10 execution days:

```json
[
  {
    "date": "2026-06-04T16:30:00+00:00",
    "results": [
      {
        "ticker": "RELIANCE",
        "price": 3050.50,
        "sma20": 3040.25,
        "sma50": 3025.10,
        "delivery_pct": 65.3,
        "volume": 45678900
      }
    ]
  }
]
```

## Extending: Telegram Integration

To add Telegram notifications, follow these steps:

### 1. Create Telegram Bot

1. Message `@BotFather` on Telegram
2. `/newbot` → name your bot → copy **API token**
3. Message your bot → `/start` → get **Chat ID**

### 2. Add Secrets

In GitHub Settings, add:
- `TELEGRAM_BOT_TOKEN` – Your bot token
- `TELEGRAM_CHAT_ID` – Your chat ID

### 3. Uncomment Telegram Code

In `screener.py`, uncomment lines at the end of `send_notification()`:

```python
# --- Telegram (now active) ---
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {'chat_id': TELEGRAM_CHAT_ID, 'text': report_html, 'parse_mode': 'HTML'}
    requests.post(url, data=payload, timeout=10)
```

## Troubleshooting

### Workflow Not Triggering

- Check **Actions** tab in GitHub for logs
- Ensure `.github/workflows/stock_screener.yml` is in `main` branch
- Verify all secrets are set (workflow will fail silently without them)

### Email Not Sending

- Confirm Gmail app password (not regular password) is set
- Verify 2-Factor Authentication is enabled on Gmail
- Check GitHub Actions logs: **Actions → Latest run → Run screener**

### NSE API Timeouts

The script gracefully handles NSE API rate-limiting. Affected stocks are skipped; the scan continues. Consider using a commercial NSE data provider for production deployments.

### History File Not Updating

Ensure `GITHUB_TOKEN` is available (automatic in GitHub Actions). Local runs require explicit Git setup:

```bash
git config user.name "Bot Name"
git config user.email "bot@example.com"
```

## Performance & Limits

- **Execution Time**: ~3–5 minutes for full Nifty 500 scan
- **GitHub Actions Free Tier**: 2,000 minutes/month (plenty for daily runs)
- **Rate Limits**: NSE may throttle after ~50 rapid requests; built-in retry logic mitigates

## API Sources

| Data | Provider | Notes |
|------|----------|-------|
| OHLCV | yfinance (Yahoo) | Reliable; historical data only |
| Delivery % | NSE public API | Real-time; may timeout occasionally |
| Index Constituents | NSE archives | Updated periodically |

## Contributing

Issues and PRs welcome! Areas for enhancement:

- [ ] Real-time delivery data via paid NSE API
- [ ] Telegram bot with interactive filters
- [ ] Web dashboard for historical scans
- [ ] Machine learning for pattern recognition
- [ ] Alerts for intraday volume spikes

## License

This project is licensed under the **MIT License** — see [LICENSE](LICENSE) file.

## Disclaimer

**For Educational & Research Purposes Only**. This tool does not constitute financial advice. Stock market investments carry risk. Always conduct your own due diligence and consult a financial advisor before trading.

## Support

- 📧 Email: your-email@example.com
- 💬 Issues: GitHub Issues tab
- 📝 Docs: See `screener.py` for inline documentation

---

**Happy Screening! 📈**

*Last Updated: June 4, 2026*
