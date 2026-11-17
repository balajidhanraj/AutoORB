---
title: Shoonya Automated ORB Terminal
emoji: 📈
colorFrom: blue
colorTo: indigo
sdk: gradio
app_file: app.py
short_description: Automated NSE Options 15m ORB + VWAP + RSI Terminal
python_version: "3.12"
startup_duration_timeout: 30m
---

# Shoonya Automated Options Trading System (ORB + VWAP + RSI)

Production-grade intraday automated option-buying system for **Nifty 50** and **Bank Nifty** using the **Finvasia Shoonya Trading API (`NorenRestApiOAuth`)**, complete with a **live FastAPI Web Dashboard**, **Headless Morning OAuth 2.0 + TOTP Auto-Login**, **PAPER/LIVE toggle**, **Telegram Alerts**, and **24/7 Free Cloud VPS deployment kit**.

## Features
- **Morning Auto-Login (`08:40 AM` IST)**: Headless Chrome Selenium + `pyotp` OAuth 2.0 flow with 30s TOTP boundary guard, ephemeral local callback server, daily JSON token cache, and 3-attempt exponential retry.
- **2026 NSE Specifications**:
  - **NIFTY**: Spot `NSE|26000`, Lot size `65`, Default `2` lots (`130` qty), Strike step `50`, Weekly Tuesday expiry.
  - **BANKNIFTY**: Spot `NSE|26009`, Lot size `30`, Default `2` lots (`60` qty), Strike step `100`, Monthly Last Tuesday expiry.
- **Strategy Engine**:
  - 15-min Opening Range (`09:15–09:30` IST) with `0.75%` wide-range rejection filter.
  - 3-day `get_time_price_series` 5m historical backfill to warm up **14-period Wilder RSI**.
  - Intraday `09:15`-anchored **VWAP** (with zero-volume spot typical-price fallback).
  - Volatility filter (`India VIX >= 12.0`).
  - Bullish (`CE`) & Bearish (`PE`) triggers within `09:30:01–14:15:00` IST.
- **Execution & Risk Management**:
  - Entry `LMT` (`LTP + 2.5`), `10s` unfilled modify once, `20s` cancel.
  - Immediate `SL-LMT` (`-20/-45` pts), Breakeven at `+25/+55` pts, Target 1 50% booking at `+30/+65` pts (reducing SL qty first), step trailing (`+15/+30` pts).
  - `1s` MTM Kill Switch (`-₹5,000`), `10s` position reconciliation, `15:15` auto square-off, `15:35` CSV export & EOD Telegram report.
- **Live Web UI (`http://localhost:8000`)**: Real-time index levels, MTM, position brackets, live logs, PAPER/LIVE toggle, and emergency Panic Kill Switch.

## Quick Start (Local)
```bash
cp .env.example .env
pip install -r requirements.txt
python main.py
```

Open `http://127.0.0.1:8000` in your browser.

## Run Unit Tests
```bash
pytest tests/test_strategy.py -v
```

## 24/7 Free Cloud VPS Deployment
See [deploy/FREE_VPS_GUIDE.md](deploy/FREE_VPS_GUIDE.md) for step-by-step instructions to deploy 24/7 with a free Static IP on Oracle Cloud Always Free or AWS Free Tier.
