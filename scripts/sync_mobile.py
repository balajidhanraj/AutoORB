"""Sync ui/templates/mobile_app.html and ui/static/* into mobile/www for Capacitor builds."""

from __future__ import annotations

import html as html_lib
import os
import shutil
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
SRC_HTML = ROOT_DIR / "ui" / "templates" / "mobile_app.html"
SRC_STATIC = ROOT_DIR / "ui" / "static"
DEST_WWW = ROOT_DIR / "mobile" / "www"
DEST_STATIC = DEST_WWW / "static"


def sync_mobile_assets(
    api_base: str | None = None,
    prod_url: str | None = None,
    dev_url: str | None = None,
    local_url: str | None = None,
) -> Path:
    """Copy and adapt the mobile PWA shell & static assets into mobile/www."""
    if not SRC_HTML.exists():
        raise FileNotFoundError(f"Missing source template: {SRC_HTML}")

    resolved_prod = (
        prod_url if prod_url is not None else os.getenv("PROD_API_URL", "https://balajidhanraj-shoonya-orb.hf.space")
    ).strip().rstrip("/")
    resolved_dev = (
        dev_url if dev_url is not None else os.getenv("DEV_API_URL", "https://balajidhanraj-shoonya-orb.hf.space")
    ).strip().rstrip("/")
    resolved_local = (
        local_url if local_url is not None else os.getenv("LOCAL_API_URL", "http://192.168.0.109:8000")
    ).strip().rstrip("/")
    resolved_base = (
        api_base if api_base is not None else os.getenv("ANDROID_API_BASE", resolved_prod)
    ).strip().rstrip("/")

    DEST_WWW.mkdir(parents=True, exist_ok=True)
    if DEST_STATIC.exists():
        shutil.rmtree(DEST_STATIC)
    shutil.copytree(SRC_STATIC, DEST_STATIC)

    html = SRC_HTML.read_text(encoding="utf-8")
    # Inject default VPS Prod / Dev / Local Wi-Fi backend URLs for native Capacitor APK builds
    html = html.replace(
        '<meta name="autoorb-api-base" content=""/>',
        f'<meta name="autoorb-api-base" content="{html_lib.escape(resolved_base, quote=True)}"/>',
    )
    html = html.replace(
        '<meta name="autoorb-prod-api" content=""/>',
        f'<meta name="autoorb-prod-api" content="{html_lib.escape(resolved_prod, quote=True)}"/>',
    )
    html = html.replace(
        '<meta name="autoorb-dev-api" content=""/>',
        f'<meta name="autoorb-dev-api" content="{html_lib.escape(resolved_dev, quote=True)}"/>',
    )
    html = html.replace(
        '<meta name="autoorb-local-api" content=""/>',
        f'<meta name="autoorb-local-api" content="{html_lib.escape(resolved_local, quote=True)}"/>',
    )
    # Rewrite root-relative /static/ references so Capacitor WebView resolves local bundle files cleanly
    html = html.replace('href="/static/', 'href="./static/').replace('src="/static/', 'src="./static/')
    out_html = DEST_WWW / "index.html"
    out_html.write_text(html, encoding="utf-8")

    # Create /app/index.html redirect inside mobile/www so any WebView or PWA navigation to /app never 404s
    app_sub = DEST_WWW / "app"
    app_sub.mkdir(parents=True, exist_ok=True)
    (app_sub / "index.html").write_text(
        '<!DOCTYPE html><html><head><meta charset="utf-8"/>'
        '<meta http-equiv="refresh" content="0;url=../index.html"/>'
        "<script>window.location.replace('../index.html');</script>"
        "</head><body></body></html>",
        encoding="utf-8",
    )
    return out_html


if __name__ == "__main__":
    dest = sync_mobile_assets()
    print(f"Synced mobile assets -> {dest}")
