"""Sync ui/templates/mobile_app.html and ui/static/* into mobile/www for Capacitor builds."""

from __future__ import annotations

import shutil
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
SRC_HTML = ROOT_DIR / "ui" / "templates" / "mobile_app.html"
SRC_STATIC = ROOT_DIR / "ui" / "static"
DEST_WWW = ROOT_DIR / "mobile" / "www"
DEST_STATIC = DEST_WWW / "static"


def sync_mobile_assets() -> Path:
    """Copy and adapt the mobile PWA shell & static assets into mobile/www."""
    if not SRC_HTML.exists():
        raise FileNotFoundError(f"Missing source template: {SRC_HTML}")

    DEST_WWW.mkdir(parents=True, exist_ok=True)
    if DEST_STATIC.exists():
        shutil.rmtree(DEST_STATIC)
    shutil.copytree(SRC_STATIC, DEST_STATIC)

    html = SRC_HTML.read_text(encoding="utf-8")
    # Rewrite root-relative /static/ references so Capacitor WebView resolves local bundle files cleanly
    html = html.replace('href="/static/', 'href="./static/').replace('src="/static/', 'src="./static/')
    out_html = DEST_WWW / "index.html"
    out_html.write_text(html, encoding="utf-8")
    return out_html


if __name__ == "__main__":
    dest = sync_mobile_assets()
    print(f"Synced mobile assets -> {dest}")
