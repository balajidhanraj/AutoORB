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
        f'border:1px solid {fg};margin-right:6px;">{html.escape(text)}</span>'
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
            _badge(f"MODE: {mode}", "live" if mode == "LIVE" else "paper"),
            _badge(f"OAuth: {snap.get('oauth_status', '--')}", "info"),
            _badge(f"WS: {'ONLINE' if snap.get('ws_connected') else 'OFFLINE'}", "live" if snap.get("ws_connected") else "neutral"),
            _badge(f"IP: {snap.get('public_ip', 'unknown')}", "info"),
            _badge(f"IST: {snap.get('ist_time', '--')}", "neutral"),
            _badge(f"Algo: {snap.get('sebi_algo_id', '--')}", "neutral"),
        ]
    )

    kpis = [
        ("Live Total MTM", f"₹{mtm:.2f}", "#10b981" if mtm >= 0 else "#ef4444"),
        ("Trades Today", f"{snap.get('trades_taken', 0)} / {snap.get('max_trades', 2)}", "#f3f4f6"),
        ("India VIX (Min 12.0)", _fmt_num(vix), "#f3f4f6"),
        ("System State", str(snap.get("halt_reason") or "HALTED") if halted else "ACTIVE", "#ef4444" if halted else "#10b981"),
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
        "No active positions.",
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
        f'{_render_indices(snap.get("indices", {}))}'
        f'<div style="background:#111827;border:1px solid #374151;border-radius:12px;padding:16px;margin-bottom:16px;">'
        f'<div style="font-weight:600;margin-bottom:8px;">Active Positions &amp; Bracket Lifecycle</div>{pos_table}</div>'
        f'<div style="background:#111827;border:1px solid #374151;border-radius:12px;padding:16px;margin-bottom:16px;">'
        f'<div style="font-weight:600;margin-bottom:8px;">Completed Trades Today</div>{trades_table}</div>'
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


def toggle_trading_mode() -> Tuple[str, str]:
    """Toggle trading mode between PAPER and LIVE when no positions are open."""
    _changed, message = orchestrator.toggle_mode()
    return message, render_dashboard_html()


def trigger_kill_switch() -> Tuple[str, str]:
    """Execute the emergency kill switch to flatten positions and halt trading today."""
    orchestrator.trigger_kill_switch(reason="MANUAL_UI_KILL_SWITCH")
    return "Emergency kill switch executed.", render_dashboard_html()


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
            mode_btn = gr.Button("Toggle PAPER / LIVE")
            refresh_btn = gr.Button("Refresh Snapshot")
            kill_btn = gr.Button("PANIC KILL SWITCH", variant="stop")

        dashboard_html = gr.HTML(value=render_dashboard_html)

        oauth_btn.click(
            fn=run_oauth_login,
            outputs=[status_banner, dashboard_html],
            api_name="oauth_login",
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
