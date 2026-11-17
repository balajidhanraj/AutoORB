# Project Specification: Shoonya Automated Options Trading System (ORB + VWAP + RSI)

## 1. Objective & System Overview
Build an end-to-end, production-grade automated intraday option-buying trading system in Python for **Nifty 50** and **Bank Nifty** using the **Finvasia Shoonya Trading API (Noren OMS)**.

The system supports:
- **Paper Trading Mode (`MODE="PAPER"`)** and **Live Trading Mode (`MODE="LIVE"`)** toggleable via `.env` or UI (when flat).
- **Headless Morning OAuth 2.0 + TOTP Auto-Login** (`08:40 AM` IST) using Selenium + `pyotp` to capture the daily OAuth redirect `code` and persist `SHOONYA_ACCESS_TOKEN`.
- **SEBI 2026 Retail Algo Compliance** (Static IP execution, mandatory daily OAuth 2.0 session handling, and exchange-approved `algo_id` order tagging).
- Real-time WebSocket tick ingestion, 5-minute OHLCV candle aggregation, technical indicator calculation (VWAP, 14-period Wilder RSI), automated ATM/ITM option strike selection, bracket-style risk management (Initial SL, Breakeven, Partial Target, Trailing SL), daily MTM kill-switch, live FastAPI Web UI, and Telegram notifications.

## 2. 2026 NSE Specifications
- **NIFTY**: Spot `NSE|26000`, Lot size `65`, Default `2` lots (`130` qty), Strike step `50`, Weekly Tuesday expiry, Initial SL `-20` pts (buffer `3`), Breakeven `+25` pts, Target 1 `+30` pts (50% exit), Trail step `15` pts.
- **BANKNIFTY**: Spot `NSE|26009`, Lot size `30`, Default `2` lots (`60` qty), Strike step `100`, Monthly Last Tuesday expiry, Initial SL `-45` pts (buffer `5`), Breakeven `+55` pts, Target 1 `+65` pts (50% exit), Trail step `30` pts.
- **Range Filter**: `09:15–09:30` IST (first 3 5-min candles). Reject index if `((ORH - ORL) / ORL) * 100 > 0.75%`.
- **Volatility Filter**: Skip entries if `India VIX < 12.0`.
- **Entry Window**: `09:30:01` to `14:15:00` IST.
- **Auto Square-Off**: `15:15:00` IST.
- **Daily Circuit Breakers**: Max `2` trades/day, `-₹5,000` MTM kill-switch.
