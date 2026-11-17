# Shoonya Automated ORB Trading Terminal — User & Strategy Guide

## Overview

The **Shoonya Automated ORB Trading Terminal** is an institutional-grade, algorithmic options trading system engineered for **NIFTY** and **BANKNIFTY** index options. It executes the **Master Alpha 15% Strategy** using the official Shoonya (Finvasia) API, delivering disciplined, risk-governed automated execution with sub-second WebSocket telemetry.

---

## 1. Master Alpha 15% Strategy Blueprint

The Master Alpha 15% Strategy is calibrated from 180 days (6 months) of tick-by-tick market data, designed to target consistent ~15% monthly return on trading capital while preserving capital through strict drawdowns guards.

```
09:15                      09:45             10:00                          13:15          15:15
  |--------------------------|-----------------|------------------------------|--------------|
      30-Min ORB Lock          Morning Shakeout      Institutional Scan Window    Decay Avoidance    EOD Square-Off
     (Calculate ORH/ORL)        (No Orders)         (5-Gate Active Breakout)       (No New Orders)   (Auto Exit)
```

### Core Execution Rules

1. **30-Minute Opening Range (09:15 – 09:45 IST)**:
   - High (ORH) and Low (ORL) are calculated from six 5-minute candles.
   - **Range Filter Gate**: The opening range must be between **0.20% and 0.85%**. Narrow ranges (< 0.20%) indicate dead markets; wide ranges (> 0.85%) indicate exhaustion and are skipped.

2. **Institutional Entry Scan Window (10:00 – 13:15 IST)**:
   - No breakout orders are permitted between 09:45 and 10:00 IST. This eliminates over 70% of morning retail fakeout traps and sudden reversals.
   - New entries cease at **13:15 IST** to avoid afternoon choppy theta decay.

3. **5-Gate Breakout Verification**:
   Before an order is triggered, all 5 gates must simultaneously pass:
   - **Gate 1 (Sys/Slot)**: System active, market hours valid, trades taken < 2.
   - **Gate 2 (India VIX >= 12.0)**: Sufficient implied volatility to support option premium expansion.
   - **Gate 3 (Range 0.20%–0.85%)**: Opening range within statistical breakout boundary.
   - **Gate 4 (Spot Breakout + Intraday VWAP)**:
     - For **CE (Call)**: 5M candle close > ORH and close > VWAP and close >= Previous Day Close (PDC).
     - For **PE (Put)**: 5M candle close < ORL and close < VWAP and close <= Previous Day Close (PDC).
   - **Gate 5 (Wilder RSI Momentum)**:
     - For **CE**: Wilder RSI(14) > 60.0.
     - For **PE**: Wilder RSI(14) < 40.0.

4. **Option Contract Selection**:
   - Automatically resolves the current weekly expiry contract.
   - Selects the nearest round ATM strike for highest liquidity and delta (~0.58).
   - Default product type: **NRML** (Normal margin), switchable to **MIS** (Intraday).

5. **Widened Stop Loss & Bracket Lifecycle**:
   - **NIFTY**:
     - Initial Stop Loss: **25.0 points** (widened from 20 points to withstand spot wicks).
     - Breakeven Trigger: At **+20 points** gain, initial SL is automatically trailed to entry price (cost).
     - Target 1: At **+40 points** gain, **50% of lots are booked**, and the remaining 50% lots trail upward.
   - **BANKNIFTY**:
     - Initial Stop Loss: **50.0 points**.
     - Breakeven Trigger: At **+40 points** gain, SL moved to breakeven.
     - Target 1: At **+80 points** gain, 50% booked, remaining trail.

6. **Tuesday Expiry Sizing Boost**:
   - **Tuesdays**: Capital allocation increases to **75%** (typically 3 lots) to capitalize on favorable risk-to-reward dynamics on expiry day.
   - **Mon/Wed/Thu/Fri**: Capital allocation is **50%** (typically 2 lots).

7. **Risk Governance & Weekly Loss Guard**:
   - **Max Trades Per Day**: Exactly 2 trades per session.
   - **Max Daily Loss Limit**: -₹5,000 (breach triggers immediate Emergency Kill-Switch).
   - **Weekly Loss Guard**: If 2 losing trades occur in a calendar week, position sizing drops to 1 lot or halts until next Monday to protect accumulated capital.

---

## 2. Settings & Parameter Reference

| Setting | Options / Values | Description |
| :--- | :--- | :--- |
| **Trading Mode** | `LIVE` / `PAPER` | `LIVE` routes real orders to Shoonya via API. `PAPER` simulates fills with real-time slippage. |
| **Default Product** | `NRML` / `MIS` | `NRML` prevents auto-square-off by broker; `MIS` utilizes intraday leverage margins. |
| **Capital Allocation** | 50% (75% Tue) | Dynamically allocates available funds from broker terminal (Live) or assigned capital (Paper). |
| **NIFTY Lots** | 1 to 10 (Default: 2-3) | Lot size: 65 qty per lot. |
| **BANKNIFTY Lots** | 1 to 10 (Default: 2-3) | Lot size: 30 qty per lot. |
| **Daily Loss Limit** | -₹5,000 | Hard risk barrier; initiates broker order cancellation and position square-off. |
| **Max Trades / Day** | 2 | Maximum trade attempts permitted per session. |

---

## 3. Terminal Navigation & Controls

### Top Telemetry Bar
* **MODE Pill**: Displays current execution mode (`LIVE` in green, `PAPER` in amber). Click to toggle.
* **Client / User Pill**: Displays active Shoonya Client ID. Click to open the **Multi-User Account Switcher**.
* **OAuth Status**: Shows session token validity. Click to trigger fresh OAuth login.
* **WS**: Live WebSocket market stream connection state (`ONLINE` or `OFFLINE`).
* **Header Buttons**:
  - `🔊 / 🔇 Sound`: Toggles Web Audio chimes for trade entries, targets, and risk alerts.
  - `🔔 Bell`: Opens the event notification history drawer.
  - `🎨 Theme`: Opens the Appearance Settings (choose from 9 color palettes and 4 typography styles).
  - `⚡ BROKER ORDER`: Opens the manual Quick Order ticket.
  - `KILL SWITCH`: Emergency cancellation of all open orders and immediate market square-off.

### Tab 1: Radar & Signals Cockpit
* **Cockpit Columns**:
  1. **NIFTY Spot & Auto-Order Radar**: Live spot price, 09:15-09:45 ORH/ORL, 5-gate readiness progress bar, ATM contract candidate, lot selector, and manual order button.
  2. **BANKNIFTY Spot & Auto-Order Radar**: Same layout for Bank Nifty options.
  3. **Shoonya Broker, Funds & Daily Timeline**: Displays real-time broker cash, margin used, net available margin, and institutional milestone timeline.
* **Live Execution Stream**: Located right below the 3 cockpit cards, displays color-coded real-time log messages (`[ORDER]`, `[SIGNAL]`, `[WARN]`, `[ERROR]`) with auto-scroll and quick filter chips.

### Tab 2: Positions & Books
* **Active Bracket Positions**: Displays open strategy bracket trades, current LTP, stop loss trigger, target prices, and live unrealized P&L.
* **Completed Strategy Trades Ledger**: Comprehensive table of all trades executed today and seeded backtest history with a dedicated **Mode** column (`LIVE`, `PAPER`, or `BACKTEST`), filter tabs (`All`, `Live`, `Paper`, `Backtest`), and **Download CSV** export.

### Tab 3: Terminal Logs
* Full-height audit console showing complete session logs with search/filter, auto-scroll toggle, copy to clipboard, and manual refresh.

### Tab 4: Analytics & Backtest
* Interactive 6-month backtest analytics dashboard featuring equity curves, monthly breakdown, win/loss distributions, and alpha metrics.

---

## 4. Multi-User Account Switching

1. Click the **Switch User** button in the header.
2. Select a preset (e.g., `FA53975`) or input custom credentials:
   - **User ID** (Client Code)
   - **Password**
   - **TOTP Secret** (32-character Google Authenticator key)
   - **API Key**
   - **Account ID**
   - **Secret Key** (SHA-256 HMAC key)
3. Click **Authenticate & Switch Session**.
4. The terminal validates the credentials, fetches a fresh token, reconciles broker margins, and switches live order routing.

---

## 5. Daily Trading Workflow

1. **08:45 – 09:10 IST**: Open terminal at `http://localhost:8000`. Verify `WS: ONLINE` and broker cash margin.
2. **09:15 – 09:45 IST**: Strategy observes market open. The 30-minute opening range is established. At 09:45, badges change to `ORB LOCKED`.
3. **09:45 – 10:00 IST**: Strategy rests during morning shakeout.
4. **10:00 – 13:15 IST**: Institutional scan window active. When all 5 gates pass on a 5-minute candle close, an entry order is placed automatically.
5. **Trade Execution**: Stop loss and Target 1 bracket orders are managed automatically. Audio chimes and desktop push notifications alert you of trade milestones.
6. **15:15 IST**: Any surviving open positions are automatically squared off before market close.
