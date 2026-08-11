"""離線回歸測試：不連網，用真實回應的結構當 fixture 驗證解析與算式。

    python tools/test_parsers.py

fixture 的欄位結構與數值都是 2026-08-11 從各資料源真實取得的內容，
用來防止「網站改版或誰動了正則」造成靜默解析錯誤。
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import margin, scoring, sources  # noqa: E402
from pipeline.build import _mini_yaml, load_config  # noqa: E402

FAILS: list[str] = []


def check(name: str, got, want, tol: float | None = None) -> None:
    ok = (abs(got - want) <= tol) if (tol is not None and got is not None) else got == want
    print(f"{'✓' if ok else '✕'} {name}: {got!r}" + ("" if ok else f"（預期 {want!r}）"))
    if not ok:
        FAILS.append(name)


# ── 1. 期交所期貨行情（正則解析 HTML，全專案最脆弱的一段）─────────────
# 真實表格是 14 個 <td>：契約、到期月份、開盤、最高、最低、最後成交價、
# 漲跌價、漲跌%、盤後量、一般量、合計量、結算價、未沖銷、… 數值取自
# 2026/08/11 盤後交易時段與 2026/08/10 一般交易時段的真實回應。
def _row(month: str, values: list[str]) -> str:
    cells = "".join(f"<td class='right'>{v}</td>" for v in values)
    return f"<tr class='12'><td>TX</td><td>{month}</td>{cells}</tr>"


NIGHT_HTML = (
    "<html><body><table class='table_f'>"
    "<tr><th>契約</th><th>到期月份(週別)</th><th>開盤價</th></tr>"
    # 先塞一列「週別」格式的列，確認 parser 會跳過它
    "<tr><td>TX</td><td>202608W3</td><td>1</td><td>2</td><td>3</td><td>4</td>"
    "<td>5</td><td>6</td><td>7</td><td>8</td><td>9</td><td>10</td></tr>"
    + _row("202608", ["45,000", "45,120", "44,600", "44,719", "▼268", "▼0.60%",
                      "-", "-", "-", "44,720", "99,034", "45,078"])
    + _row("202609", ["45,100", "45,200", "44,700", "44,830", "▼270", "▼0.60%",
                      "-", "-", "-", "44,830", "14,886", "45,206"])
    + "</table></body></html>"
)
DAY_HTML = (
    "<html><body><table class='table_f'>"
    + _row("202608", ["44,985", "45,050", "44,800", "44,987", "▲100", "▲0.22%",
                      "28,989", "50,327", "79,316", "44,985", "99,034", "45,078"])
    + "</table></body></html>"
)


def test_taifex_fut() -> None:
    print("\n── 期交所期貨行情解析 ──")
    original = sources.fetch
    pages = {1: NIGHT_HTML, 0: DAY_HTML}

    def fake_fetch(url, *, form=None, **kw):
        return pages[int(form["marketCode"])].encode("utf-8")

    sources.fetch = fake_fetch
    try:
        night = sources._taifex_fut(dt.date(2026, 8, 11), 1)
        check("夜盤取到最近月契約（跳過週別列）", night["month"], "202608")
        check("夜盤最後成交價", night["last"], 44719.0)
        check("夜盤開盤價（千分位逗號要吃掉）", night["open"], 45000.0)

        gap = sources.txf_night_gap(dt.date(2026, 8, 11), dt.date(2026, 8, 10))
        check("隔夜跳空點數 = 夜盤(T) − 日盤(T−1)", gap["gap"], -268.0)
        check("隔夜跳空百分比", gap["gapPct"], -0.596, tol=0.002)
    finally:
        sources.fetch = original


# ── 2. 證交所 JSON（真實回應結構）────────────────────────────────
BREADTH_JSON = {
    "stat": "OK", "date": "20260810",
    "tables": [
        {"title": "大盤統計資訊", "fields": ["成交統計"], "data": [["1.一般股票"]]},
        {"title": "漲跌證券數合計", "fields": ["類型", "整體市場", "股票"],
         "data": [["上漲(漲停)", "8,242(349)", "747(48)"],
                  ["下跌(跌停)", "3,560(66)", "254(1)"],
                  ["持平", "601", "69"]]},
    ],
}
MARGIN_JSON = {
    "stat": "OK", "date": "20260810",
    "tables": [{
        "title": "115年08月10日 信用交易統計",
        "fields": ["項目", "買進", "賣出", "現金(券)償還", "前日餘額", "今日餘額"],
        "data": [
            ["融資(交易單位)", "436,013", "412,135", "7,092", "8,986,437", "9,003,223"],
            ["融券(交易單位)", "17,314", "28,202", "996", "192,740", "202,632"],
            ["融資金額(仟元)", "38,849,343", "30,849,674", "368,618",
             "537,664,510", "545,295,561"],
        ],
    }, None],
}


def test_twse() -> None:
    print("\n── 證交所解析 ──")
    original = sources.fetch
    try:
        sources.fetch = lambda url, **kw: json.dumps(BREADTH_JSON).encode("utf-8")
        adv, dec = sources.twse_breadth(dt.date(2026, 8, 10))
        check("上漲家數（要去掉括號裡的漲停數）", adv, 8242)
        check("下跌家數", dec, 3560)

        sources.fetch = lambda url, **kw: json.dumps(MARGIN_JSON).encode("utf-8")
        mg = sources.twse_margin(dt.date(2026, 8, 10))
        check("融資金額今日餘額（仟元）", mg, 545295561)
        check("換算成億元", round(mg / 100_000, 2), 5452.96)
    finally:
        sources.fetch = original


# ── 3. 期交所 P/C Ratio CSV（Big5 編碼）──────────────────────────
PCR_CSV = (
    "日期,賣權成交量,買權成交量,買賣權成交量比率%,賣權未平倉量,買權未平倉量,"
    "買賣權未平倉量比率%\n"
    "2026/08/10,114188,99406,114.87,85342,72704,117.38,\n"
    "2026/08/07,328986,331682,99.19,59446,55858,106.42,\n"
)


def test_pcr() -> None:
    print("\n── 期交所 P/C Ratio 解析 ──")
    original = sources.fetch
    try:
        sources.fetch = lambda url, **kw: PCR_CSV.encode("big5")
        out = sources._taifex_pcr_chunk(dt.date(2026, 8, 3), dt.date(2026, 8, 10))
        check("取的是第 7 欄「未平倉量」比率而不是成交量比率", out["2026-08-10"], 117.38)
        check("筆數", len(out), 2)

        sources.fetch = lambda url, **kw: b"<!DOCTYPE HTML><html>error</html>"
        try:
            sources._taifex_pcr_chunk(dt.date(2026, 1, 1), dt.date(2026, 3, 1))
            check("查詢區間過長要丟錯而不是回空值", "沒有丟錯", "FetchError")
        except Exception as exc:  # noqa: BLE001
            check("查詢區間過長要丟錯而不是回空值", type(exc).__name__, "FetchError")
    finally:
        sources.fetch = original


# ── 4. 評分算式（對照原站當日公開數字）──────────────────────────
def test_scoring() -> None:
    print("\n── 評分算式（對照原站 2026-08-10 的公開數字）──")
    cfg = load_config()
    th = cfg["thresholds"]
    check("VIX 15.46 → 子分數",
          round(scoring.linear_score(15.46, th["vix"]["best"], th["vix"]["worst"]), 2),
          85.53, tol=0.01)
    check("VVIX 92.51 → 子分數",
          round(scoring.linear_score(92.51, th["vvix"]["best"], th["vvix"]["worst"]), 2),
          81.22, tol=0.01)
    check("P/C 117.38% → 子分數（逆向解讀）",
          round(scoring.linear_score(117.38, th["pcr"]["best"], th["pcr"]["worst"]), 2),
          74.76, tol=0.01)
    check("線性插值要夾在 0~100",
          scoring.linear_score(60, th["vix"]["best"], th["vix"]["worst"]), 0.0)

    total = scoring.composite(
        {"vix": 85.53, "vvix": 81.22, "pcr": 74.76, "adl": 73.85}, cfg["weights"])
    check("四項加權綜合分數", round(total, 2), 80.30, tol=0.02)

    partial = scoring.composite({"vix": 80.0, "vvix": None, "pcr": None, "adl": 60.0},
                                cfg["weights"])
    check("缺項時把權重按比例分給其他項",
          round(partial, 2), round((80 * .45 + 60 * .30) / .75, 2))

    check("市場狀態 80.3 → 強勢多頭",
          scoring.market_state_for(80.3, cfg["market_states"])["label"], "強勢多頭")
    check("扣款倍率 80.3 → 高檔過熱",
          scoring.regime_for(80.3, cfg["regimes"])["label"], "高檔過熱")
    check("兩套命名互相獨立（同分數不同標籤）",
          scoring.market_state_for(80.3, cfg["market_states"])["label"]
          != scoring.regime_for(80.3, cfg["regimes"])["label"], True)


# ── 5. 累積跌壓：peak-to-current，不是首尾相減 ─────────────────────
def test_drawdown() -> None:
    print("\n── 累積跌壓定義 ──")
    # 先漲到 110 再跌回 100：首尾相減看起來沒事，peak-to-current 要抓到 −9.1%
    series = [100, 102, 104, 106, 108, 110, 108, 106, 104, 102, 100]
    check("peak-to-current 抓到回檔",
          round(margin.peak_to_current(series, 10), 1), -9.1, tol=0.05)
    check("首尾相減會漏掉（這就是為什麼不能用它）",
          round((series[-1] - series[0]) / series[0] * 100, 1), 0.0)
    check("單調上漲時跌壓為 0",
          round(margin.peak_to_current([1, 2, 3, 4], 3), 1), 0.0)
    check("資料不足回 None", margin.peak_to_current([5], 10), None)

    check("百分位：最大值 → P100", margin.percentile_rank([1, 2, 3, 4], 4), 100.0)
    check("百分位：最小值 → P25", margin.percentile_rank([1, 2, 3, 4], 1), 25.0)


# ── 6. 設定檔備援解析器要和 PyYAML 一致 ──────────────────────────
def test_config_parser() -> None:
    print("\n── config.yaml 備援解析器 ──")
    text = (Path(__file__).resolve().parent.parent / "config.yaml").read_text("utf-8")
    try:
        import yaml
    except ModuleNotFoundError:
        print("· 沒裝 PyYAML，跳過一致性比對")
        return
    check("備援解析器與 PyYAML 結果一致",
          _mini_yaml(text) == yaml.safe_load(text), True)
    cfg = yaml.safe_load(text)
    check("權重總和 = 1.0", round(sum(cfg["weights"].values()), 6), 1.0)


def main() -> int:
    for fn in (test_taifex_fut, test_twse, test_pcr, test_scoring,
               test_drawdown, test_config_parser):
        fn()
    print()
    if FAILS:
        print(f"✕ {len(FAILS)} 項失敗：{', '.join(FAILS)}")
        return 1
    print("✓ 全部解析與算式測試通過")
    return 0


if __name__ == "__main__":
    sys.exit(main())
