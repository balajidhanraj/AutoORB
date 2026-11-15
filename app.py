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
from typing import Any, Dict, Iterable, Tuple

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


def _fmt_num(value: Any, digits: int = 2, prefix: str = "", suffix: str = "") -> str:
    if value is None:
        return "--"
    try:
        return f"{prefix}{float(value):.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return "--"


def _badge(text: str, tone: str = "neutral") -> str:
    tones = {
        "live": ("#10b981", "#064e3b"),
        "paper": ("#f59e0b", "#451a03"),
        "danger": ("#ef4444", "#450a0a"),
        "info": ("#60a5fa", "#172554"),
        "neutral": ("#9ca3af", "#1f2937"),
    }
    fg, bg = tones.get(tone, tones["neutral"])
    return (
        f'<span style="display:inline-block;padding:4px 10px;border-radius:999px;'
        f'font-size:12px;font-weight:600;color:{fg};background:{bg};'
        f'border:1px solid {fg};margin-right:6px;margin-bottom:4px;">{html.escape(text)}</span>'
    )


def _table_html(headers: Iterable[str], rows_html: str, empty_msg: str) -> str:
    if not rows_html:
        return f'<p style="color:#9ca3af;font-style:italic;margin:8px 0;">{html.escape(empty_msg)}</p>'
    ths = "".join(
        f'<th style="text-align:left;padding:8px;border-bottom:1px solid #374151;'
        f'color:#9ca3af;font-size:12px;text-transform:uppercase;">{html.escape(h)}</th>'
        for h in headers
    )
    return (
        '<div style="overflow-x:auto;">'
        '<table style="width:100%;border-collapse:collapse;font-size:13px;">'
        f"<thead><tr>{ths}</tr></thead>"
        f"<tbody>{rows_html}</tbody>"
        "</table></div>"
    )


def _render_broker_section(snap: Dict[str, Any]) -> str:
    brk = snap.get("broker") or {}
    connected = bool(brk.get("connected"))
    conn_badge = _badge(
        f"BROKER: CONNECTED ({brk.get('user_id') or snap.get('user_id') or '--'})"
        if connected
        else "BROKER: NOT CONNECTED",
        "live" if connected else "danger",
    )
    client_parts = [
        str(brk.get("user_id") or snap.get("user_id") or "--"),
        str(brk.get("profile_name") or ""),
        str(brk.get("account_name") or ""),
    ]
    client_label = " / ".join(p for p in client_parts if p)

    fields = [
        ("Client / Profile", client_label or "--"),
        ("Available Cash", f"₹{_fmt_num(brk.get('cash', 0.0))}"),
        ("Payin / Payout", f"₹{_fmt_num(brk.get('payin', 0.0))} / ₹{_fmt_num(brk.get('payout', 0.0))}"),
        ("Collateral", f"₹{_fmt_num(brk.get('collateral', 0.0))}"),
        ("Margin Used", f"₹{_fmt_num(brk.get('margin_used', 0.0))}"),
        ("Net Available", f"₹{_fmt_num(brk.get('net_available', 0.0))}"),
        ("Strategy Capital", f"₹{_fmt_num(snap.get('capital', 0.0))}"),
    ]
    kv_cards = "".join(
        f'<div style="background:#1f2937;padding:10px;border-radius:8px;">'
        f'<div style="color:#9ca3af;font-size:11px;">{html.escape(label)}</div>'
        f'<div style="color:#f3f4f6;font-size:14px;font-weight:700;">{html.escape(val)}</div>'
        f"</div>"
        for label, val in fields
    )

    err_html = (
        f'<p style="color:#fca5a5;font-size:12px;margin-top:8px;">{html.escape(str(brk.get("error")))}</p>'
        if brk.get("error") and not connected
        else ""
    )

    ord_rows = ""
    for o in brk.get("orders", []):
        st = str(o.get("status", "")).upper()
        st_color = "#10b981" if st == "COMPLETE" else "#ef4444" if st in {"REJECTED", "CANCELED"} else "#f59e0b"
        ord_rows += (
            f"<tr>"
            f'<td style="padding:8px;border-bottom:1px solid #374151;"><strong>{html.escape(str(o.get("norenordno", "")))}</strong></td>'
            f'<td style="padding:8px;border-bottom:1px solid #374151;">{html.escape(str(o.get("norentm", "")))}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #374151;">{html.escape(str(o.get("exch", "")))}:{html.escape(str(o.get("tsym", "")))}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #374151;">{html.escape(str(o.get("trantype", "")))} ({html.escape(str(o.get("prctyp", "")))})</td>'
            f'<td style="padding:8px;border-bottom:1px solid #374151;">{html.escape(str(o.get("prd", "")))}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #374151;">{html.escape(str(o.get("fillshares", "0")))} / {html.escape(str(o.get("qty", "0")))}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #374151;">₹{html.escape(str(o.get("prc", "0")))}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #374151;color:{st_color};font-weight:700;">{html.escape(st)}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #374151;">{html.escape(str(o.get("ordersource", "")))}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #374151;">{html.escape(str(o.get("reason", "")))}</td>'
            f"</tr>"
        )
    orders_table = _table_html(
        ["Order No", "Time", "Symbol", "Side", "Prod", "Qty", "Price", "Status", "Source", "Reason / Remarks"],
        ord_rows,
        "No live Shoonya orders yet (or connect your account above).",
    )

    pos_rows = "".join(
        f"<tr>"
        f'<td style="padding:8px;border-bottom:1px solid #374151;"><strong>{html.escape(str(p.get("exch", "")))}:{html.escape(str(p.get("tsym", "")))}</strong></td>'
        f'<td style="padding:8px;border-bottom:1px solid #374151;">{html.escape(str(p.get("prd", "")))}</td>'
        f'<td style="padding:8px;border-bottom:1px solid #374151;">{html.escape(str(p.get("netqty", "0")))}</td>'
        f'<td style="padding:8px;border-bottom:1px solid #374151;">₹{html.escape(str(p.get("netavgprc", "0")))}</td>'
        f'<td style="padding:8px;border-bottom:1px solid #374151;">₹{html.escape(str(p.get("lp", "0")))}</td>'
        f'<td style="padding:8px;border-bottom:1px solid #374151;">₹{html.escape(str(p.get("rpnl", "0")))}</td>'
        f'<td style="padding:8px;border-bottom:1px solid #374151;">₹{html.escape(str(p.get("urmtom", "0")))}</td>'
        f"</tr>"
        for p in brk.get("positions", [])
    )
    pos_table = _table_html(
        ["Symbol", "Prod", "Net Qty", "Avg Price", "LTP", "Realized P&L", "Unrealized MTM"],
        pos_rows,
        "No live broker positions open in Shoonya.",
    )

    links_bar = (
        '<div style="margin-top:10px;display:flex;gap:10px;flex-wrap:wrap;">'
        '<a href="https://prism.shoonya.com/" target="_blank" rel="noopener" '
        'style="display:inline-block;padding:6px 12px;border-radius:8px;background:#065f46;'
        'color:#fff;text-decoration:none;font-size:12px;font-weight:600;">+ Add Real Funds (Shoonya Prism)</a>'
        '<a href="https://trade.shoonya.com/" target="_blank" rel="noopener" '
        'style="display:inline-block;padding:6px 12px;border-radius:8px;background:#1f2937;'
        'border:1px solid #374151;color:#f3f4f6;text-decoration:none;font-size:12px;font-weight:600;">Open Shoonya Web Terminal</a>'
        "</div>"
    )

    return (
        f'<div style="background:#111827;border:1px solid #374151;border-radius:12px;padding:16px;margin-bottom:16px;">'
        f'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">'
        f'<strong style="font-size:15px;">Live Shoonya Broker Account &amp; Funds</strong>{conn_badge}</div>'
        f'<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:10px;">{kv_cards}</div>'
        f"{links_bar}{err_html}</div>"
        f'<div style="background:#111827;border:1px solid #374151;border-radius:12px;padding:16px;margin-bottom:16px;">'
        f'<div style="font-weight:600;margin-bottom:8px;">Live Shoonya Broker Order Book (OrderBook)</div>{orders_table}</div>'
        f'<div style="background:#111827;border:1px solid #374151;border-radius:12px;padding:16px;margin-bottom:16px;">'
        f'<div style="font-weight:600;margin-bottom:8px;">Live Shoonya Broker Positions (PositionBook)</div>{pos_table}</div>'
    )


def _render_indices(indices: Dict[str, Dict[str, Any]]) -> str:
    cards = []
    for name, st in indices.items():
        if st.get("no_trade_reason"):
            status = _badge(str(st["no_trade_reason"]), "danger")
        elif st.get("is_locked"):
            status = _badge("ORB LOCKED", "live")
        else:
            status = _badge("WAITING 09:30", "neutral")

        kvs = [
            ("Spot LTP", _fmt_num(st.get("spot_ltp"))),
            ("ORH (9:15-9:30)", _fmt_num(st.get("orh"))),
            ("ORL (9:15-9:30)", _fmt_num(st.get("orl"))),
            ("Range % (<=0.75%)", _fmt_num(st.get("range_pct"), suffix="%")),
            ("Intraday VWAP", _fmt_num(st.get("vwap"))),
            ("Wilder RSI(14)", _fmt_num(st.get("rsi"))),
        ]
        kv_blocks = "".join(
            f'<div style="background:#1f2937;padding:10px;border-radius:8px;">'
            f'<div style="color:#9ca3af;font-size:11px;">{html.escape(label)}</div>'
            f'<div style="color:#f3f4f6;font-size:15px;font-weight:700;">{html.escape(val)}</div>'
            f"</div>"
            for label, val in kvs
        )
        cards.append(
            f'<div style="background:#111827;border:1px solid #374151;border-radius:12px;padding:16px;">'
            f'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">'
            f'<strong style="font-size:15px;color:#f3f4f6;">{html.escape(name)} Spot &amp; ORB</strong>{status}</div>'
            f'<div style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;">{kv_blocks}</div>'
            f"</div>"
        )
    return (
        '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));'
        f'gap:14px;margin-bottom:16px;">{"".join(cards)}</div>'
    )


def render_dashboard_html() -> str:
    """Render current trading terminal snapshot as dark-themed HTML."""
    snap = orchestrator.get_status_snapshot()
    mode = str(snap.get("mode", "PAPER"))
    mtm = float(snap.get("mtm_pnl", 0.0) or 0.0)
    halted = bool(snap.get("halted"))
    vix = snap.get("india_vix")

    badges_html = "".join(
        [
            _badge(f"Client: {snap.get('user_id') or '--'}", "info"),
            _badge(f"MODE: {mode}", "live" if mode == "LIVE" else "paper"),
            _badge(f"OAuth: {snap.get('oauth_status', '--')}", "info"),
            _badge(
                f"WS: {'ONLINE' if snap.get('ws_connected') else 'OFFLINE'}",
                "live" if snap.get("ws_connected") else "neutral",
            ),
            _badge(f"IP: {snap.get('public_ip', 'unknown')}", "info"),
            _badge(f"IST: {snap.get('ist_time', '--')}", "neutral"),
            _badge(f"Algo: {snap.get('sebi_algo_id', '--')}", "neutral"),
        ]
    )

    kpis = [
        ("Live Total MTM", f"₹{mtm:.2f}", "#10b981" if mtm >= 0 else "#ef4444"),
        ("Trades Today", f"{snap.get('trades_taken', 0)} / {snap.get('max_trades', 2)}", "#f3f4f6"),
        ("India VIX (Min 12.0)", _fmt_num(vix), "#f3f4f6"),
        (
            "System State",
            str(snap.get("halt_reason") or "HALTED") if halted else "ACTIVE",
            "#ef4444" if halted else "#10b981",
        ),
    ]
    kpi_html = "".join(
        f'<div style="background:#111827;border:1px solid #374151;border-radius:12px;padding:14px;">'
        f'<div style="color:#9ca3af;font-size:11px;text-transform:uppercase;letter-spacing:0.04em;">{html.escape(title)}</div>'
        f'<div style="color:{color};font-size:22px;font-weight:700;margin-top:4px;">{html.escape(value)}</div>'
        f"</div>"
        for title, value, color in kpis
    )

    pos_rows = "".join(
        f"<tr>"
        f'<td style="padding:8px;border-bottom:1px solid #374151;"><strong>{html.escape(str(p.get("tradingsymbol", "")))}</strong></td>'
        f'<td style="padding:8px;border-bottom:1px solid #374151;">{html.escape(str(p.get("state", "")))}</td>'
        f'<td style="padding:8px;border-bottom:1px solid #374151;">{p.get("remaining_qty", 0)} / {p.get("initial_qty", 0)}</td>'
        f'<td style="padding:8px;border-bottom:1px solid #374151;">₹{_fmt_num(p.get("entry_avg_price"))}</td>'
        f'<td style="padding:8px;border-bottom:1px solid #374151;">₹{_fmt_num(p.get("last_ltp"))}</td>'
        f'<td style="padding:8px;border-bottom:1px solid #374151;">₹{_fmt_num(p.get("sl_trigger_price"))}</td>'
        f'<td style="padding:8px;border-bottom:1px solid #374151;">₹{_fmt_num(p.get("target1_price"))}</td>'
        f'<td style="padding:8px;border-bottom:1px solid #374151;">₹{_fmt_num((p.get("unrealized_pnl", 0) or 0) + (p.get("realized_pnl", 0) or 0))}</td>'
        f"</tr>"
        for p in snap.get("positions", [])
    )
    pos_table = _table_html(
        ["Contract", "State", "Qty", "Entry", "LTP", "SL Trig", "Target 1", "P&L"],
        pos_rows,
        "No active strategy positions.",
    )

    trade_rows = "".join(
        f"<tr>"
        f'<td style="padding:8px;border-bottom:1px solid #374151;">{html.escape(str(t.get("tradingsymbol", "")))}</td>'
        f'<td style="padding:8px;border-bottom:1px solid #374151;">{html.escape(str(t.get("signal_type", "")))}</td>'
        f'<td style="padding:8px;border-bottom:1px solid #374151;">{t.get("quantity", 0)}</td>'
        f'<td style="padding:8px;border-bottom:1px solid #374151;">{html.escape(str(t.get("entry_time", "")))} @ ₹{_fmt_num(t.get("entry_price"))}</td>'
        f'<td style="padding:8px;border-bottom:1px solid #374151;">{html.escape(str(t.get("exit_time", "")))} @ ₹{_fmt_num(t.get("exit_price"))}</td>'
        f'<td style="padding:8px;border-bottom:1px solid #374151;">{html.escape(str(t.get("exit_reason", "")))}</td>'
        f'<td style="padding:8px;border-bottom:1px solid #374151;">₹{_fmt_num(t.get("net_pnl"))}</td>'
        f"</tr>"
        for t in snap.get("completed_trades", [])
    )
    trades_table = _table_html(
        ["Contract", "Side", "Qty", "Entry", "Exit", "Reason", "Net P&L"],
        trade_rows,
        "No completed trades yet.",
    )

    log_lines = "".join(
        f'<div style="margin-bottom:3px;color:{"#ef4444" if l.get("level") in ("ERROR", "CRITICAL") else "#f59e0b" if "WARN" in str(l.get("level", "")) else "#d1d5db"};">'
        f"[{html.escape(str(l.get('timestamp', '')))}] {html.escape(str(l.get('level', '')))} | "
        f"{html.escape(str(l.get('logger', '')))} | {html.escape(str(l.get('message', '')))}</div>"
        for l in snap.get("logs", [])[-50:]
    ) or '<div style="color:#9ca3af;">Waiting for engine logs...</div>'

    return (
        '<div style="background:#0b1120;color:#f3f4f6;padding:18px;border-radius:14px;'
        'font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;">'
        f'<div style="background:#111827;border:1px solid #374151;border-radius:12px;padding:14px 16px;margin-bottom:16px;">'
        f'<div style="font-size:18px;font-weight:700;margin-bottom:4px;">Shoonya Automated ORB Terminal</div>'
        f'<div style="font-size:12px;color:#9ca3af;margin-bottom:10px;">15-Min ORB (09:15-09:30) + Intraday VWAP + Wilder RSI(14)</div>'
        f"<div>{badges_html}</div></div>"
        f'<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin-bottom:16px;">{kpi_html}</div>'
        f"{_render_broker_section(snap)}"
        f'{_render_indices(snap.get("indices", {}))}'
        f'<div style="background:#111827;border:1px solid #374151;border-radius:12px;padding:16px;margin-bottom:16px;">'
        f'<div style="font-weight:600;margin-bottom:8px;">Active Strategy Positions &amp; Bracket Lifecycle</div>{pos_table}</div>'
        f'<div style="background:#111827;border:1px solid #374151;border-radius:12px;padding:16px;margin-bottom:16px;">'
        f'<div style="font-weight:600;margin-bottom:8px;">Completed Strategy Trades Today</div>{trades_table}</div>'
        f'<div style="background:#111827;border:1px solid #374151;border-radius:12px;padding:16px;">'
        f'<div style="font-weight:600;margin-bottom:8px;">Live Engine Logs</div>'
        f'<div style="background:#050810;border:1px solid #374151;border-radius:8px;padding:10px;'
        f'max-height:240px;overflow-y:auto;font-family:ui-monospace,Consolas,monospace;font-size:12px;">'
        f"{log_lines}</div></div></div>"
    )


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
            oauth_btn = gr.Button("Run OAuth Login", variant="primary")
            sync_btn = gr.Button("Sync Broker Funds & Orders")
            mode_btn = gr.Button("Toggle PAPER / LIVE")
            refresh_btn = gr.Button("Refresh Snapshot")
            kill_btn = gr.Button("PANIC KILL SWITCH", variant="stop")

        with gr.Accordion("Add / Update Strategy Capital & Add Real Funds", open=False):
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
                ord_exch = gr.Dropdown(["NSE", "NFO"], value="NSE", label="Exchange")
                ord_tsym = gr.Textbox(value="TCS-EQ", label="Trading Symbol")
                ord_side = gr.Dropdown(["B", "S"], value="B", label="Side")
                ord_prd = gr.Dropdown(["C", "I", "M"], value="C", label="Product (C=CNC, I=MIS, M=NRML)")
                ord_prctyp = gr.Dropdown(["LMT", "MKT"], value="LMT", label="Order Type")
                ord_qty = gr.Number(value=1, precision=0, label="Qty")
                ord_prc = gr.Number(value=2407.50, label="Price")
            ord_btn = gr.Button("Send Live Broker Order", variant="primary")

        with gr.Accordion("Connect / Switch Any Shoonya Account (Multi-User Onboarding)", open=False):
            with gr.Row():
                conn_uid = gr.Textbox(label="Shoonya User ID *", placeholder="e.g. FA53975")
                conn_actid = gr.Textbox(label="Account ID (optional)", placeholder="Defaults to User ID")
                conn_pwd = gr.Textbox(label="Password *", type="password")
            with gr.Row():
                conn_totp = gr.Textbox(label="TOTP Secret (Base32) *", type="password")
                conn_apikey = gr.Textbox(label="API Key / Client ID", placeholder="Default: <USER_ID>_U")
                conn_skey = gr.Textbox(label="Secret Key (64-char) *", type="password")
                conn_algoid = gr.Textbox(label="SEBI Algo ID (optional)")
            conn_btn = gr.Button("Connect & Login Shoonya Account", variant="primary")

        dashboard_html = gr.HTML(value=render_dashboard_html)

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
