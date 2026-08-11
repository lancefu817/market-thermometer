"""融資警戒：散戶槓桿水位與三層斷頭風險指標。

三個門檻都有依據，不是隨手挑的數字（每一層在網頁上 hover 都能看到推導）：

1. 槓桿擁擠度　融資餘額落在近 N 個交易日的 P80 以上 → 歷史前 20% 高位。
   ≥P90 為強訊號。意義是「受傷面積」：位階越高，下跌時被波及的帳戶越廣。
2. 累積跌壓　　TAIEX 近 10 個交易日的 **peak-to-current** 跌幅 ≤ −5%。
   維持率數學：脆弱帳戶（R=137%）在指數跌 5% 時被打到 130% 斷頭線。
   −10% 為強訊號（數年一次的尾部極端）。
   ⚠ 注意這是「最高點跌到現價」，不是「10 日前到今天的變化」——
     兩者差很多，用錯的話盤整盤會完全看不出壓力。
3. 去化加速　　融資近 3 個交易日累積淨減少 ≥ 總餘額的 1%。
   門檻用「總餘額百分比」而不是固定金額，才不會隨市場規模成長被稀釋。
   機制：T+2 追繳週期下，1% 縮幅約等於最脆弱 5% 帳戶的集體去化量級。

四個位階視窗（30/60/90/180）全部預先算好放進 latest.json，網頁上切換不需重算。
"""

from __future__ import annotations


# ────────────────────────── 統計小工具 ──────────────────────────


def percentile_rank(series: list[float], value: float) -> float:
    """value 在 series 中的百分位（0~100），定義為「小於或等於的比例」。"""
    if not series:
        return 50.0
    below = sum(1 for x in series if x <= value)
    return round(below / len(series) * 100.0, 1)


def quantile(series: list[float], q: float) -> float | None:
    if not series:
        return None
    ordered = sorted(series)
    if len(ordered) == 1:
        return ordered[0]
    pos = q * (len(ordered) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    frac = pos - lo
    return ordered[lo] * (1 - frac) + ordered[hi] * frac


def _counts(series: list[float], level: float) -> tuple[int, int]:
    """回傳 (≤level 的天數, >level 的天數)，給 tooltip 用。"""
    below = sum(1 for x in series if x <= level)
    return below, len(series) - below


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = min(len(xs), len(ys))
    if n < 3:
        return None
    xs, ys = xs[-n:], ys[-n:]
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    sxx = sum((a - mx) ** 2 for a in xs)
    syy = sum((b - my) ** 2 for b in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / (sxx * syy) ** 0.5


def _pct_change(series: list[float], days: int) -> float | None:
    if len(series) <= days:
        return None
    old, new = series[-1 - days], series[-1]
    if not old:
        return None
    return (new - old) / old * 100.0


def peak_to_current(series: list[float], days: int) -> float | None:
    """近 days 個交易日的最高點跌到現價的幅度（%）。永遠 ≤ 0。

    這是「跌壓」的正確定義：盤中創高後回落，這個值會馬上反映，
    而「N 日前 → 今天」的算法會完全看不見。
    """
    window = series[-(days + 1):]
    if len(window) < 2:
        return None
    peak = max(window)
    if not peak:
        return None
    return (window[-1] - peak) / peak * 100.0


def _pct_label(p: float | None) -> str:
    if p is None:
        return "資料不足"
    if p >= 80:
        return "偏高位階"
    if p >= 60:
        return "中高位階"
    if p >= 40:
        return "中性位階"
    if p >= 20:
        return "中低位階"
    return "偏低位階"


# ────────────────────────── 主體 ──────────────────────────


def build(rows: list[dict], cfg: dict) -> dict:
    """rows：完整歷史（含缺值），需按日期排序。回傳可直接丟進 latest.json 的 dict。"""
    mcfg = cfg["margin"]
    trig = mcfg["triggers"]
    windows = list(mcfg.get("windows") or [90])
    default_window = int(mcfg.get("default_window", 90))

    mrows = [r for r in rows if r.get("margin_kntd")]
    if not mrows:
        return {"available": False, "windows": {}, "defaultWindow": default_window}

    balances = [r["margin_kntd"] / 100_000.0 for r in mrows]   # 仟元 → 億元
    dates = [r["date"] for r in mrows]
    taiex = [r["taiex_close"] for r in rows if r.get("taiex_close")]

    out: dict = {
        "available": True,
        "date": dates[-1],
        "balance_e": round(balances[-1], 1),
        "prev_e": round(balances[-2], 1) if len(balances) > 1 else None,
        "change_e": round(balances[-1] - balances[-2], 1) if len(balances) > 1 else None,
        "history_days": len(balances),
        "defaultWindow": default_window,
        "windows": {},
    }

    # ── 第 2、3 層與視窗無關，只算一次 ──
    dd_days = int(trig["drawdown_days"])
    drawdown = peak_to_current(taiex, dd_days)
    dl_days = int(trig["deleverage_days"])
    delev = (balances[-1] - balances[-1 - dl_days]
             if len(balances) > dl_days else None)
    dl_threshold = balances[-1] * trig["deleverage_pct"] / 100.0
    dl_strong = balances[-1] * trig["deleverage_strong_pct"] / 100.0

    for win in windows:
        out["windows"][str(win)] = _one_window(
            win, balances, dates, taiex, trig, mcfg,
            drawdown, dd_days, delev, dl_days, dl_threshold, dl_strong,
        )
    return out


def _one_window(
    win: int, balances: list[float], dates: list[str], taiex: list[float],
    trig: dict, mcfg: dict,
    drawdown: float | None, dd_days: int,
    delev: float | None, dl_days: int,
    dl_threshold: float, dl_strong: float,
) -> dict:
    ref = balances[-win:]
    today = balances[-1]
    pct = percentile_rank(ref, today)
    q75, q25 = quantile(ref, 0.75), quantile(ref, 0.25)
    n = len(ref)
    le_today, gt_today = _counts(ref, today)

    def days_note(level: float | None) -> str:
        if level is None:
            return ""
        le, gt = _counts(ref, level)
        return f"近 {n} 個交易日中，{le} 天 ≤ {level:,.0f} 億、{gt} 天 > {level:,.0f} 億。"

    # ── 三層檢查 ──
    crowding_hit = pct >= trig["crowding_pct"]
    checks = [
        {
            "name": "槓桿擁擠度",
            "value": f"P{pct:.0f}",
            "threshold": f"≥ P{trig['crowding_pct']:.0f}",
            "hit": crowding_hit,
            "strong": pct >= trig["crowding_strong_pct"],
            "tip": (
                f"市場散戶融資餘額在近 {n} 個交易日中的百分位排名。\n"
                f"門檻根據：\n"
                f"• ≥P{trig['crowding_pct']:.0f}：歷史上前 "
                f"{100 - trig['crowding_pct']:.0f}% 高位，散戶槓桿擁擠\n"
                f"• ≥P{trig['crowding_strong_pct']:.0f}（強訊號）：歷史上前 "
                f"{100 - trig['crowding_strong_pct']:.0f}% 極端高位\n"
                f"• 意義：P% 越高，受傷面積越大 — 一旦市場下跌，被波及的帳戶越廣"
            ),
        },
        {
            "name": f"累積跌壓 ({dd_days}d)",
            # peak-to-current 依定義 ≤ 0，不加正號才不會出現「+0.0%」這種怪寫法
            "value": f"{drawdown:.1f}%" if drawdown is not None else "—",
            "threshold": f"≤ {trig['drawdown_pct']:.0f}%",
            "hit": drawdown is not None and drawdown <= trig["drawdown_pct"],
            "strong": drawdown is not None and drawdown <= trig["drawdown_strong_pct"],
            "tip": (
                f"TAIEX 過去 {dd_days} 個交易日 peak-to-current 跌幅"
                f"（最高點跌到現價，不是 {dd_days} 日前到今天的變化）。\n"
                f"門檻根據：\n"
                f"• 維持率數學：脆弱帳戶（R=137%）在 TAIEX 跌 "
                f"{abs(trig['drawdown_pct']):.0f}% 時被打到 130% 斷頭線\n"
                f"• 統計：屬歷史下尾事件，一年約十餘天\n"
                f"• 強訊號 {trig['drawdown_strong_pct']:.0f}%：數年一次的歷史尾部極端"
            ),
        },
        {
            "name": f"去化加速 ({dl_days}d)",
            "value": f"{delev:+,.1f} 億" if delev is not None else "—",
            "threshold": f"≤ {dl_threshold:,.0f} 億 ({trig['deleverage_pct']:.0f}%)",
            "hit": delev is not None and delev <= dl_threshold,
            "strong": delev is not None and delev <= dl_strong,
            "tip": (
                f"融資餘額過去 {dl_days} 個交易日的累積淨變化。\n"
                f"門檻為「當前總餘額 {abs(trig['deleverage_pct']):.0f}%」動態計算，"
                f"當前 = {abs(dl_threshold):,.0f} 億。"
                f"隨市場規模成長自動 scale，不會像固定金額被稀釋。\n"
                f"根據：\n"
                f"• 機制：T+2 追繳週期下，{abs(trig['deleverage_pct']):.0f}% 縮幅 "
                f"≈ 最脆弱 5% 帳戶的集體去化量級\n"
                f"• 強訊號 ≤ {trig['deleverage_strong_pct']:.0f}%"
                f"（{abs(dl_strong):,.0f} 億）：歷史尾部極端"
            ),
        },
    ]
    hits = sum(1 for c in checks if c["hit"])
    risk_label, risk_tone, risk_tip = {
        0: ("風險低", "good", "三層皆未觸發 — 無實質斷頭壓力"),
        1: ("留意", "warning", "觸發 1 層 — 有壓力跡象，但還不成斷頭情境"),
        2: ("風險升高", "serious", "觸發 2 層 — 已接近斷頭潮的前置條件"),
        3: ("斷頭警報", "critical", "三層全中 — 最接近真實斷頭潮（維持率 130% 強制平倉）"),
    }[hits]

    # ── 槓桿動能（跟著視窗連動；解讀門檻按視窗比例 scale）──
    base = int(mcfg.get("momentum_base_window", 90))
    base_pp = float(mcfg.get("momentum_excess_pp", 5.0))
    scaled_pp = round(base_pp * win / base, 1)

    mg_pct = _pct_change(balances, win)
    tx_pct = _pct_change(taiex, win)
    momentum = None
    if mg_pct is not None and tx_pct is not None:
        mg_d = [
            (balances[i] - balances[i - 1]) / balances[i - 1] * 100
            for i in range(max(1, len(balances) - win), len(balances))
            if balances[i - 1]
        ]
        tx_d = [
            (taiex[i] - taiex[i - 1]) / taiex[i - 1] * 100
            for i in range(max(1, len(taiex) - win), len(taiex))
            if taiex[i - 1]
        ]
        corr = _pearson(mg_d, tx_d)
        excess = mg_pct - tx_pct
        if excess > scaled_pp:
            verdict, tone = "散戶加槓桿追多", "warning"
        elif excess < -scaled_pp:
            verdict, tone = "散戶去槓桿觀望", "good"
        else:
            verdict, tone = "槓桿與指數同步", "neutral"
        momentum = {
            "window": win,
            "taiex_pct": round(tx_pct, 1),
            "margin_pct": round(mg_pct, 1),
            "excess_pp": round(excess, 1),
            "sync_pct": round(corr * 100, 0) if corr is not None else None,
            "verdict": verdict,
            "tone": tone,
            "threshold_pp": scaled_pp,
            "tip": (
                f"過去 {win} 個交易日的 TAIEX 與融資餘額對比（連動位階視窗）。\n"
                f"散戶超漲 = 融資漲幅 − TAIEX 漲幅，>0 代表散戶比指數更狂熱地加槓桿。\n"
                f"同步 = 兩者日變化的 Pearson 相關係數，接近 100% 代表幾乎完美同步。\n"
                f"解讀門檻按視窗比例 scale（基準 {base} 日 = ±{base_pp:.0f}pp）："
                f"當前視窗 {win}d = ±{scaled_pp:.1f}pp"
            ),
        }

    return {
        "window": win,
        "percentile": pct,
        "percentile_label": _pct_label(pct),
        "percentile_tip": (
            f"近 {n} 個交易日散戶槓桿位階：第 {pct:.0f} 百分位（{_pct_label(pct)}）。\n"
            f"近 {n} 個交易日中，{le_today} 天 ≤ {balances[-1]:,.0f} 億、"
            f"{gt_today} 天 > {balances[-1]:,.0f} 億。\n"
            f"≥P{trig['crowding_pct']:.0f} 代表槓桿擁擠，"
            f"≤P{100 - trig['crowding_pct']:.0f} 代表槓桿冷清。"
        ),
        "p75_e": round(q75, 1) if q75 is not None else None,
        "p25_e": round(q25, 1) if q25 is not None else None,
        "p75_tip": "上四分位線 P75：" + days_note(q75) + "越過代表槓桿擁擠。",
        "p25_tip": "下四分位線 P25：" + days_note(q25) + "跌破代表槓桿冷清。",
        "today_tip": f"今日水位：{days_note(today)}第 {pct:.0f} 百分位。",
        "sample_days": n,
        "trend": [
            {"date": d, "balance_e": round(b, 1)}
            for d, b in zip(dates[-win:], ref)
        ],
        "triggers": checks,
        "risk_hits": hits,
        "risk_label": risk_label,
        "risk_tone": risk_tone,
        "risk_tip": risk_tip,
        "momentum": momentum,
    }


# ────────────────────────── 開盤跳空預警 ──────────────────────────


def gap_report(taiex: dict, cfg: dict) -> dict:
    """開盤跳空與盤中累積，附券商追繳門檻判定。"""
    mcfg = cfg["margin"]
    warn, severe = mcfg["gap_warn_pct"], mcfg["gap_severe_pct"]
    prev, open_, close = taiex.get("prevClose"), taiex.get("open"), taiex.get("close")

    gap = gap_pct = intraday_pct = None
    if prev and open_ is not None:
        gap = open_ - prev
        gap_pct = gap / prev * 100
    if prev and close is not None:
        intraday_pct = (close - prev) / prev * 100

    if gap_pct is None:
        level, label = "unknown", "—"
    elif gap_pct <= severe:
        level, label = "severe", "嚴重低開"
    elif gap_pct <= warn:
        level, label = "warn", "低開預警"
    elif gap_pct > 0.05:
        level, label = "up", "開高"
    elif gap_pct < -0.05:
        level, label = "down", "開低"
    else:
        level, label = "flat", "平盤開"

    return {
        "gap": round(gap, 1) if gap is not None else None,
        "gapPct": round(gap_pct, 3) if gap_pct is not None else None,
        "intradayPct": round(intraday_pct, 3) if intraday_pct is not None else None,
        "level": level,
        "label": label,
        "tip": (
            "TWSE ^TWII 加權指數行情。\n"
            "• 開盤跳空 = 今日開盤 − 前日收盤（券商於開盤瞬間用此計算追繳）\n"
            "• 盤中累積 = 收盤價 − 前日收盤（含開盤後的續跌／反彈）\n"
            f"門檻（與第 2 層同一套維持率推導）：\n"
            f"• ≤ {warn:.0f}% 低開預警：R=133% 最脆弱 1% 帳戶觸及斷頭線\n"
            f"• ≤ {severe:.0f}% 嚴重低開：R=137% 脆弱 5% 帳戶觸及斷頭線"
        ),
    }
