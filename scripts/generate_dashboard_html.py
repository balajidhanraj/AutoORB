"""Generates the self-contained HTML for the 1-Year Option 2 Dual-Engine Backtest Interactive Dashboard."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRADES_JSON_PATH = ROOT / "data" / "backtest" / "trades_minified.json"

with open(TRADES_JSON_PATH, "r", encoding="utf-8") as f:
    trades_json = f.read().strip()

html_template = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Shoonya ORB 1-Year Option 2 Backtest Analytics (NIFTY H1 + BANKNIFTY)</title>
  <script src="https://www.gstatic.com/antigravity/web/dev/tailwindcss.min.js"></script>
  <style>
    :root {
      --bg: #0b0f19;
      --card: #111827;
      --card-border: #1f2937;
      --foreground: #f3f4f6;
      --muted: #9ca3af;
      --primary: #3b82f6;
      --success: #10b981;
      --danger: #ef4444;
      --warning: #f59e0b;
    }
    body {
      background: var(--bg);
      color: var(--foreground);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    }
    .badge {
      display: inline-flex;
      align-items: center;
      padding: 0.15rem 0.5rem;
      border-radius: 9999px;
      font-size: 0.72rem;
      font-weight: 600;
    }
    .badge-win { background: rgba(16, 185, 129, 0.15); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.3); }
    .badge-loss { background: rgba(239, 68, 68, 0.15); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.3); }
    .badge-be { background: rgba(245, 158, 11, 0.15); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.3); }
  </style>
</head>
<body class="p-4 md:p-6 space-y-6 max-w-7xl mx-auto antialiased">

  <!-- Header -->
  <div class="flex flex-col md:flex-row md:items-center justify-between gap-4 border-b border-[var(--card-border)] pb-4">
    <div>
      <div class="flex items-center gap-3">
        <h1 class="text-xl md:text-2xl font-bold tracking-tight text-white flex items-center gap-2">
          <span>📊 Shoonya AutoORB 1-Year Dual-Engine Backtest</span>
        </h1>
        <span class="badge bg-blue-500/10 text-blue-400 border border-blue-500/30">Nov 2025 – Nov 2026</span>
      </div>
      <p class="text-xs md:text-sm text-[var(--muted)] mt-1">
        1-year bar-by-bar Option 2 backtest (10:00–13:15 IST, India VIX &ge; 12, Supertrend 10,3, Global 1-Order Lock) on Shoonya 5m spot &amp; VIX candles.
      </p>
    </div>
    <div class="flex items-center gap-2 text-xs">
      <span class="px-3 py-1.5 rounded-lg bg-[var(--card)] border border-[var(--card-border)] text-gray-300 font-mono">
        Capital: <strong>₹1,00,000</strong>
      </span>
      <span class="px-3 py-1.5 rounded-lg bg-[var(--card)] border border-[var(--card-border)] text-gray-300 font-mono">
        Lots: <strong>2 Lots (3 Tue Boost)</strong>
      </span>
    </div>
  </div>

  <!-- Interactive Filter Toolbar -->
  <div class="bg-[var(--card)] border border-[var(--card-border)] rounded-xl p-4 space-y-3">
    <div class="flex items-center justify-between text-xs font-semibold text-[var(--muted)] uppercase tracking-wider">
      <span>Interactive Strategy Filters &amp; Scenario Analysis</span>
      <button onclick="resetFilters()" class="text-blue-400 hover:text-blue-300 normal-case underline text-xs font-normal">Reset Filters</button>
    </div>

    <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3 text-xs">
      <!-- Instrument Filter -->
      <div>
        <label class="block text-[var(--muted)] mb-1 font-medium">Instrument</label>
        <select id="filter-inst" onchange="applyFilters()" class="w-full bg-gray-900 border border-gray-700 rounded-lg px-2.5 py-1.5 text-white focus:outline-none focus:border-blue-500">
          <option value="ALL">All Instruments (NIFTY + BNF)</option>
          <option value="BANKNIFTY">BANKNIFTY Only</option>
          <option value="NIFTY">NIFTY Only</option>
        </select>
      </div>

      <!-- Time Window Filter -->
      <div>
        <label class="block text-[var(--muted)] mb-1 font-medium">Breakout Entry Window</label>
        <select id="filter-time" onchange="applyFilters()" class="w-full bg-gray-900 border border-gray-700 rounded-lg px-2.5 py-1.5 text-white focus:outline-none focus:border-blue-500">
          <option value="ALL" selected>All Entries (10:00 – 13:15 IST)</option>
          <option value="SWEET_SPOT">Midday Focus (10:15 – 13:15 IST)</option>
          <option value="AFTER_10">After 10:00 (10:00 – 13:15 IST)</option>
        </select>
      </div>

      <!-- Direction Filter -->
      <div>
        <label class="block text-[var(--muted)] mb-1 font-medium">Signal Direction</label>
        <select id="filter-side" onchange="applyFilters()" class="w-full bg-gray-900 border border-gray-700 rounded-lg px-2.5 py-1.5 text-white focus:outline-none focus:border-blue-500">
          <option value="ALL">Both Directions (CE + PE)</option>
          <option value="CE">Call Breakouts Only (CE)</option>
          <option value="PE">Put Breakdowns Only (PE)</option>
        </select>
      </div>

      <!-- Day of Week Filter -->
      <div>
        <label class="block text-[var(--muted)] mb-1 font-medium">Day of the Week</label>
        <select id="filter-dow" onchange="applyFilters()" class="w-full bg-gray-900 border border-gray-700 rounded-lg px-2.5 py-1.5 text-white focus:outline-none focus:border-blue-500">
          <option value="ALL">All Trading Days (Mon–Fri)</option>
          <option value="TUE">Tuesday (Expiry Day Only)</option>
          <option value="MON">Monday Only</option>
          <option value="WED">Wednesday Only</option>
          <option value="THU">Thursday Only</option>
          <option value="FRI">Friday Only</option>
        </select>
      </div>
    </div>
  </div>

  <!-- KPI Metrics Grid -->
  <div class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
    <div class="bg-[var(--card)] border border-[var(--card-border)] rounded-xl p-3">
      <div class="text-[0.7rem] uppercase tracking-wider text-[var(--muted)]">Net P&amp;L</div>
      <div id="kpi-net" class="text-xl font-bold font-mono mt-1 text-emerald-400">₹0.00</div>
      <div id="kpi-roi" class="text-[0.7rem] text-[var(--muted)] mt-0.5">ROI: 0.0%</div>
    </div>

    <div class="bg-[var(--card)] border border-[var(--card-border)] rounded-xl p-3">
      <div class="text-[0.7rem] uppercase tracking-wider text-[var(--muted)]">Win Rate</div>
      <div id="kpi-winrate" class="text-xl font-bold font-mono mt-1 text-white">0.0%</div>
      <div id="kpi-winloss" class="text-[0.7rem] text-[var(--muted)] mt-0.5">0W / 0L</div>
    </div>

    <div class="bg-[var(--card)] border border-[var(--card-border)] rounded-xl p-3">
      <div class="text-[0.7rem] uppercase tracking-wider text-[var(--muted)]">Profit Factor</div>
      <div id="kpi-pf" class="text-xl font-bold font-mono mt-1 text-blue-400">0.00</div>
      <div id="kpi-expectancy" class="text-[0.7rem] text-[var(--muted)] mt-0.5">Exp: ₹0.00/trd</div>
    </div>

    <div class="bg-[var(--card)] border border-[var(--card-border)] rounded-xl p-3">
      <div class="text-[0.7rem] uppercase tracking-wider text-[var(--muted)]">Gross P&amp;L</div>
      <div id="kpi-gross" class="text-xl font-bold font-mono mt-1 text-white">₹0.00</div>
      <div id="kpi-charges" class="text-[0.7rem] text-red-400 mt-0.5">Taxes: -₹0.00</div>
    </div>

    <div class="bg-[var(--card)] border border-[var(--card-border)] rounded-xl p-3">
      <div class="text-[0.7rem] uppercase tracking-wider text-[var(--muted)]">Max Drawdown</div>
      <div id="kpi-dd" class="text-xl font-bold font-mono mt-1 text-amber-400">₹0.00</div>
      <div id="kpi-ddpct" class="text-[0.7rem] text-[var(--muted)] mt-0.5">0.0% of Capital</div>
    </div>

    <div class="bg-[var(--card)] border border-[var(--card-border)] rounded-xl p-3">
      <div class="text-[0.7rem] uppercase tracking-wider text-[var(--muted)]">Avg Win / Loss</div>
      <div id="kpi-avgwin" class="text-xl font-bold font-mono mt-1 text-white">1.6:1</div>
      <div id="kpi-avgratio" class="text-[0.7rem] text-[var(--muted)] mt-0.5">+₹0 / -₹0</div>
    </div>
  </div>

  <!-- Charts Row -->
  <div class="grid grid-cols-1 lg:grid-cols-3 gap-4">
    <!-- Cumulative Equity Curve (2 Cols) -->
    <div class="lg:col-span-2 bg-[var(--card)] border border-[var(--card-border)] rounded-xl p-4 flex flex-col justify-between">
      <div class="flex items-center justify-between mb-2">
        <h3 class="text-sm font-semibold text-white flex items-center gap-2">
          <span>📈 Cumulative Portfolio Equity Curve</span>
        </h3>
        <span class="text-xs text-[var(--muted)]">₹1,00,000 Initial Capital</span>
      </div>
      <div class="w-full h-64 relative">
        <canvas id="equityCanvas" class="w-full h-full"></canvas>
      </div>
    </div>

    <!-- P&L by Time of Day (1 Col) -->
    <div class="bg-[var(--card)] border border-[var(--card-border)] rounded-xl p-4 flex flex-col justify-between">
      <div class="flex items-center justify-between mb-2">
        <h3 class="text-sm font-semibold text-white flex items-center gap-2">
          <span>⏰ P&amp;L by Entry Time</span>
        </h3>
        <span class="text-xs text-[var(--muted)]">Hourly Breakdown</span>
      </div>
      <div class="w-full h-64 relative">
        <canvas id="timeCanvas" class="w-full h-full"></canvas>
      </div>
    </div>
  </div>

  <!-- Key Findings & Strategy Recommendations Banner -->
  <div class="bg-gradient-to-r from-blue-900/30 to-indigo-900/30 border border-blue-500/30 rounded-xl p-4">
    <div class="flex items-start gap-3">
      <div class="text-2xl">💡</div>
      <div class="space-y-1">
        <h4 class="text-sm font-semibold text-blue-300">Empirical 1-Year Option 2 Dual-Engine Results (214 Trades)</h4>
        <div class="grid grid-cols-1 md:grid-cols-3 gap-3 text-xs text-gray-300 mt-2">
          <div class="bg-gray-900/60 p-2.5 rounded-lg border border-gray-800">
            <strong class="text-emerald-400 block mb-1">1. NIFTY Hybrid H1 (+₹1,86,347)</strong>
            15m HTF Swing Break (swing_len=2) + VIX &ge; 12 + Supertrend(10,3) Confirm &amp; Pure Trail starting at <strong>10:00 IST</strong> delivered <strong>PF 1.83</strong> across 105 trades.
          </div>
          <div class="bg-gray-900/60 p-2.5 rounded-lg border border-gray-800">
            <strong class="text-blue-400 block mb-1">2. BANKNIFTY 5-Gate + ST (+₹98,472)</strong>
            30m ORB + VIX &ge; 12 + Supertrend(10,3) delivered <strong>38.5% WR</strong>, <strong>PF 1.65</strong>, and only -₹22,278 max drawdown across 109 trades.
          </div>
          <div class="bg-gray-900/60 p-2.5 rounded-lg border border-gray-800">
            <strong class="text-amber-400 block mb-1">3. Combined Portfolio (+₹2,84,818)</strong>
            Global 1-Order Lock on ₹1,00,000 capital achieved <strong>PF 1.76</strong>, -₹44,803 MaxDD (6.36x Return/DD), and <strong>10/12 profitable months</strong>.
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- Trade Log Table -->
  <div class="bg-[var(--card)] border border-[var(--card-border)] rounded-xl overflow-hidden">
    <div class="p-4 border-b border-[var(--card-border)] flex flex-col sm:flex-row sm:items-center justify-between gap-3">
      <h3 class="text-sm font-semibold text-white flex items-center gap-2">
        <span>📜 Filtered Trade Ledger</span>
        <span id="trade-count-badge" class="badge bg-gray-800 text-gray-300">0 trades</span>
      </h3>
      <input id="search-input" oninput="renderTable()" placeholder="Search date, strike, exit reason..." class="bg-gray-900 border border-gray-700 rounded-lg px-3 py-1 text-xs text-white placeholder-gray-500 focus:outline-none focus:border-blue-500 w-full sm:w-64" />
    </div>

    <div class="overflow-x-auto max-h-96 text-xs">
      <table class="w-full text-left border-collapse">
        <thead class="bg-gray-900/80 sticky top-0 border-b border-gray-800 text-[var(--muted)] font-mono">
          <tr>
            <th class="p-2.5">Date</th>
            <th class="p-2.5">Index</th>
            <th class="p-2.5">Side</th>
            <th class="p-2.5">Strike</th>
            <th class="p-2.5">In Time</th>
            <th class="p-2.5">Out Time</th>
            <th class="p-2.5 text-right">In Px</th>
            <th class="p-2.5 text-right">Out Px</th>
            <th class="p-2.5 text-right">Pts</th>
            <th class="p-2.5 text-right">Gross P&amp;L</th>
            <th class="p-2.5 text-right">Net P&amp;L</th>
            <th class="p-2.5">Exit Reason</th>
          </tr>
        </thead>
        <tbody id="trade-table-body" class="divide-y divide-gray-800 font-mono">
          <!-- Populated by JS -->
        </tbody>
      </table>
    </div>
  </div>

  <script>
    const ALL_TRADES = __TRADES_JSON__;
    let filteredTrades = [];

    function resetFilters() {
      document.getElementById("filter-inst").value = "ALL";
      document.getElementById("filter-time").value = "ALL";
      document.getElementById("filter-side").value = "ALL";
      document.getElementById("filter-dow").value = "ALL";
      document.getElementById("search-input").value = "";
      applyFilters();
    }

    function applyFilters() {
      const inst = document.getElementById("filter-inst").value;
      const timeW = document.getElementById("filter-time").value;
      const side = document.getElementById("filter-side").value;
      const dow = document.getElementById("filter-dow").value;

      filteredTrades = ALL_TRADES.filter(t => {
        if (inst === "BANKNIFTY" && t.i !== "BNF") return false;
        if (inst === "NIFTY" && t.i !== "NIFTY") return false;
        if (side !== "ALL" && t.s !== side) return false;

        // Time window filter
        if (timeW === "SWEET_SPOT" && (t.in < "10:15" || t.in > "13:15")) return false;
        if (timeW === "AFTER_10" && t.in < "10:00") return false;
        if (timeW === "MORNING_ONLY" && (t.in < "09:30" || t.in >= "10:15")) return false;

        // Day of week
        if (dow !== "ALL") {
          const dt = new Date(t.d + "T00:00:00Z");
          const dayIdx = dt.getUTCDay(); // 0 Sun, 1 Mon, 2 Tue ...
          if (dow === "MON" && dayIdx !== 1) return false;
          if (dow === "TUE" && dayIdx !== 2) return false;
          if (dow === "WED" && dayIdx !== 3) return false;
          if (dow === "THU" && dayIdx !== 4) return false;
          if (dow === "FRI" && dayIdx !== 5) return false;
        }

        return true;
      });

      updateKPIs();
      drawEquityCurve();
      drawTimeChart();
      renderTable();
    }

    function updateKPIs() {
      const total = filteredTrades.length;
      const wins = filteredTrades.filter(t => t.n > 0);
      const losses = filteredTrades.filter(t => t.n < 0);
      const net = filteredTrades.reduce((acc, t) => acc + t.n, 0);
      const gross = filteredTrades.reduce((acc, t) => acc + t.g, 0);
      const charges = filteredTrades.reduce((acc, t) => acc + t.ch, 0);

      const winSum = wins.reduce((acc, t) => acc + t.n, 0);
      const lossSum = Math.abs(losses.reduce((acc, t) => acc + t.n, 0));
      const pf = lossSum > 0 ? (winSum / lossSum) : (winSum > 0 ? 99.0 : 0.0);
      const winRate = total > 0 ? (wins.length / total * 100) : 0;
      const roi = (net / 100000) * 100;
      const expectancy = total > 0 ? (net / total) : 0;

      // Drawdown
      let cum = 0, peak = 0, maxDD = 0;
      filteredTrades.forEach(t => {
        cum += t.n;
        if (cum > peak) peak = cum;
        const dd = peak - cum;
        if (dd > maxDD) maxDD = dd;
      });
      const ddPct = (maxDD / 100000) * 100;

      const avgWin = wins.length > 0 ? winSum / wins.length : 0;
      const avgLoss = losses.length > 0 ? lossSum / losses.length : 0;
      const rrRatio = avgLoss > 0 ? (avgWin / avgLoss).toFixed(1) : "--";

      const netEl = document.getElementById("kpi-net");
      netEl.textContent = (net >= 0 ? "+₹" : "-₹") + Math.abs(net).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
      netEl.className = "text-xl font-bold font-mono mt-1 " + (net >= 0 ? "text-emerald-400" : "text-rose-400");

      const roiPrefix = roi >= 0 ? "+" : "";
      document.getElementById("kpi-roi").textContent = "ROI: " + roiPrefix + roi.toFixed(1) + "%";
      document.getElementById("kpi-winrate").textContent = winRate.toFixed(1) + "%";
      document.getElementById("kpi-winloss").textContent = wins.length + "W / " + losses.length + "L";
      document.getElementById("kpi-pf").textContent = pf.toFixed(2);
      const expPrefix = expectancy >= 0 ? "+" : "";
      document.getElementById("kpi-expectancy").textContent = "Exp: " + expPrefix + "₹" + expectancy.toFixed(0) + "/trd";
      document.getElementById("kpi-gross").textContent = "₹" + gross.toLocaleString("en-IN", { maximumFractionDigits: 0 });
      document.getElementById("kpi-charges").textContent = "Taxes: -₹" + charges.toLocaleString("en-IN", { maximumFractionDigits: 0 });
      document.getElementById("kpi-dd").textContent = "₹" + maxDD.toLocaleString("en-IN", { maximumFractionDigits: 0 });
      document.getElementById("kpi-ddpct").textContent = ddPct.toFixed(1) + "% of Capital";
      document.getElementById("kpi-avgwin").textContent = rrRatio + ":1 RR";
      document.getElementById("kpi-avgratio").textContent = "+₹" + avgWin.toFixed(0) + " / -₹" + avgLoss.toFixed(0);
      document.getElementById("trade-count-badge").textContent = total + " trades";
    }

    function drawEquityCurve() {
      const canvas = document.getElementById("equityCanvas");
      const ctx = canvas.getContext("2d");
      const rect = canvas.getBoundingClientRect();
      canvas.width = rect.width * window.devicePixelRatio;
      canvas.height = rect.height * window.devicePixelRatio;
      ctx.scale(window.devicePixelRatio, window.devicePixelRatio);
      const w = rect.width;
      const h = rect.height;

      ctx.clearRect(0, 0, w, h);

      if (filteredTrades.length === 0) {
        ctx.fillStyle = "#6b7280";
        ctx.font = "12px sans-serif";
        ctx.textAlign = "center";
        ctx.fillText("No trades match the current filter selection.", w / 2, h / 2);
        return;
      }

      // Cumulative data points
      let cum = 0;
      const points = [0];
      filteredTrades.forEach(t => {
        cum += t.n;
        points.push(cum);
      });

      const minVal = Math.min(0, ...points);
      const maxVal = Math.max(1000, ...points);
      const range = maxVal - minVal || 1;

      const padX = 40;
      const padY = 25;
      const chartW = w - padX * 2;
      const chartH = h - padY * 2;

      const zeroY = padY + chartH - ((0 - minVal) / range) * chartH;

      // Draw zero axis line
      ctx.strokeStyle = "rgba(107, 114, 128, 0.4)";
      ctx.lineWidth = 1;
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(padX, zeroY);
      ctx.lineTo(padX + chartW, zeroY);
      ctx.stroke();
      ctx.setLineDash([]);

      // Draw baseline text
      ctx.fillStyle = "#9ca3af";
      ctx.font = "10px monospace";
      ctx.textAlign = "right";
      ctx.fillText("₹0", padX - 6, zeroY + 3);

      const maxLabel = (maxVal >= 0 ? "+₹" : "-₹") + Math.abs(Math.round(maxVal)).toLocaleString("en-IN");
      ctx.fillText(maxLabel, padX - 6, padY + 6);

      // Coordinate mapper
      const getX = i => padX + (i / (points.length - 1)) * chartW;
      const getY = val => padY + chartH - ((val - minVal) / range) * chartH;

      // Draw filled gradient area
      ctx.beginPath();
      ctx.moveTo(getX(0), zeroY);
      points.forEach((val, i) => ctx.lineTo(getX(i), getY(val)));
      ctx.lineTo(getX(points.length - 1), zeroY);
      ctx.closePath();

      const lastVal = points[points.length - 1];
      const isProfitable = lastVal >= 0;
      const grad = ctx.createLinearGradient(0, padY, 0, padY + chartH);
      if (isProfitable) {
        grad.addColorStop(0, "rgba(16, 185, 129, 0.35)");
        grad.addColorStop(1, "rgba(16, 185, 129, 0.0)");
      } else {
        grad.addColorStop(0, "rgba(239, 68, 68, 0.35)");
        grad.addColorStop(1, "rgba(239, 68, 68, 0.0)");
      }
      ctx.fillStyle = grad;
      ctx.fill();

      // Draw Line
      ctx.beginPath();
      points.forEach((val, i) => {
        const x = getX(i);
        const y = getY(val);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.strokeStyle = isProfitable ? "#10b981" : "#ef4444";
      ctx.lineWidth = 2.2;
      ctx.stroke();

      // End point marker
      const endX = getX(points.length - 1);
      const endY = getY(lastVal);
      ctx.fillStyle = isProfitable ? "#34d399" : "#f87171";
      ctx.beginPath();
      ctx.arc(endX, endY, 4, 0, Math.PI * 2);
      ctx.fill();
    }

    function drawTimeChart() {
      const canvas = document.getElementById("timeCanvas");
      const ctx = canvas.getContext("2d");
      const rect = canvas.getBoundingClientRect();
      canvas.width = rect.width * window.devicePixelRatio;
      canvas.height = rect.height * window.devicePixelRatio;
      ctx.scale(window.devicePixelRatio, window.devicePixelRatio);
      const w = rect.width;
      const h = rect.height;

      ctx.clearRect(0, 0, w, h);

      const buckets = [
        { label: "10:00–11:00", filter: t => t.in < "11:00" },
        { label: "11:00–12:00", filter: t => t.in >= "11:00" && t.in < "12:00" },
        { label: "12:00–13:00", filter: t => t.in >= "12:00" && t.in <= "13:00" },
        { label: "13:00–13:15", filter: t => t.in > "13:00" }
      ];

      const pnlData = buckets.map(b => {
        const subset = filteredTrades.filter(b.filter);
        const sum = subset.reduce((acc, t) => acc + t.n, 0);
        return { label: b.label, pnl: sum, count: subset.length };
      });

      const maxAbs = Math.max(1000, ...pnlData.map(d => Math.abs(d.pnl)));
      const padX = 20;
      const padY = 25;
      const barH = (h - padY * 2) / buckets.length;
      const midX = padX + (w - padX * 2) / 2;

      // Draw Center Line
      ctx.strokeStyle = "rgba(107, 114, 128, 0.4)";
      ctx.lineWidth = 1;
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(midX, padY);
      ctx.lineTo(midX, h - padY);
      ctx.stroke();
      ctx.setLineDash([]);

      pnlData.forEach((d, idx) => {
        const y = padY + idx * barH;
        const barWidth = (Math.abs(d.pnl) / maxAbs) * ((w - padX * 2) / 2 - 10);
        const isPos = d.pnl >= 0;

        ctx.fillStyle = isPos ? "rgba(16, 185, 129, 0.75)" : "rgba(239, 68, 68, 0.75)";
        if (isPos) {
          ctx.fillRect(midX, y + 6, barWidth, barH - 12);
        } else {
          ctx.fillRect(midX - barWidth, y + 6, barWidth, barH - 12);
        }

        // Text Labels
        ctx.fillStyle = "#e5e7eb";
        ctx.font = "10px monospace";
        ctx.textAlign = isPos ? "left" : "right";
        const valTxt = (isPos ? "+₹" : "-₹") + Math.abs(Math.round(d.pnl)).toLocaleString("en-IN");
        ctx.fillText(valTxt, isPos ? (midX + barWidth + 5) : (midX - barWidth - 5), y + barH / 2 + 3);

        ctx.fillStyle = "#9ca3af";
        ctx.textAlign = "center";
        ctx.font = "9px sans-serif";
        ctx.fillText(d.label + " (" + d.count + ")", midX, y + 3);
      });
    }

    function renderTable() {
      const tbody = document.getElementById("trade-table-body");
      const search = document.getElementById("search-input").value.toLowerCase().trim();

      const list = filteredTrades.filter(t => {
        if (!search) return true;
        return (
          t.d.includes(search) ||
          t.i.toLowerCase().includes(search) ||
          t.s.toLowerCase().includes(search) ||
          String(t.k).includes(search) ||
          t.r.toLowerCase().includes(search)
        );
      });

      tbody.innerHTML = list.slice(0, 150).map(t => {
        const isWin = t.n > 0;
        const isBE = t.n === 0 || (t.pts >= -2 && t.pts <= 2 && t.r === "BREAKEVEN_SL");
        const badgeClass = isBE ? "badge-be" : (isWin ? "badge-win" : "badge-loss");
        const ptsTxt = (t.pts >= 0 ? "+" : "") + t.pts.toFixed(1);
        const netTxt = (t.n >= 0 ? "+₹" : "-₹") + Math.abs(t.n).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        const grossTxt = (t.g >= 0 ? "+₹" : "-₹") + Math.abs(t.g).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

        return `
          <tr class="hover:bg-gray-800/40">
            <td class="p-2.5 text-gray-300 font-medium">${t.d}</td>
            <td class="p-2.5"><span class="badge ${t.i === 'BNF' ? 'bg-amber-500/10 text-amber-400 border border-amber-500/30' : 'bg-blue-500/10 text-blue-400 border border-blue-500/30'}">${t.i}</span></td>
            <td class="p-2.5 font-bold ${t.s === 'CE' ? 'text-emerald-400' : 'text-rose-400'}">${t.s}</td>
            <td class="p-2.5 text-gray-300">${t.k}</td>
            <td class="p-2.5 text-gray-400">${t.in}</td>
            <td class="p-2.5 text-gray-400">${t.out}</td>
            <td class="p-2.5 text-right text-gray-300">₹${t.inp.toFixed(1)}</td>
            <td class="p-2.5 text-right text-gray-300">₹${t.outp.toFixed(1)}</td>
            <td class="p-2.5 text-right font-bold ${t.pts >= 0 ? 'text-emerald-400' : 'text-rose-400'}">${ptsTxt}</td>
            <td class="p-2.5 text-right text-gray-300">${grossTxt}</td>
            <td class="p-2.5 text-right font-bold ${t.n >= 0 ? 'text-emerald-400' : 'text-rose-400'}">${netTxt}</td>
            <td class="p-2.5"><span class="badge ${badgeClass}">${t.r}</span></td>
          </tr>
        `;
      }).join("");
    }

    window.addEventListener("resize", () => {
      drawEquityCurve();
      drawTimeChart();
    });

    // Initial render
    applyFilters();
  </script>
</body>
</html>
"""

final_html = html_template.replace("__TRADES_JSON__", trades_json)
out_file = ROOT / "data" / "backtest" / "backtest_dashboard.html"
with open(out_file, "w", encoding="utf-8") as f:
    f.write(final_html)

print(f"Generated standalone dashboard HTML at: {out_file} ({len(final_html)} bytes)")
