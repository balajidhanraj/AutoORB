"""Hugging Face Spaces & standalone launcher for Shoonya Automated ORB Terminal."""

from __future__ import annotations

try:
    import spaces

    @spaces.GPU(duration=1)
    def _noop() -> str:
        """No-op placeholder required by ZeroGPU runtime; never called."""
        return "ok"
except ImportError:
    def _noop() -> str:
        """Fallback no-op when running outside ZeroGPU."""
        return "ok"

import html
import os
from typing import Any, Dict, Iterable, Optional, Tuple

import uvicorn

from config.settings import load_settings
from main import TradingSystemOrchestrator
from ui.dashboard import create_app

try:
    import gradio as gr
except ImportError:
    gr = None  # type: ignore[assignment]


settings = load_settings()
settings.ui_port = int(
    os.getenv("UI_PORT") or os.getenv("PORT") or os.getenv("GRADIO_SERVER_PORT") or 7860
)
orchestrator = TradingSystemOrchestrator(settings=settings)
orchestrator.start_background_loop()

THEMES: Dict[str, Dict[str, str]] = {
    "midnight": {
        "bg": "#070b14",
        "surface": "#0f172a",
        "alt": "#1e293b",
        "border": "#334155",
        "text": "#f1f5f9",
        "muted": "#94a3b8",
        "green": "#10b981",
        "red": "#ef4444",
        "amber": "#f59e0b",
        "accent": "#3b82f6",
        "accent_soft": "rgba(59,130,246,0.16)",
        "log_bg": "#04070e",
    },
    "emerald": {
        "bg": "#040d0a",
        "surface": "#091914",
        "alt": "#112a21",
        "border": "#1e4d3c",
        "text": "#ecfdf5",
        "muted": "#6ee7b7",
        "green": "#34d399",
        "red": "#fb7185",
        "amber": "#fbbf24",
        "accent": "#10b981",
        "accent_soft": "rgba(16,185,129,0.18)",
        "log_bg": "#020806",
    },
    "gold": {
        "bg": "#0c0a09",
        "surface": "#181411",
        "alt": "#27201a",
        "border": "#44372b",
        "text": "#fafaf9",
        "muted": "#d6c3a5",
        "green": "#22c55e",
        "red": "#ef4444",
        "amber": "#f59e0b",
        "accent": "#eab308",
        "accent_soft": "rgba(234,179,8,0.16)",
        "log_bg": "#070504",
    },
    "light": {
        "bg": "#f1f5f9",
        "surface": "#ffffff",
        "alt": "#f8fafc",
        "border": "#cbd5e1",
        "text": "#0f172a",
        "muted": "#475569",
        "green": "#059669",
        "red": "#dc2626",
        "amber": "#d97706",
        "accent": "#2563eb",
        "accent_soft": "rgba(37,99,235,0.12)",
        "log_bg": "#0f172a",
    },
    "nordic": {
        "bg": "#0f141d",
        "surface": "#17202e",
        "alt": "#222e42",
        "border": "#384a66",
        "text": "#e2e8f0",
        "muted": "#93c5fd",
        "green": "#2dd4bf",
        "red": "#f87171",
        "amber": "#fbbf24",
        "accent": "#38bdf8",
        "accent_soft": "rgba(56,189,248,0.16)",
        "log_bg": "#090d14",
    },
}

_current_theme: str = "midnight"


def _fmt_num(value: Any, digits: int = 2, prefix: str = "", suffix: str = "") -> str:
    if value is None:
        return "--"
    try:
        return f"{prefix}{float(value):.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return "--"


def _badge(text: str, tone: str, p: Dict[str, str]) -> str:
    colors = {
        "live": (p["green"], p["alt"]),
        "paper": (p["amber"], p["alt"]),
        "danger": (p["red"], p["alt"]),
        "info": (p["accent"], p["accent_soft"]),
        "neutral": (p["muted"], p["alt"]),
    }
    fg, bg = colors.get(tone, colors["neutral"])
    return (
        f'<span style="display:inline-block;padding:4px 10px;border-radius:999px;'
        f'font-size:11px;font-weight:600;color:{fg};background:{bg};'
        f'border:1px solid {fg};margin-right:6px;margin-bottom:4px;">{html.escape(text)}</span>'
    )


def _table_html(headers: Iterable[str], rows_html: str, empty_msg: str, p: Dict[str, str]) -> str:
    if not rows_html:
        return f'<p style="color:{p["muted"]} !important;font-style:italic;margin:8px 0;font-size:12px;">{html.escape(empty_msg)}</p>'
    ths = "".join(
        f'<th style="text-align:left;padding:7px 6px;border-bottom:1px solid {p["border"]};'
        f'color:{p["muted"]} !important;font-size:11px;text-transform:uppercase;">{html.escape(h)}</th>'
        for h in headers
    )
    return (
        '<div style="overflow-x:auto;">'
        f'<table style="width:100%;border-collapse:collapse;font-size:12px;color:{p["text"]} !important;">'
        f"<thead><tr>{ths}</tr></thead><tbody>{rows_html}</tbody></table></div>"
    )


def _render_radar_card(
    name: str,
    st: Dict[str, Any],
    p: Dict[str, str],
    orb_start: str = "09:15",
    orb_end: str = "09:30",
) -> str:
    r = st.get("radar") or {}
    if st.get("no_trade_reason"):
        status = _badge(str(st["no_trade_reason"]), "danger", p)
    elif st.get("is_locked"):
        status = _badge("ORB LOCKED", "live", p)
    else:
        status = _badge(f"WAITING {orb_end}", "neutral", p)

    kvs = [
        ("Spot LTP", _fmt_num(st.get("spot_ltp"))),
        (f"ORH ({orb_start}-{orb_end})", _fmt_num(st.get("orh"))),
        (f"ORL ({orb_start}-{orb_end})", _fmt_num(st.get("orl"))),
        ("Range %", _fmt_num(st.get("range_pct"), suffix="%")),
        ("Intraday VWAP", _fmt_num(st.get("vwap"))),
        ("Wilder RSI(14)", _fmt_num(st.get("rsi"))),
    ]
    kv_blocks = "".join(
        f'<div style="background:{p["alt"]};border:1px solid {p["border"]};padding:8px;border-radius:8px;">'
        f'<div style="color:{p["muted"]};font-size:10px;">{html.escape(label)}</div>'
        f'<div style="color:{p["text"]};font-size:14px;font-weight:700;">{html.escape(val)}</div></div>'
        for label, val in kvs
    )

    gates = [
        ("Sys/Slot", bool(r.get("system_ready"))),
        ("VIX>=12", bool(r.get("vix_ready"))),
        ("ORB<=0.75%", bool(r.get("orb_ready"))),
        ("Break+VWAP", bool(r.get("ce_price_ok") or r.get("pe_price_ok"))),
        ("RSI>60/<40", bool(r.get("ce_rsi_ok") or r.get("pe_rsi_ok"))),
    ]
    gate_chips = "".join(
        f'<span style="font-size:11px;font-weight:600;padding:3px 7px;border-radius:6px;'
        f'border:1px solid {p["green"] if ok else p["border"]};'
        f'background:{p["alt"]};color:{p["green"] if ok else p["muted"]};margin:2px;display:inline-block;">'
        f'{"✓" if ok else "○"} {lbl}</span>'
        for lbl, ok in gates
    )
    pct = int(r.get("readiness_pct", 0) or 0)
    is_imm = r.get("radar_state") in {"IMMINENT_CE_BREAKOUT", "IMMINENT_PE_BREAKOUT"}
    banner_bg = "rgba(16,185,129,0.16)" if is_imm else p["accent_soft"]
    banner_border = p["green"] if is_imm else p["accent"]
    banner_fg = p["green"] if is_imm else p["text"]

    return (
        f'<div style="background:{p["surface"]};border:1px solid {p["border"]};border-radius:14px;padding:15px;color:{p["text"]} !important;">'
        f'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">'
        f'<strong style="font-size:14px;color:{p["text"]} !important;">{html.escape(name)} Spot &amp; Auto-Order Radar</strong>{status}</div>'
        f'<div style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-bottom:10px;">{kv_blocks}</div>'
        f'<div style="display:flex;justify-content:space-between;font-size:11px;color:{p["muted"]} !important;">'
        f'<span style="color:{p["muted"]} !important;">Auto-Trigger Readiness</span><strong style="color:{p["text"]} !important;">{pct}%</strong></div>'
        f'<div style="background:{p["alt"]};border:1px solid {p["border"]};border-radius:999px;height:8px;overflow:hidden;margin:6px 0 8px;">'
        f'<div style="width:{pct}%;height:100%;background:{p["accent"]};"></div></div>'
        f'<div style="margin-bottom:8px;">{gate_chips}</div>'
        f'<div style="font-size:11px;color:{p["muted"]} !important;margin-bottom:6px;">Candidate: '
        f'<strong style="color:{p["text"]} !important;">{html.escape(str(r.get("candidate_ce", "--")))}</strong> | '
        f'<strong style="color:{p["text"]} !important;">{html.escape(str(r.get("candidate_pe", "--")))}</strong></div>'
        f'<div style="font-size:12px;padding:8px 10px;border-radius:8px;background:{banner_bg};'
        f'border:1px solid {banner_border};color:{banner_fg} !important;font-weight:{"600" if is_imm else "400"};">'
        f'{html.escape(str(r.get("radar_summary", "")))}</div></div>'
    )


def _render_broker_and_schedule_col(snap: Dict[str, Any], p: Dict[str, str]) -> str:
    brk = snap.get("broker") or {}
    sched = snap.get("schedule") or {}
    connected = bool(brk.get("connected"))
    conn_badge = _badge(
        f"CONNECTED ({brk.get('user_id') or snap.get('user_id') or '--'})" if connected else "NOT CONNECTED",
        "live" if connected else "danger",
        p,
    )
    client_label = " / ".join(
        part
        for part in [
            str(brk.get("user_id") or snap.get("user_id") or "--"),
            str(brk.get("profile_name") or ""),
            str(brk.get("account_name") or ""),
        ]
        if part
    )
    fields = [
        ("Client / Profile", client_label or "--"),
        ("Broker Cash / Margin Used", f"₹{_fmt_num(brk.get('cash', 0.0))} / ₹{_fmt_num(brk.get('margin_used', 0.0))}"),
        ("Payin / Collateral", f"₹{_fmt_num(brk.get('payin', 0.0))} / ₹{_fmt_num(brk.get('collateral', 0.0))}"),
        ("Net Available Margin", f"₹{_fmt_num(brk.get('net_available', 0.0))}"),
    ]
    kv_cards = "".join(
        f'<div style="background:{p["alt"]};border:1px solid {p["border"]};padding:8px;border-radius:8px;">'
        f'<div style="color:{p["muted"]};font-size:10px;">{html.escape(label)}</div>'
        f'<div style="color:{p["text"]};font-size:13px;font-weight:700;">{html.escape(val)}</div></div>'
        for label, val in fields
    )
    ms_rows = "".join(
        f'<div style="display:flex;justify-content:space-between;align-items:center;padding:5px 9px;'
        f'border-radius:8px;background:{p["alt"]};border:1px solid {p["border"]};margin-top:5px;font-size:11px;color:{p["text"]} !important;">'
        f'<span style="color:{p["text"]} !important;"><strong style="color:{p["text"]} !important;">{html.escape(str(m.get("time", "")))}</strong> — {html.escape(str(m.get("label", "")))}</span>'
        f'<span style="font-weight:700;color:{p["green"] if m.get("status") == "DONE" else p["amber"] if m.get("status") == "ACTIVE" else p["muted"]} !important;">'
        f'{html.escape(str(m.get("status", "")))}</span></div>'
        for m in sched.get("milestones", [])
    )
    links = (
        '<div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap;">'
        '<a href="https://prism.shoonya.com/" target="_blank" rel="noopener" '
        'style="padding:5px 10px;border-radius:7px;background:#059669;color:#ffffff !important;text-decoration:none;font-size:11px;font-weight:600;">+ Add Real Funds</a>'
        '<a href="https://trade.shoonya.com/" target="_blank" rel="noopener" '
        f'style="padding:5px 10px;border-radius:7px;background:{p["alt"]};border:1px solid {p["border"]};color:{p["text"]} !important;text-decoration:none;font-size:11px;font-weight:600;">Shoonya Web</a>'
        "</div>"
    )
    return (
        f'<div style="background:{p["surface"]};border:1px solid {p["border"]};border-radius:14px;padding:15px;color:{p["text"]} !important;">'
        f'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">'
        f'<strong style="font-size:14px;color:{p["text"]} !important;">Broker Funds &amp; Auto-Schedule</strong>{conn_badge}</div>'
        f'<div style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;">{kv_cards}</div>'
        f"{links}"
        f'<div style="font-weight:700;font-size:12px;margin-top:12px;color:{p["text"]} !important;">Automated Daily Timeline</div>'
        f'<div style="font-size:11px;color:{p["muted"]} !important;margin-top:2px;">{html.escape(str(sched.get("auto_mode_note", "")))}</div>'
        f"{ms_rows}</div>"
    )


def render_dashboard_html(theme_name: Optional[str] = None) -> str:
    """Render multi-column dark/light themed trading terminal snapshot."""
    chosen = theme_name if theme_name in THEMES else _current_theme
    p = THEMES.get(chosen, THEMES["midnight"])
    snap = orchestrator.get_status_snapshot()
    sched = snap.get("schedule") or {}
    brk = snap.get("broker") or {}

    mode = str(snap.get("mode", "PAPER"))
    mtm = float(snap.get("mtm_pnl", 0.0) or 0.0)
    halted = bool(snap.get("halted"))
    vix = snap.get("india_vix")
    countdown = (
        f"{sched['next_candle_close_sec']}s"
        if sched.get("next_candle_close_sec")
        else ("WEEKEND" if sched.get("phase") == "WEEKEND_STANDBY" else "CLOSED")
    )

    badges_html = "".join(
        [
            _badge(f"Client: {snap.get('user_id') or '--'}", "info", p),
            _badge(f"MODE: {mode}", "live" if mode == "LIVE" else "paper", p),
            _badge(f"OAuth: {snap.get('oauth_status', '--')}", "info", p),
            _badge(f"WS: {'ONLINE' if snap.get('ws_connected') else 'OFFLINE'}", "live" if snap.get("ws_connected") else "neutral", p),
            _badge(f"IP: {snap.get('public_ip', 'unknown')}", "info", p),
            _badge(f"{sched.get('phase', '--')}", "info", p),
            _badge(f"IST: {snap.get('ist_time', '--')}", "neutral", p),
        ]
    )

    kpis = [
        ("Live Total MTM", f"₹{mtm:.2f}", p["green"] if mtm >= 0 else p["red"], f"Max Loss: -₹{snap.get('max_daily_loss', 5000):.0f}"),
        ("Broker Cash", f"₹{_fmt_num(brk.get('cash', 0.0))}", p["text"], f"Net Avail: ₹{_fmt_num(brk.get('net_available', 0.0))}"),
        ("Strategy Capital", f"₹{_fmt_num(snap.get('capital', 0.0))}", p["text"], "NIFTY 130 | BNF 60"),
        ("Trades Today", f"{snap.get('trades_taken', 0)} / {snap.get('max_trades', 2)}", p["text"], "Entry cutoff: 14:15"),
        ("India VIX", _fmt_num(vix), p["text"], f"Contracts: {snap.get('cached_contracts', 0)}"),
        ("Next 5m Eval", countdown, p["red"] if halted else p["green"], str(snap.get("halt_reason") or "HALTED") if halted else "Engine: ACTIVE"),
    ]
    kpi_html = "".join(
        f'<div style="background:{p["surface"]};border:1px solid {p["border"]};border-radius:12px;padding:12px;">'
        f'<div style="color:{p["muted"]};font-size:10px;text-transform:uppercase;letter-spacing:0.05em;">{html.escape(title)}</div>'
        f'<div style="color:{color};font-size:20px;font-weight:700;margin-top:3px;">{html.escape(value)}</div>'
        f'<div style="color:{p["muted"]};font-size:11px;margin-top:2px;">{html.escape(sub)}</div></div>'
        for title, value, color, sub in kpis
    )

    indices = snap.get("indices", {})
    orb_start = str(snap.get("orb_start_time") or "09:15")
    orb_end = str(snap.get("orb_end_time") or "09:30")
    col3_html = (
        f'<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px;margin-bottom:14px;">'
        f'{_render_radar_card("NIFTY", indices.get("NIFTY", {}), p, orb_start=orb_start, orb_end=orb_end)}'
        f'{_render_radar_card("BANKNIFTY", indices.get("BANKNIFTY", {}), p, orb_start=orb_start, orb_end=orb_end)}'
        f'{_render_radar_card("CRUDEOILM", indices.get("CRUDEOILM", {}), p, orb_start=orb_start, orb_end=orb_end)}'
        f"{_render_broker_and_schedule_col(snap, p)}"
        f"</div>"
    )

    pos_rows = "".join(
        f'<tr style="color:{p["text"]} !important;">'
        f'<td style="padding:7px;border-bottom:1px solid {p["border"]};color:{p["text"]} !important;"><strong style="color:{p["text"]} !important;">{html.escape(str(x.get("tradingsymbol", "")))}</strong></td>'
        f'<td style="padding:7px;border-bottom:1px solid {p["border"]};color:{p["text"]} !important;">{html.escape(str(x.get("state", "")))}</td>'
        f'<td style="padding:7px;border-bottom:1px solid {p["border"]};color:{p["text"]} !important;">{x.get("remaining_qty", 0)}/{x.get("initial_qty", 0)}</td>'
        f'<td style="padding:7px;border-bottom:1px solid {p["border"]};color:{p["text"]} !important;">₹{_fmt_num(x.get("entry_avg_price"))}</td>'
        f'<td style="padding:7px;border-bottom:1px solid {p["border"]};color:{p["text"]} !important;">₹{_fmt_num(x.get("sl_trigger_price"))}</td>'
        f'<td style="padding:7px;border-bottom:1px solid {p["border"]};color:{p["text"]} !important;">₹{_fmt_num((x.get("unrealized_pnl", 0) or 0) + (x.get("realized_pnl", 0) or 0))}</td>'
        f"</tr>"
        for x in snap.get("positions", [])
    )
    pos_table = _table_html(["Contract", "State", "Qty", "Entry", "SL", "P&L"], pos_rows, "No active strategy positions.", p)

    trade_rows = "".join(
        f'<tr style="color:{p["text"]} !important;">'
        f'<td style="padding:7px;border-bottom:1px solid {p["border"]};color:{p["text"]} !important;">{html.escape(str(t.get("tradingsymbol", "")))}</td>'
        f'<td style="padding:7px;border-bottom:1px solid {p["border"]};color:{p["text"]} !important;">{html.escape(str(t.get("signal_type", "")))}</td>'
        f'<td style="padding:7px;border-bottom:1px solid {p["border"]};color:{p["text"]} !important;">{t.get("quantity", 0)}</td>'
        f'<td style="padding:7px;border-bottom:1px solid {p["border"]};color:{p["text"]} !important;">{html.escape(str(t.get("exit_reason", "")))}</td>'
        f'<td style="padding:7px;border-bottom:1px solid {p["border"]};color:{p["text"]} !important;">₹{_fmt_num(t.get("net_pnl"))}</td>'
        f"</tr>"
        for t in snap.get("completed_trades", [])
    )
    trades_table = _table_html(["Contract", "Side", "Qty", "Reason", "Net P&L"], trade_rows, "No completed strategy trades yet.", p)

    ord_rows = "".join(
        f'<tr style="color:{p["text"]} !important;">'
        f'<td style="padding:7px;border-bottom:1px solid {p["border"]};color:{p["text"]} !important;"><strong style="color:{p["text"]} !important;">{html.escape(str(o.get("norenordno", "")))}</strong></td>'
        f'<td style="padding:7px;border-bottom:1px solid {p["border"]};color:{p["text"]} !important;">{html.escape(str(o.get("exch", "")))}:{html.escape(str(o.get("tsym", "")))}</td>'
        f'<td style="padding:7px;border-bottom:1px solid {p["border"]};color:{p["text"]} !important;">{html.escape(str(o.get("trantype", "")))} ({html.escape(str(o.get("prd", "")))})</td>'
        f'<td style="padding:7px;border-bottom:1px solid {p["border"]};color:{p["text"]} !important;">{html.escape(str(o.get("fillshares", "0")))}/{html.escape(str(o.get("qty", "0")))}</td>'
        f'<td style="padding:7px;border-bottom:1px solid {p["border"]};color:{p["text"]} !important;">₹{html.escape(str(o.get("prc", "0")))}</td>'
        f'<td style="padding:7px;border-bottom:1px solid {p["border"]};color:{p["green"] if str(o.get("status", "")).upper() == "COMPLETE" else p["red"] if str(o.get("status", "")).upper() in ("REJECTED", "CANCELED") else p["amber"]} !important;font-weight:700;">{html.escape(str(o.get("status", "")))}</td>'
        f'<td style="padding:7px;border-bottom:1px solid {p["border"]};color:{p["text"]} !important;">{html.escape(str(o.get("reason", "")))}</td>'
        f"</tr>"
        for o in brk.get("orders", [])
    )
    brk_orders_table = _table_html(
        ["Order No", "Symbol", "Side", "Qty", "Price", "Status", "Reason"],
        ord_rows,
        str(brk.get("error") or "No broker orders yet."),
        p,
    )

    brk_pos_rows = "".join(
        f'<tr style="color:{p["text"]} !important;">'
        f'<td style="padding:7px;border-bottom:1px solid {p["border"]};color:{p["text"]} !important;"><strong style="color:{p["text"]} !important;">{html.escape(str(x.get("exch", "")))}:{html.escape(str(x.get("tsym", "")))}</strong></td>'
        f'<td style="padding:7px;border-bottom:1px solid {p["border"]};color:{p["text"]} !important;">{html.escape(str(x.get("prd", "")))}</td>'
        f'<td style="padding:7px;border-bottom:1px solid {p["border"]};color:{p["text"]} !important;">{html.escape(str(x.get("netqty", "0")))}</td>'
        f'<td style="padding:7px;border-bottom:1px solid {p["border"]};color:{p["text"]} !important;">₹{html.escape(str(x.get("netavgprc", "0")))}</td>'
        f'<td style="padding:7px;border-bottom:1px solid {p["border"]};color:{p["text"]} !important;">₹{html.escape(str(x.get("urmtom", "0")))}</td>'
        f"</tr>"
        for x in brk.get("positions", [])
    )
    brk_pos_table = _table_html(["Symbol", "Prod", "Qty", "Avg", "MTM"], brk_pos_rows, "No open broker positions in Shoonya.", p)

    col2_books = (
        f'<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(380px,1fr));gap:14px;margin-bottom:14px;">'
        f'<div style="display:flex;flex-direction:column;gap:14px;">'
        f'<div style="background:{p["surface"]};border:1px solid {p["border"]};border-radius:14px;padding:15px;color:{p["text"]} !important;">'
        f'<div style="font-weight:700;margin-bottom:8px;color:{p["text"]} !important;">Active Strategy Bracket Positions</div>{pos_table}</div>'
        f'<div style="background:{p["surface"]};border:1px solid {p["border"]};border-radius:14px;padding:15px;color:{p["text"]} !important;">'
        f'<div style="font-weight:700;margin-bottom:8px;color:{p["text"]} !important;">Completed Strategy Trades Today</div>{trades_table}</div></div>'
        f'<div style="display:flex;flex-direction:column;gap:14px;">'
        f'<div style="background:{p["surface"]};border:1px solid {p["border"]};border-radius:14px;padding:15px;color:{p["text"]} !important;">'
        f'<div style="font-weight:700;margin-bottom:8px;color:{p["text"]} !important;">Live Shoonya Broker Order Book (OrderBook)</div>{brk_orders_table}</div>'
        f'<div style="background:{p["surface"]};border:1px solid {p["border"]};border-radius:14px;padding:15px;color:{p["text"]} !important;">'
        f'<div style="font-weight:700;margin-bottom:8px;color:{p["text"]} !important;">Live Shoonya Broker Positions (PositionBook)</div>{brk_pos_table}</div></div>'
        f"</div>"
    )

    log_lines = "".join(
        f'<div style="margin-bottom:3px;color:{"#ef4444" if l.get("level") in ("ERROR", "CRITICAL") else "#f59e0b" if "WARN" in str(l.get("level", "")) else "#d1d5db"} !important;">'
        f"[{html.escape(str(l.get('timestamp', '')))}] {html.escape(str(l.get('level', '')))} | "
        f"{html.escape(str(l.get('logger', '')))} | {html.escape(str(l.get('message', '')))}</div>"
        for l in snap.get("logs", [])[-50:]
    ) or '<div style="color:#9ca3af !important;">Waiting for engine logs...</div>'

    reset_css = (
        "<style>"
        ".orb-root{color:var(--orb-text) !important;}"
        ".orb-root strong,.orb-root b{color:var(--orb-text) !important;}"
        ".orb-root th{color:var(--orb-muted) !important;}"
        ".orb-root td,.orb-root p{color:inherit;}"
        "</style>"
    )

    return (
        f'<div class="orb-root not-prose" style="--orb-text:{p["text"]};--orb-muted:{p["muted"]};background:{p["bg"]};color:{p["text"]} !important;padding:16px;border-radius:14px;'
        'font-family:Inter,-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;">'
        f"{reset_css}"
        f'<div style="background:{p["surface"]};border:1px solid {p["border"]};border-radius:14px;padding:14px 16px;margin-bottom:14px;color:{p["text"]} !important;">'
        f'<div style="font-size:18px;font-weight:700;margin-bottom:4px;color:{p["text"]} !important;">Shoonya Automated ORB Terminal '
        f'<span style="font-size:12px;color:{p["muted"]} !important;font-weight:500;">(Theme: {html.escape(chosen)})</span></div>'
        f'<div style="font-size:12px;color:{p["muted"]} !important;margin-bottom:8px;">{snap.get("orb_window_minutes", 15)}-Min ORB (09:15-{snap.get("orb_end_time", "09:30")}) + Intraday VWAP + Wilder RSI(14) | Algo ID: {html.escape(str(snap.get("sebi_algo_id", "--")))}</div>'
        f"<div>{badges_html}</div></div>"
        f'<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-bottom:14px;">{kpi_html}</div>'
        f"{col3_html}{col2_books}"
        f'<div style="background:{p["surface"]};border:1px solid {p["border"]};border-radius:14px;padding:15px;color:{p["text"]} !important;">'
        f'<div style="font-weight:700;margin-bottom:8px;color:{p["text"]} !important;">Live Engine &amp; Auto-Execution Logs</div>'
        f'<div style="background:{p["log_bg"]};border:1px solid {p["border"]};border-radius:8px;padding:10px;color:#d1d5db !important;'
        f'max-height:240px;overflow-y:auto;font-family:ui-monospace,Consolas,monospace;font-size:12px;">'
        f"{log_lines}</div></div></div>"
    )


def change_theme_ui(theme_name: str) -> str:
    """Switch the dashboard color theme across Midnight, Emerald, Gold, Light, or Nordic."""
    global _current_theme
    _current_theme = theme_name if theme_name in THEMES else "midnight"
    return render_dashboard_html(_current_theme)


def run_oauth_login() -> Tuple[str, str]:
    """Run pure-Python Shoonya OAuth + TOTP authentication and refresh the dashboard."""
    token = orchestrator.run_daily_login(force_refresh=True)
    status = (
        f"OAuth status: {orchestrator.auth.last_login_status} "
        f"(token_present={bool(token)}, date={orchestrator.auth.last_token_date})"
    )
    return status, render_dashboard_html()


def sync_broker_ui() -> Tuple[str, str]:
    """Force sync of live Shoonya limits, orders, and positions."""
    brk = orchestrator.refresh_broker_data()
    if brk.get("connected"):
        msg = f"Synced live Shoonya account {brk.get('user_id')} (orders={len(brk.get('orders', []))}, cash=₹{brk.get('cash', 0):.2f})."
    else:
        msg = f"Broker offline: {brk.get('error') or 'Click Run OAuth Login or Connect Account first.'}"
    return msg, render_dashboard_html()


def toggle_trading_mode() -> Tuple[str, str]:
    """Toggle trading mode between PAPER and LIVE when no positions are open."""
    _changed, message = orchestrator.toggle_mode()
    return message, render_dashboard_html()


def trigger_kill_switch() -> Tuple[str, str]:
    """Execute the emergency kill switch to flatten positions and halt trading today."""
    orchestrator.trigger_kill_switch(reason="MANUAL_UI_KILL_SWITCH")
    return "Emergency kill switch executed.", render_dashboard_html()


def update_funds_ui(amount: float, action: str) -> Tuple[str, str]:
    """Add to or set strategy capital and sync live Shoonya broker balances."""
    _ok, msg = orchestrator.add_or_update_funds(amount=amount or 0.0, action=action or "add")
    return msg, render_dashboard_html()


def place_broker_order_ui(
    exchange: str,
    tradingsymbol: str,
    buy_or_sell: str,
    product_type: str,
    price_type: str,
    quantity: float,
    price: float,
) -> Tuple[str, str]:
    """Place a live broker order directly on Shoonya and refresh the order book."""
    _ok, msg, _resp = orchestrator.place_manual_broker_order(
        exchange=exchange,
        tradingsymbol=tradingsymbol,
        buy_or_sell=buy_or_sell,
        quantity=int(quantity or 0),
        price_type=price_type,
        price=float(price or 0.0),
        product_type=product_type,
    )
    return msg, render_dashboard_html()


def connect_account_ui(
    user_id: str,
    account_id: str,
    password: str,
    totp_secret: str,
    api_key: str,
    secret_key: str,
    sebi_algo_id: str,
) -> Tuple[str, str]:
    """Connect or switch to any user's Shoonya account at runtime."""
    _ok, msg = orchestrator.connect_shoonya_account(
        user_id=user_id or "",
        password=password or "",
        totp_secret=totp_secret or "",
        secret_key=secret_key or "",
        account_id=account_id or "",
        api_key=api_key or "",
        sebi_algo_id=sebi_algo_id or "",
    )
    return msg, render_dashboard_html()


def change_orb_window_ui(window_choice: str) -> Tuple[str, str]:
    """Change ORB anchor & duration dynamically."""
    mapping = {
        "15m (09:15-09:30 Morning)": ("09:15", 15),
        "5m (09:15-09:20 Fast)": ("09:15", 5),
        "30m (09:15-09:45)": ("09:15", 30),
        "45m (09:15-10:00)": ("09:15", 45),
        "60m (09:15-10:15)": ("09:15", 60),
        "15m (10:00-10:15)": ("10:00", 15),
        "15m (11:00-11:15)": ("11:00", 15),
        "15m (12:00-12:15)": ("12:00", 15),
        "15m (12:30-12:45)": ("12:30", 15),
        "15m (1:00 PM-1:15 PM Afternoon)": ("13:00", 15),
        "15m (1:15 PM-1:30 PM)": ("13:15", 15),
        "15m (1:30 PM-1:45 PM)": ("13:30", 15),
        "15m (2:00 PM-2:15 PM)": ("14:00", 15),
        "Recent 15-Min (Auto Anchor)": ("recent", 15),
        "Recent 5-Min (Immediate Test)": ("recent", 5),
    }
    start_t, mins = mapping.get(window_choice, ("09:15", 15))
    _ok, msg = orchestrator.set_orb_anchor(start_t, mins)
    banner = f"**ORB Window**: {msg}"
    return banner, render_dashboard_html()


def reset_trades_ui() -> Tuple[str, str]:
    _ok, msg = orchestrator.reset_daily_trades()
    banner = f"**Trades Reset**: {msg}"
    return banner, render_dashboard_html()


def change_entry_cutoff_ui(cutoff_choice: str) -> Tuple[str, str]:
    """Change Auto Breakout entry cutoff window (09:30 to 15:15 IST)."""
    _ok, msg = orchestrator.set_entry_cutoff_time(cutoff_choice)
    banner = f"**Entry Cutoff**: {msg}"
    return banner, render_dashboard_html()


def refresh_dashboard() -> str:
    """Return the latest HTML snapshot for periodic Gradio timer updates."""
    return render_dashboard_html()


def build_gradio_demo() -> Any:
    """Build the Gradio Blocks UI for Hugging Face Spaces."""
    if gr is None:
        return None

    with gr.Blocks(title="Shoonya Automated ORB Terminal") as blocks:
        status_banner = gr.Markdown(
            f"Terminal initialized in **{orchestrator.settings.mode}** mode."
        )
        with gr.Row():
            theme_selector = gr.Dropdown(
                choices=["midnight", "emerald", "gold", "light", "nordic"],
                value="midnight",
                label="UI Theme",
            )
            orb_window_selector = gr.Dropdown(
                choices=[
                    "15m (09:15-09:30 Morning)",
                    "5m (09:15-09:20 Fast)",
                    "30m (09:15-09:45)",
                    "45m (09:15-10:00)",
                    "60m (09:15-10:15)",
                    "15m (10:00-10:15)",
                    "15m (11:00-11:15)",
                    "15m (12:00-12:15)",
                    "15m (12:30-12:45)",
                    "15m (1:00 PM-1:15 PM Afternoon)",
                    "15m (1:15 PM-1:30 PM)",
                    "15m (1:30 PM-1:45 PM)",
                    "15m (2:00 PM-2:15 PM)",
                    "Recent 15-Min (Auto Anchor)",
                    "Recent 5-Min (Immediate Test)",
                ],
                value="15m (09:15-09:30 Morning)",
                label="ORB Anchor Window",
            )
            cutoff_selector = gr.Dropdown(
                choices=[
                    "11:30",
                    "12:30",
                    "13:30",
                    "14:00",
                    "14:15",
                    "14:30",
                    "14:45",
                    "15:00",
                    "15:10",
                    "15:15",
                ],
                value=orchestrator.settings.entry_cutoff_time.strftime("%H:%M"),
                label="Entry Cutoff",
            )
            oauth_btn = gr.Button("Run OAuth Login", variant="primary")
            sync_btn = gr.Button("Sync Broker Funds")
            reset_trd_btn = gr.Button("Reset Trades (0/2)")
            mode_btn = gr.Button("Toggle PAPER / LIVE")
            refresh_btn = gr.Button("Refresh Snapshot")
            kill_btn = gr.Button("PANIC KILL SWITCH", variant="stop")

        with gr.Row():
            with gr.Accordion("Add / Update Strategy Capital & Real Funds", open=False):
                gr.Markdown(
                    "[+ Add Real Funds in Shoonya Prism](https://prism.shoonya.com/) | "
                    "[Open Shoonya Web Terminal](https://trade.shoonya.com/)"
                )
                with gr.Row():
                    funds_amt = gr.Number(label="Amount (INR)", value=50000)
                    funds_act = gr.Dropdown(["add", "set"], value="add", label="Action")
                    funds_btn = gr.Button("Update Strategy Capital", variant="primary")

            with gr.Accordion("Place Live Broker Order (Test / Manual)", open=False):
                with gr.Row():
                    ord_exch = gr.Dropdown(["NSE", "NFO", "MCX"], value="NSE", label="Exchange")
                    ord_tsym = gr.Textbox(value="TCS-EQ", label="Symbol")
                    ord_side = gr.Dropdown(["B", "S"], value="B", label="Side")
                    ord_prd = gr.Dropdown(["C", "I", "M"], value="C", label="Prod")
                    ord_prctyp = gr.Dropdown(["LMT", "MKT"], value="LMT", label="Type")
                    ord_qty = gr.Number(value=1, precision=0, label="Qty")
                    ord_prc = gr.Number(value=2407.50, label="Price")
                ord_btn = gr.Button("Send Live Broker Order", variant="primary")

            with gr.Accordion("Connect / Switch Any Shoonya Account", open=False):
                with gr.Row():
                    conn_uid = gr.Textbox(label="Shoonya User ID *", placeholder="e.g. FA53975")
                    conn_actid = gr.Textbox(label="Account ID (optional)")
                    conn_pwd = gr.Textbox(label="Password *", type="password")
                with gr.Row():
                    conn_totp = gr.Textbox(label="TOTP Secret (Base32) *", type="password")
                    conn_apikey = gr.Textbox(label="API Key", placeholder="Default: <UID>_U")
                    conn_skey = gr.Textbox(label="Secret Key (64-char) *", type="password")
                    conn_algoid = gr.Textbox(label="SEBI Algo ID (optional)")
                conn_btn = gr.Button("Connect & Login Shoonya Account", variant="primary")

        dashboard_html = gr.HTML(value=render_dashboard_html)

        theme_selector.change(
            fn=change_theme_ui,
            inputs=[theme_selector],
            outputs=[dashboard_html],
            api_name="change_theme",
        )
        orb_window_selector.change(
            fn=change_orb_window_ui,
            inputs=[orb_window_selector],
            outputs=[status_banner, dashboard_html],
            api_name="change_orb_window",
        )
        cutoff_selector.change(
            fn=change_entry_cutoff_ui,
            inputs=[cutoff_selector],
            outputs=[status_banner, dashboard_html],
            api_name="change_entry_cutoff",
        )
        oauth_btn.click(
            fn=run_oauth_login,
            outputs=[status_banner, dashboard_html],
            api_name="oauth_login",
        )
        sync_btn.click(
            fn=sync_broker_ui,
            outputs=[status_banner, dashboard_html],
            api_name="sync_broker",
        )
        reset_trd_btn.click(
            fn=reset_trades_ui,
            outputs=[status_banner, dashboard_html],
            api_name="reset_trades",
        )
        mode_btn.click(
            fn=toggle_trading_mode,
            outputs=[status_banner, dashboard_html],
            api_name="toggle_mode",
        )
        kill_btn.click(
            fn=trigger_kill_switch,
            outputs=[status_banner, dashboard_html],
            api_name="kill_switch",
        )
        refresh_btn.click(
            fn=refresh_dashboard,
            outputs=[dashboard_html],
            api_name="refresh_dashboard",
        )
        funds_btn.click(
            fn=update_funds_ui,
            inputs=[funds_amt, funds_act],
            outputs=[status_banner, dashboard_html],
            api_name="update_funds",
        )
        ord_btn.click(
            fn=place_broker_order_ui,
            inputs=[ord_exch, ord_tsym, ord_side, ord_prd, ord_prctyp, ord_qty, ord_prc],
            outputs=[status_banner, dashboard_html],
            api_name="place_broker_order",
        )
        conn_btn.click(
            fn=connect_account_ui,
            inputs=[conn_uid, conn_actid, conn_pwd, conn_totp, conn_apikey, conn_skey, conn_algoid],
            outputs=[status_banner, dashboard_html],
            api_name="connect_account",
        )
        gr.Timer(2.0).tick(fn=refresh_dashboard, outputs=[dashboard_html])

    return blocks


demo = build_gradio_demo()


if __name__ == "__main__":
    try:
        if demo is not None:
            server_port = int(
                os.getenv("PORT") or os.getenv("GRADIO_SERVER_PORT") or settings.ui_port or 7860
            )
            demo.launch(server_name="0.0.0.0", server_port=server_port)
        else:
            fastapi_app = create_app(orchestrator)
            uvicorn.run(
                fastapi_app,
                host=settings.ui_host,
                port=settings.ui_port,
                log_level="info",
            )
    finally:
        orchestrator.stop()
