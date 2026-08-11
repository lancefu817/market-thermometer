"""互動與版面自我檢查：跑一遍就知道圖表、游標、切換按鈕、行動版有沒有壞。

    python tools/check.py
"""

from __future__ import annotations

import sys

from shot import DOCS, OUT, serve  # noqa: F401  同目錄


def main() -> int:
    from playwright.sync_api import sync_playwright

    OUT.mkdir(parents=True, exist_ok=True)
    port = serve()
    url = f"http://127.0.0.1:{port}/index.html"
    problems: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch()

        page = browser.new_page(viewport={"width": 1360, "height": 950})
        page.on("pageerror", lambda e: problems.append(f"JS 錯誤：{e}"))
        page.on("console",
                lambda m: problems.append(f"console.error：{m.text}")
                if m.type == "error" else None)
        page.goto(url, wait_until="networkidle")
        page.wait_for_timeout(500)

        # 1. 兩張圖都有畫出線
        for sel, name in (("#scoreChart", "分數圖"), ("#priceChart", "價格圖")):
            paths = page.eval_on_selector_all(f"{sel} path", "els => els.length")
            if paths < 1:
                problems.append(f"{name}沒有畫出資料線")

        # 2. 融資趨勢小圖
        if page.eval_on_selector_all("#marginTrend path", "els => els.length") < 1:
            problems.append("融資趨勢小圖沒有畫出資料線")

        # 3. 游標連動與 tooltip（要先捲到畫面內，否則滑鼠座標落在視窗外）
        page.locator("#scoreChart").scroll_into_view_if_needed()
        page.wait_for_timeout(200)
        box = page.locator("#scoreChart").bounding_box()
        page.mouse.move(box["x"] + box["width"] * 0.55, box["y"] + box["height"] / 2)
        page.wait_for_timeout(220)
        if page.locator("#tip").evaluate("el => getComputedStyle(el).opacity") == "0":
            problems.append("游標移到圖上時 tooltip 沒有出現")
        markers = page.eval_on_selector_all(
            "#priceChart circle",
            "els => els.filter(e => e.getAttribute('opacity') === '1').length")
        if markers < 1:
            problems.append("價格圖的游標沒有跟著分數圖連動")

        # 4. 範圍切換
        for days in ("30", "180", "0"):
            page.click(f'#rangeBtns button[data-days="{days}"]')
            page.wait_for_timeout(180)
            info = page.locator("#rangeInfo").inner_text()
            if "筆" not in info:
                problems.append(f"切換到 {days} 日後沒有更新筆數說明")

        # 5. 資料表存在
        page.click("details.table-view summary")
        page.wait_for_timeout(150)
        rows = page.eval_on_selector_all("#tableHost tbody tr", "els => els.length")
        if rows < 5:
            problems.append("資料表列數異常")

        # 6. 雙標籤系統：市場狀態與扣款倍率要是兩個不同的標籤
        state = page.locator(".score-regime").inner_text()
        advice = page.locator(".advice h3").inner_text()
        if not state or not advice:
            problems.append("雙標籤系統缺少市場狀態或扣款倍率標籤")

        # 7. 倒數計時器要真的在動
        first = page.locator("#cdTW").inner_text()
        page.wait_for_timeout(1400)
        if page.locator("#cdTW").inner_text() == first and "已到期" not in first:
            problems.append(f"下次更新倒數沒有在跑（一直是 {first}）")

        # 8. 位階視窗切換要連動位階、三層檢查與趨勢圖
        seen = set()
        for win in ("30", "60", "90", "180"):
            btn = page.locator(f'#winBtns button[data-win="{win}"]')
            if btn.count() == 0:
                problems.append(f"缺少 {win}d 位階視窗按鈕")
                continue
            btn.click()
            page.wait_for_timeout(200)
            label = page.locator("#pctLabel").inner_text()
            if win not in label:
                problems.append(f"切到 {win}d 後位階標題沒更新（{label}）")
            seen.add(page.locator("#pctValue").inner_text())
            if page.eval_on_selector_all("#marginTrend path", "e => e.length") < 1:
                problems.append(f"切到 {win}d 後趨勢圖沒有資料線")
            if page.eval_on_selector_all("#riskHost .checks li", "e => e.length") != 3:
                problems.append(f"切到 {win}d 後三層檢查不是 3 條")
        if len(seen) < 2:
            problems.append("切換位階視窗時位階數值完全沒變，可能沒有連動")

        # 9. tooltip：關鍵推導必須有 title 屬性
        tips = page.evaluate(
            "[...document.querySelectorAll('[title]')].map(e=>e.getAttribute('title'))"
            ".filter(t=>t&&t.length>20).length")
        if tips < 8:
            problems.append(f"詳細推導 tooltip 只有 {tips} 個，應該至少 8 個")
        if not page.evaluate(
                "[...document.querySelectorAll('[title]')]"
                ".some(e=>e.getAttribute('title').includes('peak-to-current'))"):
            problems.append("累積跌壓的 tooltip 沒有說明 peak-to-current 定義")

        # 10. 總經事件：清單要有內容，且時間要用台北時區顯示
        if page.eval_on_selector_all("ul.events li", "e => e.length") < 3:
            problems.append("總經事件清單筆數不足")
        fomc = page.evaluate(
            "(()=>{const li=[...document.querySelectorAll('ul.events li')]"
            ".find(e=>e.textContent.includes('FOMC'));"
            "return li?li.querySelector('.ev-when').textContent:null})()")
        if fomc and not fomc.endswith("02:00"):
            problems.append(f"FOMC 顯示時間 {fomc} 不是台北時間（應為 02:00）")

        # 11. 走勢圖上的事件標記
        if page.eval_on_selector_all(
                "#scoreChart line[stroke-dasharray='2 4']", "e => e.length") < 1:
            problems.append("走勢圖上沒有畫出總經事件標記")

        page.screenshot(path=str(OUT / "desktop.png"), full_page=True)
        page.close()

        # 6. 行動版：圖表不能被壓成一條線
        m = browser.new_page(viewport={"width": 414, "height": 900},
                             device_scale_factor=2)
        m.on("pageerror", lambda e: problems.append(f"行動版 JS 錯誤：{e}"))
        m.goto(url, wait_until="networkidle")
        m.wait_for_timeout(500)
        h = m.locator("#scoreChart").bounding_box()["height"]
        if h < 150:
            problems.append(f"行動版分數圖太扁（{h:.0f}px）")
        if m.evaluate("document.documentElement.scrollWidth > innerWidth + 2"):
            problems.append("行動版有水平溢出")
        m.screenshot(path=str(OUT / "mobile.png"), full_page=True)
        m.close()
        browser.close()

    if problems:
        for p in problems:
            print("✕", p)
        return 1
    print("✓ 全部檢查通過（圖表、游標連動、範圍切換、資料表、行動版版面、"
          "雙標籤、倒數計時、位階視窗連動、推導 tooltip、總經事件與台北時區、事件標記）")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(__file__.rsplit("/", 1)[0]))
    sys.exit(main())
