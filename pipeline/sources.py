"""四個資料源的抓取與解析。

| 指標                    | 來源                | 端點 |
|-------------------------|---------------------|------|
| VIX / VVIX / TAIEX/0050 | Yahoo Finance       | /v8/finance/chart/{symbol} |
| 台指選擇權 P/C OI 比    | 台灣期貨交易所      | /cht/3/pcRatioDown (Big5 CSV) |
| 漲跌證券數（ADL 原料）  | 台灣證券交易所      | /rwd/zh/afterTrading/MI_INDEX?type=MS |
| 市場融資餘額            | 台灣證券交易所      | /rwd/zh/marginTrading/MI_MARGN?selectType=MS |

全部是公開端點，不需要任何金鑰。
"""

from __future__ import annotations

import datetime as dt
import json
import re
from zoneinfo import ZoneInfo

from .net import FAST, SLOW, FetchError, fetch

YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/"
TAIFEX_PCR = "https://www.taifex.com.tw/cht/3/pcRatioDown"
TAIFEX_FUT = "https://www.taifex.com.tw/cht/3/futDailyMarketReport"
TWSE_INDEX = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
TWSE_MARGIN = "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN"

SYMBOLS = {
    "vix": "^VIX",
    "vvix": "^VVIX",
    "taiex": "^TWII",
    "etf": "0050.TW",
}


# ──────────────────────────── Yahoo Finance ────────────────────────────


def yahoo_series(symbol: str, rng: str = "1y") -> dict[str, dict]:
    """回傳 {YYYY-MM-DD: {'close':x, 'open':y}}，日期是該交易所當地日曆日。"""
    url = f"{YAHOO}{symbol}?range={rng}&interval=1d&includePrePost=false"
    payload = json.loads(fetch(url, throttle=FAST).decode("utf-8"))
    chart = payload.get("chart") or {}
    results = chart.get("result") or []
    if not results:
        raise FetchError(f"Yahoo 沒有回傳 {symbol} 的資料：{chart.get('error')}")
    res = results[0]
    tz = ZoneInfo(res["meta"].get("exchangeTimezoneName", "UTC"))
    stamps = res.get("timestamp") or []
    quote = (res.get("indicators", {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    opens = quote.get("open") or []

    out: dict[str, dict] = {}
    for i, ts in enumerate(stamps):
        close = closes[i] if i < len(closes) else None
        if close is None:
            continue
        day = dt.datetime.fromtimestamp(ts, tz).date().isoformat()
        entry = {"close": round(float(close), 2)}
        op = opens[i] if i < len(opens) else None
        if op is not None:
            entry["open"] = round(float(op), 2)
        out[day] = entry
    return out


def yahoo_all(rng: str = "1y") -> dict[str, dict[str, dict]]:
    return {key: yahoo_series(sym, rng) for key, sym in SYMBOLS.items()}


# ─────────────────────── 台灣期交所 Put/Call Ratio ───────────────────────
# 陷阱：查詢區間超過約一個月會回傳 HTML 錯誤頁（HTTP 200），所以要自己切成小段。


def taifex_pcr(start: dt.date, end: dt.date) -> dict[str, float]:
    """回傳 {YYYY-MM-DD: 買賣權未平倉量比率(%)}。會自動把區間切成 25 天一段。"""
    out: dict[str, float] = {}
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + dt.timedelta(days=24), end)
        out.update(_taifex_pcr_chunk(cursor, chunk_end))
        cursor = chunk_end + dt.timedelta(days=1)
    return out


def _taifex_pcr_chunk(start: dt.date, end: dt.date) -> dict[str, float]:
    form = {
        "down_type": "1",
        "commodity_id": "",
        "queryStartDate": start.strftime("%Y/%m/%d"),
        "queryEndDate": end.strftime("%Y/%m/%d"),
    }
    raw = fetch(TAIFEX_PCR, form=form, throttle=SLOW)
    text = raw.decode("big5", errors="replace")
    if "<" in text[:400]:  # 期交所把錯誤頁當成 200 回來
        raise FetchError("期交所回傳錯誤頁（查詢區間可能過長）")

    out: dict[str, float] = {}
    for line in text.splitlines()[1:]:
        cols = [c.strip() for c in line.split(",")]
        if len(cols) < 7 or not re.match(r"^\d{4}/\d{2}/\d{2}$", cols[0]):
            continue
        try:
            value = float(cols[6])
        except ValueError:
            continue
        out[cols[0].replace("/", "-")] = value
    return out


# ─────────────────── 台指期 日盤 / 夜盤（盤後交易時段）───────────────────
# 已實測確認的語意（很容易搞反，改動前先讀）：
#   「盤後交易時段(T)」跑的是 T−1 下午 15:00 到 T 清晨 05:00 那一夜，
#   也就是它**先於**同一個交易日 T 的一般交易時段。
#   驗證：夜盤(T) 的最後成交價幾乎等於日盤(T) 的開盤價
#     2026/08/11 夜盤 last 44719 → 日盤 open 44761（差 42）
#     2026/08/10 夜盤 last 45033 → 日盤 open 44985（差 48）
#   所以「隔夜跳空」= 夜盤(T).最後成交價 − 日盤(T−1).最後成交價。


def _taifex_fut(day: dt.date, market_code: int) -> dict | None:
    """market_code：0 = 一般交易時段（日盤），1 = 盤後交易時段（夜盤）。

    回傳最近月契約的 {'month','open','last'}；當日無資料回 None。
    期交所這頁只給 HTML，所以用正規表示式挑出表格列（不引入 HTML 解析套件）。
    """
    form = {
        "queryType": "2",
        "marketCode": str(market_code),
        "MarketCode": str(market_code),
        "commodity_id": "TX",
        "commodity_idt": "TX",
        "queryDate": day.strftime("%Y/%m/%d"),
        "button": "送出查詢",
    }
    try:
        html = fetch(TAIFEX_FUT, form=form, throttle=SLOW).decode("utf-8", "replace")
    except FetchError:
        return None

    for row_html in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        cells = [
            re.sub(r"<[^>]+>", "", cell).replace("&nbsp;", " ").strip()
            for cell in re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.S)
        ]
        if len(cells) < 6 or cells[0] != "TX":
            continue
        if not re.fullmatch(r"\d{6}", cells[1] or ""):
            continue  # 只要月契約，跳過週選擇權那種帶週別的列

        def clean(text: str) -> float | None:
            text = text.replace(",", "").strip()
            try:
                return float(text)
            except ValueError:
                return None

        last = clean(cells[5])
        if last is None:
            continue
        return {"month": cells[1], "open": clean(cells[2]), "last": last}
    return None


def txf_night_gap(day: dt.date, prev_day: dt.date) -> dict | None:
    """算 day 這個交易日的隔夜跳空（夜盤 day − 日盤 prev_day）。"""
    night = _taifex_fut(day, 1)
    prev = _taifex_fut(prev_day, 0)
    if not night or not prev or not prev["last"]:
        return None
    gap = night["last"] - prev["last"]
    return {
        "date": day.isoformat(),
        "prevDate": prev_day.isoformat(),
        "contractMonth": night["month"],
        "nightLast": night["last"],
        "prevDayLast": prev["last"],
        "gap": round(gap, 1),
        "gapPct": round(gap / prev["last"] * 100, 3),
    }


# ─────────────────── 台灣證交所：漲跌證券數 & 融資餘額 ───────────────────


def _twse_json(url: str, params: str) -> dict | None:
    """TWSE 太忙時會回 307；net.fetch 已經重試過，這裡把仍失敗的視為當日無資料。"""
    try:
        raw = fetch(f"{url}?{params}", throttle=SLOW)
    except FetchError:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if payload.get("stat") != "OK":
        return None
    return payload


def _first_int(cell: str) -> int | None:
    """'8,242(349)' → 8242"""
    m = re.match(r"^\s*([\d,]+)", cell or "")
    return int(m.group(1).replace(",", "")) if m else None


def twse_breadth(day: dt.date) -> tuple[int, int] | None:
    """回傳 (整體市場上漲家數, 下跌家數)；當日無資料回 None。"""
    payload = _twse_json(
        TWSE_INDEX, f"date={day:%Y%m%d}&type=MS&response=json"
    )
    if not payload:
        return None
    table = next(
        (t for t in payload.get("tables", []) if t and t.get("title") == "漲跌證券數合計"),
        None,
    )
    if not table:
        return None
    adv = dec = None
    for row in table.get("data", []):
        if not row:
            continue
        if row[0].startswith("上漲"):
            adv = _first_int(row[1])
        elif row[0].startswith("下跌"):
            dec = _first_int(row[1])
    if adv is None or dec is None:
        return None
    return adv, dec


def twse_margin(day: dt.date) -> int | None:
    """回傳當日『融資金額(仟元)』的今日餘額；當日無資料回 None。"""
    payload = _twse_json(
        TWSE_MARGIN, f"date={day:%Y%m%d}&selectType=MS&response=json"
    )
    if not payload:
        return None
    tables = payload.get("tables") or []
    if not tables or not tables[0]:
        return None
    for row in tables[0].get("data", []):
        if row and row[0].startswith("融資金額"):
            return _first_int(row[5])
    return None
