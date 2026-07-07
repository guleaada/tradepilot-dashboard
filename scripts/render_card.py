#!/usr/bin/env python3
"""Screenshot docs/card.html into a 1200x630 share-card PNG.

Serves docs/ on localhost (card.html fetches ./data.json, which file:// would
block), waits for the page to flag itself ready, and screenshots at exactly
1200x630. Used by CI after export_dashboard.py; runs locally too if you have
playwright installed (pip install playwright && playwright install chromium).

Usage: python scripts/render_card.py [out.png]
"""
import functools
import http.server
import sys
import threading

from playwright.sync_api import sync_playwright


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "docs/card-latest.png"
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory="docs")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1200, "height": 630}, device_scale_factor=1)
            page.goto(f"http://127.0.0.1:{port}/card.html")
            page.wait_for_selector("body[data-ready='1']", timeout=15000)
            page.screenshot(path=out)
            browser.close()
    finally:
        server.shutdown()
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
