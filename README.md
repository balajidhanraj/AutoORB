---
title: Shoonya Automated ORB Terminal
emoji: 📈
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 8000
short_description: Automated NSE Options Dual-Engine (NIFTY H1 + BANKNIFTY 5-Gate ORB + ST 10,3) Terminal
startup_duration_timeout: 30m
---

# Shoonya Automated Options Trading System (Option 2 Dual-Engine + Supertrend)

Production-grade intraday automated option-buying system for **Nifty 50** and **Bank Nifty** using the **Finvasia Shoonya Trading API (`NorenRestApiOAuth`)**, complete with a **live FastAPI Web Dashboard**, **Headless Morning OAuth 2.0 + TOTP Auto-Login**, **PAPER/LIVE toggle**, **Telegram Alerts**, and **24/7 Free Cloud VPS / Render deployment**.

## Features
- **Unattended Morning Startup (`08:40 – 09:45` IST)**:
  - `08:40` Direct TOTP + OAuth auto-login (Selenium fallback & 120s auto-retry).
  - `08:55` NFO contract & strike cache (60s auto-retry).
  - `09:14` WebSocket live feed + **7-day 5m backfill** to warm 15m HTF swings, Wilder RSI(14), 20-EMA, Supertrend(10,3), and India VIX.
  - `09:45` 30-min Opening Range lock (`0.20%–0.85%` filter).
- **2026 NSE Specifications & Sizing (`₹1,00,000` Global 1-Order Lock)**:
  - **NIFTY**: Spot `NSE|26000`, `65` qty/lot, `2` lots (`3` lots Tue expiry boost), Strike step `50`, Weekly Tuesday expiry.
  - **BANKNIFTY**: Spot `NSE|26009`, `30` qty/lot, `2` lots (`3` lots Tue expiry boost), Strike step `100`, Monthly Last Tuesday expiry.
- **Option 2 Dual-Engine Strategy (`10:00 – 13:15` IST, `India VIX >= 12.0` on both indices)**:
  - **NIFTY Hybrid H1**: 15m HTF Swing Break (`swing_len=2`, `+0.1×ATR` buffer) + `Supertrend(10,3)` confirm & pure close trail, Spot `0.5×15m ATR` SL + `35pt` option SL cap, max `2` trades/day.
  - **BANKNIFTY 5-Gate ORB**: 30m ORB (`09:15–09:45`) + VWAP + Daily Bias + 20-EMA + Body `>= 40%` + RSI(`>60/<40`) + `Supertrend(10,3)` confirm & line close trail, fixed `45pt` SL, `+45pt` BE, `+80pt` T1 (50% book), Stop-on-SL.
- **Execution & Risk Management**:
  - Global 1-Order Lock (`Max 4 trades/day`), `1s` MTM Kill Switch (`-₹10,000`), `10s` position reconciliation, `15:15` auto square-off, `15:35` CSV export & EOD Telegram report.
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
