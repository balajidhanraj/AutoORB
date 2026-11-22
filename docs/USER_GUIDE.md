# Shoonya Automated ORB Trading Terminal — User & Strategy Guide

## Overview

The **Shoonya Automated ORB Trading Terminal (AutoORB)** is an institutional-grade, algorithmic options trading system engineered for **NIFTY** and **BANKNIFTY** index options. It runs the **Option 2 Dual-Engine Strategy** using the official Finvasia Shoonya API with a strict **Global 1-Order Lock (`₹1,00,000` capital safe)**, `India VIX >= 12.0` filter on both indices, and **Supertrend(10,3)** confirmation & trailing.

---

## 1. Option 2 Dual-Engine Strategy Blueprint

Calibrated on 1 full year (`2025-11-20` to `2026-11-20`) of real Shoonya 5-minute spot and India VIX data (**214 trades**, **+₹2,84,818 net**, **PF 1.76**, **Max DD -₹44,803**, **10/12 profitable months**):

```
08:40        08:55        09:14        09:45       10:00                          13:15          15:15
  |------------|------------|------------|-----------|------------------------------|--------------|
Auto OAuth   NFO Cache    WS + 7D      30m ORB Lock  Option 2 Dual-Engine Scan      No New       EOD Square-Off
 (120s retry)             Backfill     (BNF Range)   (NF H1 + BNF 5-Gate, VIX>=12)  Entries      & 15:35 CSV
```

### Core Execution Rules

1. **Global Entry Window & Volatility Gate (10:00 – 13:15 IST, India VIX ≥ 12.0)**:
   - Both **NIFTY** and **BANKNIFTY** scan from **10:00 IST to 13:15 IST**, avoiding `09:30–09:55` opening whipsaws and afternoon theta decay.
   - **India VIX ≥ 12.0** is enforced across **both** NIFTY and BANKNIFTY to skip dead low-volatility chop.
   - **Global 1-Order Lock**: Only **one active position** across NIFTY + BANKNIFTY is held at a time (`Max 4 trades/day` global).

2. **NIFTY — Hybrid H1 Engine (`15m HTF Swing Break + Supertrend(10,3)`, Max 2/day)**:
   - Uses non-repainting **15-minute HTF Swing High (`15m RED`) / Swing Low (`15m GRN`)** levels (`swing_len=2`) with a `+0.1 × 15m ATR(14)` breakout buffer.
   - Entry triggers on a 5m close breaking `15m RED` (CE) or `15m GRN` (PE) confirmed by **5m Supertrend(10,3)** (`dir == +1` for CE / `-1` for PE).
   - **Stop Loss**: Dual protection — Spot `0.5 × 15m ATR` stop (`SPOT_ATR_SL`) plus hard **`35.0 pt`** option SL cap (`STOP_LOSS`).
   - **Pure Supertrend Runner Trail**: Skips early breakeven/T1 partial cuts and trails full quantity until a 5m spot close crosses the Supertrend(10,3) line.
   - *1-Year Verified Performance*: `105` trades, `35.2%` WR, **PF `1.83`**, **+₹1,86,347** net, Max DD `-₹32,152`.

3. **BANKNIFTY — 5-Gate 30m ORB + Supertrend(10,3) (`Stop-on-SL`)**:
   - **30-Minute Opening Range (`09:15 – 09:45 IST`)**: Six 5m candles; range filter **`0.20% – 0.85%`**.
   - **5-Gate + Institutional Filters**:
     - **Gate 1 (Sys)**: System active, `India VIX >= 12.0`, `< 4` global trades/day, no prior `STOP_LOSS` on BANKNIFTY today, no active position in either index.
     - **Gate 2 (VIX)**: `India VIX >= 12.0`.
     - **Gate 3 (ORB + VWAP + PDC)**: 5m close breaks `ORH` (CE) / `ORL` (PE), aligned with intraday `VWAP` and Previous Day Close (`PDC`).
     - **Gate 4 (Momentum & Candle Structure)**: Wilder `RSI(14)` `> 60` (CE) / `< 40` (PE), aligned with `20-EMA` and candle body ratio `>= 40%`.
     - **Gate 5 (Supertrend 10,3)**: Aligned with 5m `Supertrend(10,3)`.
   - **Bracket & Exit Trail**:
     - Fixed Initial Stop Loss: **`45.0 points`**.
     - Breakeven Lock: **`+45.0 points`** gain moves SL to entry cost.
     - Target 1 (50% Book): **`+80.0 points`** books 50% of lots; remaining quantity trails on **5m Supertrend(10,3) Line Close**.
   - *1-Year Verified Performance*: `109` trades, `38.5%` WR, **PF `1.65`**, **+₹98,472** net, Max DD `-₹22,278`.

4. **Option Contract Selection & Tuesday Expiry Boost**:
   - **NIFTY**: Spot `NSE|26000`, `65` qty/lot, Weekly Tuesday expiry (`NRML` default).
   - **BANKNIFTY**: Spot `NSE|26009`, `30` qty/lot, Monthly Last Tuesday expiry (`NRML` default).
   - **Position Sizing**: **2 lots** (`Mon/Wed/Thu/Fri`, 50% allocation) and **3 lots** on **Tuesdays** (75% allocation).
   - **Daily Risk Cap**: **`-₹10,000`** MTM kill-switch auto-liquidates and halts on breach.

---

## 2. Settings & Parameter Reference

| Setting | Default Value | Description |
| :--- | :--- | :--- |
| **Trading Mode** | `LIVE` / `PAPER` | `LIVE` routes real orders to Shoonya via API. `PAPER` simulates fills cleanly. |
| **Default Product** | `NRML` (`M`) | `NRML` prevents broker auto-square-off; switchable to `MIS` (`I`) per index. |
| **Capital Base** | `₹1,00,000` | Enforces Global 1-Order Lock so ₹1L capital is sufficient across both indices. |
| **NIFTY Engine** | Hybrid H1 (`10:00–13:15`) | `65` qty/lot, max `2`/day, Spot `0.5×ATR` (`35pt` cap), Pure ST(10,3) Close Trail. |
| **BANKNIFTY Engine** | 5-Gate 30m ORB (`10:00–13:15`) | `30` qty/lot, `45pt` SL, `+45` BE, `+80` T1 (50%), ST(10,3) Line Trail. |
| **Daily Loss Limit** | `-₹10,000` | Hard risk barrier; cancels pending orders & liquidates open legs immediately. |
| **Max Trades / Day** | `4` Global (`2` NIFTY H1) | Maximum automated trades per session. |

---

## 3. Unattended Daily Workflow (Mon–Fri, No Manual Intervention)

1. **08:40 IST (`Auto OAuth Login`)**: Direct TOTP + OAuth login (with Selenium fallback and **120s automatic retry** if transient errors occur).
2. **08:55 IST (`NFO Contract Cache`)**: Downloads and indexes Shoonya NFO option strikes & expiries (retries every `60s` if count is `0`).
3. **09:14 IST (`WS & 7D Backfill`)**: Connects Shoonya WebSocket for `NIFTY`, `BANKNIFTY`, and `INDIAVIX`, backfills **7 days** of 5m candles, warms up `15m HTF Swings`, `ATR(14)`, `RSI(14)`, `20-EMA`, and `Supertrend(10,3)`.
4. **09:45 IST (`30-Min ORB Lock`)**: Locks BANKNIFTY `09:15–09:45` Opening Range (`ORH`/`ORL`) and range filter (`0.20%–0.85%`).
5. **10:00 – 13:15 IST (`Dual-Engine Scan`)**: Evaluates every closed 5m candle for both NIFTY Hybrid H1 and BANKNIFTY 5-Gate ORB with `VIX >= 12.0` and Global 1-Order Lock.
6. **15:15 – 15:35 IST (`Auto EOD Square-Off & Report`)**: Squares off any open positions at `15:15 IST`, exports CSV logs, and dispatches Telegram EOD summary at `15:35 IST`.
