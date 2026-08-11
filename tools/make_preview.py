"""把 docs/index.html 與目前的資料打包成一個「單檔預覽版」。

產出的 preview.html 不需要伺服器、不需要網路，雙擊就能開，
資料是產生當下的快照（不會自己更新）。適合寄給別人看，或離線留存。

    python tools/make_preview.py [輸出路徑]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "preview.html"
    html = (DOCS / "index.html").read_text(encoding="utf-8")

    embed = {
        "latest": json.loads((DOCS / "data" / "latest.json").read_text("utf-8")),
        "history": json.loads((DOCS / "data" / "history.json").read_text("utf-8")),
        "status": json.loads((DOCS / "data" / "status.json").read_text("utf-8")),
    }
    payload = json.dumps(embed, ensure_ascii=False).replace("</", "<\\/")

    # 移除外部檔案參照（單檔版旁邊沒有 manifest 與 icon），改成內嵌資料
    for tag in (
        '<link rel="manifest" href="manifest.webmanifest">',
        '<link rel="icon" href="favicon.svg" type="image/svg+xml">',
        '<link rel="apple-touch-icon" href="favicon.svg">',
    ):
        html = html.replace(tag, "")
    html = html.replace(
        "</head>",
        f"<script>window.__EMBED__ = {payload};</script>\n</head>",
        1,
    )
    html = html.replace(
        "<title>市場溫度計 — Market Thermometer</title>",
        "<title>市場溫度計 — 單檔預覽版</title>",
    )
    out.write_text(html, encoding="utf-8")
    print(f"單檔預覽版 → {out}（{out.stat().st_size / 1024:.0f} KB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
