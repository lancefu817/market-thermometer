"""總經事件行事曆。

資料真實性說明（重要）
--------------------
下面的日期**不是推導的、也不是憑印象寫的**，是從官方排程表抄下來的：

* FOMC 利率決議：Federal Reserve 官方會議行事曆
  https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
* 美國 CPI 公布：BLS 官方發布排程
  https://www.bls.gov/schedule/news_release/cpi.htm
* 美國非農就業（Employment Situation）：BLS 官方發布排程
  https://www.bls.gov/schedule/news_release/empsit.htm

特別注意：**非農就業不是「每月第一個週五」**。2026/07/02 是週四、2026/02/11 是週三，
用「第一個週五」的土法推導會錯，所以這裡一律照官方表。

唯一用規則推導的是**台指期結算日 = 每月第三個週三**，這是期交所的契約規則，
可以安全地往未來推算。

排程表每年會更新，過期了就照上面三個網址補；`_RELEASES` 沒涵蓋到的月份不會
憑空生成，只會少幾筆事件，不會出現假日期。
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

TPE = ZoneInfo("Asia/Taipei")
ET = ZoneInfo("America/New_York")

# ── FOMC 利率決議（決議公布日 = 會期第二天，美東 14:00）────────────────
# (年, 月, 決議公布日, 是否含經濟預測 SEP)
_FOMC = [
    (2026, 1, 28, False), (2026, 3, 18, True), (2026, 4, 29, False),
    (2026, 6, 17, True),  (2026, 7, 29, False), (2026, 9, 16, True),
    (2026, 10, 28, False), (2026, 12, 9, True),
    (2027, 1, 27, False), (2027, 3, 17, True), (2027, 4, 28, False),
    (2027, 6, 9, True),   (2027, 7, 28, False), (2027, 9, 15, True),
    (2027, 10, 27, False), (2027, 12, 8, True),
]

# ── BLS 發布排程，一律美東 08:30 ─────────────────────────────────────
# (發布日, 資料月份說明)
_CPI = [
    ((2026, 1, 13), "2025 年 12 月"), ((2026, 2, 13), "1 月"),
    ((2026, 3, 11), "2 月"), ((2026, 4, 10), "3 月"),
    ((2026, 5, 12), "4 月"), ((2026, 6, 10), "5 月"),
    ((2026, 7, 14), "6 月"), ((2026, 8, 12), "7 月"),
    ((2026, 9, 11), "8 月"), ((2026, 10, 14), "9 月"),
    ((2026, 11, 10), "10 月"), ((2026, 12, 10), "11 月"),
]
_NFP = [
    ((2026, 1, 9), "2025 年 12 月"), ((2026, 2, 11), "1 月"),
    ((2026, 3, 6), "2 月"), ((2026, 4, 3), "3 月"),
    ((2026, 5, 8), "4 月"), ((2026, 6, 5), "5 月"),
    ((2026, 7, 2), "6 月"), ((2026, 8, 7), "7 月"),
    ((2026, 9, 4), "8 月"), ((2026, 10, 2), "9 月"),
    ((2026, 11, 6), "10 月"), ((2026, 12, 4), "11 月"),
]

CATEGORY_STYLE = {
    "FOMC": {"label": "FOMC", "color": "#a78bfa"},
    "CPI": {"label": "CPI", "color": "#f472b6"},
    "NFP": {"label": "非農", "color": "#38bdf8"},
    "TXF": {"label": "台指結算", "color": "#fbbf24"},
}


def _et(y: int, m: int, d: int, hh: int, mm: int) -> dt.datetime:
    return dt.datetime(y, m, d, hh, mm, tzinfo=ET)


def txf_settlement(year: int, month: int) -> dt.date:
    """台指期結算日 = 每月第三個週三（期交所契約規則，可安全推算）。"""
    day = dt.date(year, month, 1)
    # 先找到第一個週三（weekday(): 週一=0，週三=2）
    day += dt.timedelta(days=(2 - day.weekday()) % 7)
    return day + dt.timedelta(days=14)


def all_events(start: dt.date, months_ahead: int = 8) -> list[dict]:
    """回傳排序好的事件清單，時間一律換算成台北時間。"""
    out: list[dict] = []

    for year, month, day, sep in _FOMC:
        when = _et(year, month, day, 14, 0)
        out.append({
            "id": f"fomc-{year}-{month:02d}",
            "name": "FOMC 利率決議",
            "category": "FOMC",
            "at": when.astimezone(TPE).isoformat(timespec="minutes"),
            "note": ("含經濟預測 SEP" if sep else "無經濟預測")
                    + f"　美東 {when:%m/%d %H:%M}",
        })

    for table, category, name in ((_CPI, "CPI", "美國 CPI 公布"),
                                  (_NFP, "NFP", "美國非農就業")):
        for (year, month, day), ref in table:
            when = _et(year, month, day, 8, 30)
            out.append({
                "id": f"{category.lower()}-{year}-{month:02d}",
                "name": name,
                "category": category,
                "at": when.astimezone(TPE).isoformat(timespec="minutes"),
                "note": f"{ref}份資料　美東 {when:%m/%d %H:%M}",
            })

    # 台指期結算日（推導）
    cursor = dt.date(start.year, start.month, 1)
    for _ in range(months_ahead + 2):
        day = txf_settlement(cursor.year, cursor.month)
        when = dt.datetime(day.year, day.month, day.day, 13, 30, tzinfo=TPE)
        out.append({
            "id": f"txf-{day.year}-{day.month:02d}",
            "name": "台指期結算",
            "category": "TXF",
            "at": when.isoformat(timespec="minutes"),
            "note": f"{day.month} 月契約，每月第三個週三",
        })
        cursor = (cursor.replace(day=28) + dt.timedelta(days=7)).replace(day=1)

    out.sort(key=lambda e: e["at"])
    return out


def build(now: dt.datetime, upcoming_count: int = 6) -> dict:
    """回傳 {'upcoming': [...], 'past_markers': [...]}。

    upcoming：從現在起最近的幾筆，附「還有幾天」。
    past_markers：過去 200 天內的事件，用來在走勢圖上畫標記。
    """
    events = all_events(now.date() - dt.timedelta(days=210))
    upcoming = []
    markers = []
    horizon = (now - dt.timedelta(days=210)).isoformat()

    for event in events:
        if event["at"] >= now.isoformat():
            if len(upcoming) < upcoming_count:
                when = dt.datetime.fromisoformat(event["at"])
                delta = when - now
                days = delta.days
                hours = delta.seconds // 3600
                upcoming.append({
                    **event,
                    "in_days": days,
                    "in_label": (f"{days} 天後" if days >= 1
                                 else f"{hours} 小時後" if hours >= 1 else "即將"),
                })
        elif event["at"] >= horizon:
            markers.append({
                "date": event["at"][:10],
                "category": event["category"],
                "name": event["name"],
            })

    return {"upcoming": upcoming, "markers": markers,
            "styles": CATEGORY_STYLE}
