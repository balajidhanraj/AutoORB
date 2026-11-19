/**
 * Shared Tailwind Theme Config & API Client for AutoORB Desktop + Mobile + Capacitor Android
 */
(function (global) {
  'use strict';

  const DEFAULT_ANDROID_API_BASE = 'http://192.168.0.109:8000';

  // Shared Tailwind CSS configuration referencing CSS RGB variables in theme.css
  global.AUTOORB_TAILWIND_CONFIG = {
    darkMode: 'class',
    theme: {
      extend: {
        colors: {
          background: 'rgb(var(--rgb-background) / <alpha-value>)',
          surface: 'rgb(var(--rgb-surface) / <alpha-value>)',
          'surface-dim': 'rgb(var(--rgb-surface-dim) / <alpha-value>)',
          'surface-bright': 'rgb(var(--rgb-surface-bright) / <alpha-value>)',
          'surface-container-lowest': 'rgb(var(--rgb-surface-container-lowest) / <alpha-value>)',
          'surface-container-low': 'rgb(var(--rgb-surface-container-low) / <alpha-value>)',
          'surface-container': 'rgb(var(--rgb-surface-container) / <alpha-value>)',
          'surface-container-high': 'rgb(var(--rgb-surface-container-high) / <alpha-value>)',
          'surface-container-highest': 'rgb(var(--rgb-surface-container-highest) / <alpha-value>)',
          'surface-variant': 'rgb(var(--rgb-surface-variant) / <alpha-value>)',
          'on-surface': 'rgb(var(--rgb-on-surface) / <alpha-value>)',
          'on-background': 'rgb(var(--rgb-on-background) / <alpha-value>)',
          'on-surface-variant': 'rgb(var(--rgb-on-surface-variant) / <alpha-value>)',
          'inverse-surface': 'rgb(var(--rgb-inverse-surface) / <alpha-value>)',
          'inverse-on-surface': 'rgb(var(--rgb-inverse-on-surface) / <alpha-value>)',
          outline: 'rgb(var(--rgb-outline) / <alpha-value>)',
          'outline-variant': 'rgb(var(--rgb-outline-variant) / <alpha-value>)',
          'surface-tint': 'rgb(var(--rgb-surface-tint) / <alpha-value>)',
          primary: 'rgb(var(--rgb-primary) / <alpha-value>)',
          'on-primary': 'rgb(var(--rgb-on-primary) / <alpha-value>)',
          'primary-container': 'rgb(var(--rgb-primary-container) / <alpha-value>)',
          'on-primary-container': 'rgb(var(--rgb-on-primary-container) / <alpha-value>)',
          'inverse-primary': 'rgb(var(--rgb-inverse-primary) / <alpha-value>)',
          'primary-fixed': 'rgb(var(--rgb-primary-fixed) / <alpha-value>)',
          'primary-fixed-dim': 'rgb(var(--rgb-primary-fixed-dim) / <alpha-value>)',
          'on-primary-fixed': 'rgb(var(--rgb-on-primary-fixed) / <alpha-value>)',
          'on-primary-fixed-variant': 'rgb(var(--rgb-on-primary-fixed-variant) / <alpha-value>)',
          secondary: 'rgb(var(--rgb-secondary) / <alpha-value>)',
          'on-secondary': 'rgb(var(--rgb-on-secondary) / <alpha-value>)',
          'secondary-container': 'rgb(var(--rgb-secondary-container) / <alpha-value>)',
          'on-secondary-container': 'rgb(var(--rgb-on-secondary-container) / <alpha-value>)',
          'secondary-fixed': 'rgb(var(--rgb-secondary-fixed) / <alpha-value>)',
          'secondary-fixed-dim': 'rgb(var(--rgb-secondary-fixed-dim) / <alpha-value>)',
          'on-secondary-fixed': 'rgb(var(--rgb-on-secondary-fixed) / <alpha-value>)',
          'on-secondary-fixed-variant': 'rgb(var(--rgb-on-secondary-fixed-variant) / <alpha-value>)',
          tertiary: 'rgb(var(--rgb-tertiary) / <alpha-value>)',
          'on-tertiary': 'rgb(var(--rgb-on-tertiary) / <alpha-value>)',
          'tertiary-container': 'rgb(var(--rgb-tertiary-container) / <alpha-value>)',
          'on-tertiary-container': 'rgb(var(--rgb-on-tertiary-container) / <alpha-value>)',
          'tertiary-fixed': 'rgb(var(--rgb-tertiary-fixed) / <alpha-value>)',
          'tertiary-fixed-dim': 'rgb(var(--rgb-tertiary-fixed-dim) / <alpha-value>)',
          'on-tertiary-fixed': 'rgb(var(--rgb-on-tertiary-fixed) / <alpha-value>)',
          'on-tertiary-fixed-variant': 'rgb(var(--rgb-on-tertiary-fixed-variant) / <alpha-value>)',
          error: 'rgb(var(--rgb-error) / <alpha-value>)',
          'on-error': 'rgb(var(--rgb-on-error) / <alpha-value>)',
          'error-container': 'rgb(var(--rgb-error-container) / <alpha-value>)',
          'on-error-container': 'rgb(var(--rgb-on-error-container) / <alpha-value>)'
        },
        borderRadius: {
          DEFAULT: '0.25rem',
          lg: '0.5rem',
          xl: '0.75rem',
          full: '9999px'
        },
        spacing: {
          margin: '0.75rem',
          'margin-desktop': '1.5rem',
          gutter: '0.5rem',
          'gutter-desktop': '0.75rem',
          'space-xs': '0.25rem',
          'space-sm': '0.5rem',
          'space-md': '0.75rem',
          'space-lg': '1rem',
          'space-xl': '1.5rem'
        },
        fontFamily: {
          headline: ['Space Grotesk', 'sans-serif'],
          display: ['Space Grotesk', 'sans-serif'],
          'display-lg': ['Space Grotesk', 'sans-serif'],
          'display-lg-mobile': ['Space Grotesk', 'sans-serif'],
          'headline-md': ['Space Grotesk', 'sans-serif'],
          body: ['Geist', 'Inter', 'sans-serif'],
          'body-md': ['Geist', 'Inter', 'sans-serif'],
          'body-sm': ['Geist', 'Inter', 'sans-serif'],
          'title-sm': ['Geist', 'Inter', 'sans-serif'],
          label: ['JetBrains Mono', 'monospace'],
          'label-caps': ['JetBrains Mono', 'monospace'],
          'data-mono-lg': ['JetBrains Mono', 'monospace'],
          'data-mono-md': ['JetBrains Mono', 'monospace'],
          'data-mono-sm': ['JetBrains Mono', 'monospace']
        },
        fontSize: {
          'display-lg': ['36px', { lineHeight: '44px', letterSpacing: '-0.03em', fontWeight: '700' }],
          'display-lg-mobile': ['28px', { lineHeight: '34px', letterSpacing: '-0.02em', fontWeight: '700' }],
          'headline-md': ['22px', { lineHeight: '28px', letterSpacing: '-0.01em', fontWeight: '600' }],
          'title-sm': ['16px', { lineHeight: '22px', letterSpacing: '0em', fontWeight: '600' }],
          'body-md': ['14px', { lineHeight: '20px', letterSpacing: '0.01em', fontWeight: '400' }],
          'body-sm': ['12px', { lineHeight: '16px', letterSpacing: '0.01em', fontWeight: '400' }],
          'data-mono-lg': ['16px', { lineHeight: '20px', letterSpacing: '-0.02em', fontWeight: '500' }],
          'data-mono-md': ['13px', { lineHeight: '16px', letterSpacing: '-0.01em', fontWeight: '500' }],
          'data-mono-sm': ['11px', { lineHeight: '14px', letterSpacing: '0em', fontWeight: '400' }],
          'label-caps': ['10px', { lineHeight: '12px', letterSpacing: '0.08em', fontWeight: '600' }]
        }
      }
    }
  };

  if (global.tailwind) {
    global.tailwind.config = global.AUTOORB_TAILWIND_CONFIG;
  }

  function isNativeCapacitor() {
    try {
      return Boolean(
        (global.Capacitor && typeof global.Capacitor.isNativePlatform === 'function' && global.Capacitor.isNativePlatform()) ||
        global.location.protocol === 'file:' ||
        global.location.protocol === 'capacitor:'
      );
    } catch (_e) {
      return false;
    }
  }

  function getApiBase() {
    const saved = (localStorage.getItem('autoorb_api_base') || '').trim().replace(/\/+$/, '');
    if (saved) return saved;
    if (isNativeCapacitor()) return DEFAULT_ANDROID_API_BASE;
    return '';
  }

  function setApiBase(url) {
    const clean = (url || '').trim().replace(/\/+$/, '');
    if (!clean) {
      localStorage.removeItem('autoorb_api_base');
    } else {
      localStorage.setItem('autoorb_api_base', clean);
    }
    return getApiBase();
  }

  function getToken() {
    return localStorage.getItem('autoorb_token') || '';
  }

  function setToken(token) {
    if (token) {
      localStorage.setItem('autoorb_token', token);
    } else {
      localStorage.removeItem('autoorb_token');
    }
  }

  function getTheme() {
    return localStorage.getItem('autoorb_theme') || 'dark';
  }

  function setTheme(themeName) {
    const next = themeName === 'light' ? 'light' : 'dark';
    localStorage.setItem('autoorb_theme', next);
    if (next === 'light') {
      document.documentElement.setAttribute('data-theme', 'light');
      document.documentElement.classList.remove('dark');
    } else {
      document.documentElement.removeAttribute('data-theme');
      document.documentElement.classList.add('dark');
    }
    return next;
  }

  function toggleTheme() {
    return setTheme(getTheme() === 'dark' ? 'light' : 'dark');
  }

  const state = {
    lastPingMs: null
  };

  async function request(path, options = {}) {
    const base = getApiBase();
    const url = path.startsWith('http') ? path : `${base}${path}`;
    const headers = Object.assign({ Accept: 'application/json' }, options.headers || {});
    if (options.body && !headers['Content-Type'] && !(options.body instanceof FormData)) {
      headers['Content-Type'] = 'application/json';
    }
    const token = getToken();
    if (token && !headers.Authorization) {
      headers.Authorization = `Bearer ${token}`;
    }

    const t0 = performance.now();
    const res = await fetch(url, Object.assign({}, options, { headers }));
    state.lastPingMs = Math.max(1, Math.round(performance.now() - t0));

    const contentType = res.headers.get('content-type') || '';
    const data = contentType.includes('application/json') ? await res.json() : await res.text();
    if (!res.ok) {
      const msg = (data && (data.detail || data.message || data.error)) || `HTTP ${res.status}`;
      const err = new Error(msg);
      err.status = res.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  function postJson(path, bodyObj) {
    return request(path, {
      method: 'POST',
      body: JSON.stringify(bodyObj || {})
    });
  }

  function putJson(path, bodyObj) {
    return request(path, {
      method: 'PUT',
      body: JSON.stringify(bodyObj || {})
    });
  }

  // Formatters
  function fmtNum(val, decimals = 2) {
    if (val === null || val === undefined || Number.isNaN(Number(val))) return '--';
    return Number(val).toLocaleString('en-IN', {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals
    });
  }

  function fmtINR(val, decimals = 2) {
    if (val === null || val === undefined || Number.isNaN(Number(val))) return '₹--';
    const num = Number(val);
    const absStr = Math.abs(num).toLocaleString('en-IN', {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals
    });
    return num < 0 ? `-₹${absStr}` : `₹${absStr}`;
  }

  function fmtSignedINR(val, decimals = 2) {
    if (val === null || val === undefined || Number.isNaN(Number(val))) return '₹--';
    const num = Number(val);
    const absStr = Math.abs(num).toLocaleString('en-IN', {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals
    });
    if (num > 0) return `+₹${absStr}`;
    if (num < 0) return `-₹${absStr}`;
    return `₹${absStr}`;
  }

  function escapeHtml(str) {
    return String(str ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function buildSparklineSvg(points, orh, orl, isBullish) {
    const pts = (points && points.length >= 2) ? points : [100, 100.5, 100.2, 101, 100.8, 101.4];
    const allVals = pts.slice();
    if (orh) allVals.push(Number(orh));
    if (orl) allVals.push(Number(orl));
    const min = Math.min(...allVals);
    const max = Math.max(...allVals);
    const span = max - min || 1;
    const w = 300;
    const h = 44;
    const pad = 4;
    const mapY = (v) => (h - pad) - ((v - min) / span) * (h - pad * 2);

    const coords = pts.map((v, i) => {
      const x = (i / Math.max(1, pts.length - 1)) * w;
      return `${x.toFixed(1)},${mapY(v).toFixed(1)}`;
    });
    const pathD = 'M' + coords.join(' L');
    const areaD = `${pathD} L${w},${h} L0,${h} Z`;
    const color = isBullish ? 'var(--c-profit)' : 'var(--c-loss)';
    const orhLine = orh ? `<line x1="0" y1="${mapY(orh).toFixed(1)}" x2="${w}" y2="${mapY(orh).toFixed(1)}" stroke="rgba(0,245,212,0.45)" stroke-dasharray="4 3" stroke-width="1"/>` : '';
    const orlLine = orl ? `<line x1="0" y1="${mapY(orl).toFixed(1)}" x2="${w}" y2="${mapY(orl).toFixed(1)}" stroke="rgba(255,51,102,0.45)" stroke-dasharray="4 3" stroke-width="1"/>` : '';

    return `<svg class="w-full h-full" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" fill="none">
      ${orhLine}${orlLine}
      <path d="${areaD}" fill="${color}" fill-opacity="0.10"></path>
      <path d="${pathD}" stroke="${color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"></path>
    </svg>`;
  }

  function buildEquityCurveSvg(equityPoints) {
    const pts = (equityPoints && equityPoints.length >= 2) ? equityPoints : [0, 0];
    const min = Math.min(...pts);
    const max = Math.max(...pts);
    const span = max - min || 1;
    const w = 700;
    const h = 160;
    const pad = 10;
    const coords = pts.map((v, i) => {
      const x = (i / Math.max(1, pts.length - 1)) * w;
      const y = (h - pad) - ((v - min) / span) * (h - pad * 2);
      return [x, y];
    });
    const polyline = coords.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(' ');
    const area = `M0,${h} L${coords.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(' L')} L${w},${h} Z`;
    const baseY = (h - pad) - ((pts[0] - min) / span) * (h - pad * 2);
    const last = coords[coords.length - 1];
    return `<svg class="w-full h-44 z-10" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" fill="none">
      <defs>
        <linearGradient id="alphaCurveGrad" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#00f5d4" stop-opacity="0.34"/>
          <stop offset="100%" stop-color="#00f5d4" stop-opacity="0.0"/>
        </linearGradient>
      </defs>
      <line x1="0" y1="${baseY.toFixed(1)}" x2="${w}" y2="${baseY.toFixed(1)}" stroke="rgba(131,148,143,0.35)" stroke-dasharray="4 4" stroke-width="1"/>
      <path d="${area}" fill="url(#alphaCurveGrad)"/>
      <polyline points="${polyline}" stroke="var(--c-profit)" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>
      <circle cx="${last[0].toFixed(1)}" cy="${last[1].toFixed(1)}" r="4.5" fill="var(--c-profit)"/>
    </svg>`;
  }

  let toastTimer = null;
  function showToast(msg, type = 'info') {
    const el = document.getElementById('global-toast');
    if (!el) return;
    const textEl = document.getElementById('global-toast-text');
    const iconEl = document.getElementById('global-toast-icon');
    if (textEl) textEl.textContent = msg;
    if (iconEl) {
      iconEl.textContent = type === 'error' ? 'error' : type === 'warn' ? 'warning' : 'check_circle';
      iconEl.className = `material-symbols-outlined text-[18px] ${
        type === 'error' ? 'text-loss' : type === 'warn' ? 'text-amber-tactical' : 'text-profit'
      }`;
    }
    el.classList.remove('opacity-0', 'pointer-events-none', 'translate-y-2');
    el.classList.add('opacity-100', 'translate-y-0');
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      el.classList.add('opacity-0', 'pointer-events-none', 'translate-y-2');
      el.classList.remove('opacity-100', 'translate-y-0');
    }, 3400);
  }

  function normalizeLogEntry(entry) {
    if (entry && typeof entry === 'object') {
      const ts = String(entry.timestamp || '').trim();
      const level = String(entry.level || 'INFO').trim().toUpperCase();
      const rawLogger = String(entry.logger || 'engine').trim();
      const logger = rawLogger.replace(/^shoonya_orb\./i, '');
      const message = String(entry.message ?? '');
      const prefix = ts ? `[${ts}] ` : '';
      const raw = `${prefix}[${level}] [${logger}] ${message}`.trim();
      return { timestamp: ts, level, logger, message, raw };
    }
    const raw = String(entry ?? '');
    const m = raw.match(/^\[?(\d{2}:\d{2}:\d{2}|\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\]?\s*\|?\s*([A-Z]+)?\s*\|?\s*(.*)$/);
    if (m) {
      return {
        timestamp: (m[1] || '').trim(),
        level: (m[2] || 'INFO').trim().toUpperCase(),
        logger: '',
        message: (m[3] || raw).trim(),
        raw
      };
    }
    return { timestamp: '', level: 'INFO', logger: '', message: raw, raw };
  }

  function classifyLogLine(entry) {
    const norm = normalizeLogEntry(entry);
    const lvl = norm.level;
    const upper = norm.raw.toUpperCase();
    if (lvl === 'ERROR' || lvl === 'CRITICAL' || upper.includes('ERROR') || upper.includes('CRITICAL') || upper.includes('FAILED') || upper.includes('REJECTED')) return 'ERRORS';
    if (lvl === 'WARNING' || lvl === 'WARN' || upper.includes('WARNING') || upper.includes('WARN') || upper.includes('RETRY')) return 'WARNINGS';
    if (upper.includes('SIGNAL') || upper.includes('ORB LOCKED') || upper.includes('BREAKOUT') || upper.includes('GATES')) return 'SIGNALS';
    if (upper.includes('ORDER') || upper.includes('ENTRY') || upper.includes('TARGET') || upper.includes('SL ') || upper.includes('SQUARE')) return 'ORDERS';
    return 'INFO';
  }

  function renderLogRowHtml(entry, truncate = false) {
    const norm = normalizeLogEntry(entry);
    const cat = classifyLogLine(norm);
    const msgClr = cat === 'ERRORS'
      ? 'text-loss'
      : cat === 'WARNINGS'
        ? 'text-amber-tactical'
        : (cat === 'SIGNALS' || cat === 'ORDERS')
          ? 'text-profit font-medium'
          : 'text-on-surface';
    const badgeClr = cat === 'ERRORS'
      ? 'text-loss bg-error-container/25'
      : cat === 'WARNINGS'
        ? 'text-amber-tactical bg-tertiary-container/15'
        : (cat === 'SIGNALS' || cat === 'ORDERS')
          ? 'text-profit bg-primary-container/15'
          : 'text-on-surface-variant bg-surface-container';
    const tsPart = norm.timestamp
      ? `<span class="text-on-surface-variant opacity-70 shrink-0 tabular-nums">[${escapeHtml(norm.timestamp)}]</span>`
      : '';
    const badgeText = norm.logger ? norm.logger.toUpperCase() : norm.level;
    const badgePart = badgeText
      ? `<span class="${badgeClr} px-1.5 py-0.5 rounded text-[10px] font-semibold shrink-0">[${escapeHtml(badgeText)}]</span>`
      : '';
    const msgCls = truncate ? `${msgClr} truncate` : `${msgClr} break-words min-w-0 flex-1`;
    return `<div class="flex items-start gap-1.5 py-0.5 leading-relaxed">${tsPart}${badgePart}<span class="${msgCls}">${escapeHtml(norm.message)}</span></div>`;
  }

  function formatLogsForCopy(entries) {
    return (entries || []).map(e => normalizeLogEntry(e).raw).join('\n');
  }

  setTheme(getTheme());

  global.AutoORB = {
    DEFAULT_ANDROID_API_BASE,
    isNativeCapacitor,
    getApiBase,
    setApiBase,
    getToken,
    setToken,
    getTheme,
    setTheme,
    toggleTheme,
    state,
    request,
    postJson,
    putJson,
    fmtNum,
    fmtINR,
    fmtSignedINR,
    escapeHtml,
    buildSparklineSvg,
    buildEquityCurveSvg,
    showToast,
    normalizeLogEntry,
    classifyLogLine,
    renderLogRowHtml,
    formatLogsForCopy,
    api: {
      getStatus: () => request('/api/status'),
      getLogs: (limit = 150) => request(`/api/logs?limit=${limit}`),
      getBacktestTrades: () => request('/api/backtest/trades'),
      getPublicConfig: () => request('/api/config/public'),
      health: () => request('/health'),
      toggleMode: () => postJson('/api/toggle-mode'),
      killSwitch: () => postJson('/api/kill-switch'),
      oauthLogin: () => postJson('/api/oauth-login'),
      refreshBroker: () => postJson('/api/broker/refresh'),
      connectBrokerLegacy: (payload) => postJson('/api/broker/connect', payload),
      brokerFunds: (amount, action) => postJson('/api/broker/funds', { amount, action }),
      placeManualOrder: (payload) => postJson('/api/broker/order', payload),
      setOrbAnchor: (start_time, minutes) => postJson('/api/settings/orb-anchor', { start_time, minutes }),
      setOrbWindow: (minutes) => postJson('/api/settings/orb-window', { minutes }),
      setEntryCutoff: (cutoff_time) => postJson('/api/settings/entry-cutoff', { cutoff_time }),
      setLots: (index_name, lots) => postJson('/api/settings/lots', { index_name, lots }),
      setProductType: (index_name, product_type) => postJson('/api/settings/product-type', { index_name, product_type }),
      autoTrigger: (index_name, signal_type = 'AUTO') => postJson('/api/strategy/auto-trigger', { index_name, signal_type }),
      exitPosition: (index_name) => postJson('/api/positions/exit', { index_name }),
      modifyPositionSl: (index_name, sl_trigger_price) => postJson('/api/positions/modify-sl', { index_name, sl_trigger_price }),
      resetTrades: () => postJson('/api/trades/reset'),
      // Multi-user Auth & Config v1
      register: (email, password, name) => postJson('/api/v1/auth/register', { email, password, name }),
      login: (email, password) => postJson('/api/v1/auth/login', { email, password }),
      googleAuth: (payload) => postJson('/api/v1/auth/google', payload),
      facebookAuth: (payload) => postJson('/api/v1/auth/facebook', payload),
      firebaseAuth: (id_token, provider = 'firebase') => postJson('/api/v1/auth/firebase', { id_token, provider }),
      sendPhoneOtp: (phone) => postJson('/api/v1/auth/phone/send-otp', { phone }),
      verifyPhoneOtp: (phone, otp, name) => postJson('/api/v1/auth/phone/verify-otp', { phone, otp, name }),
      getMe: () => request('/api/v1/auth/me'),
      logout: () => postJson('/api/v1/auth/logout', {}),
      connectBrokerV1: (payload) => postJson('/api/v1/broker/connect', payload),
      toggleTradingV1: (enabled) => postJson('/api/v1/broker/toggle-trading', { enabled }),
      disconnectBrokerV1: () => postJson('/api/v1/broker/disconnect', {}),
      getRiskV1: () => request('/api/v1/risk/settings'),
      updateRiskV1: (payload) => putJson('/api/v1/risk/settings', payload),
      getTradesV1: (limit = 50) => request(`/api/v1/trades/my-trades?limit=${limit}`),
      getTradeStatsV1: () => request('/api/v1/trades/stats')
    }
  };
})(window);
