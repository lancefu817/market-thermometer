"""本機截圖驗收：把 docs/ 起一個 http server，用 Chromium 截桌機與手機版。

    python tools/shot.py            # 產生 /tmp/shots/desktop.png、mobile.png
"""

from __future__ import annotations

import functools
import http.server
import socketserver
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
OUT = Path("/tmp/shots")


def serve() -> int:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(DOCS))
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return port


def main() -> int:
    from playwright.sync_api import sync_playwright

    OUT.mkdir(parents=True, exist_ok=True)
    port = serve()
    url = f"http://127.0.0.1:{port}/index.html"
    errors: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for name, size in (("desktop", (1360, 1000)), ("mobile", (414, 900))):
            page = browser.new_page(viewport={"width": size[0], "height": size[1]},
                                    device_scale_factor=2 if name == "mobile" else 1)
            page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}")
                    if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            page.goto(url, wait_until="networkidle")
            page.wait_for_timeout(600)
            page.screenshot(path=str(OUT / f"{name}.png"), full_page=True)
            page.close()
        browser.close()

    for line in errors:
        print("!", line)
    print(f"截圖完成 → {OUT}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
