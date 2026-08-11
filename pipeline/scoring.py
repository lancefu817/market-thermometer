"""評分邏輯：四個指標 → 子分數 → 加權綜合分數 → 市場狀態。

公式全部透明可查，門檻都放在 config.yaml。

* VIX / VVIX：越低越好，在 best~worst 之間線性插值。
* P/C OI Ratio：逆向解讀（PCR 高＝法人偏多防守），越高越好。
* ADL：先算滾動視窗內的累計騰落線，取均線，再看均線斜率。
        斜率用「當期視窗內平均每日漲跌家數差的絕對值」做無因次化，
        再經 tanh 壓到 0~100，50 分代表廣度持平。
"""

from __future__ import annotations

import math
from dataclasses import dataclass


def clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def linear_score(value: float, best: float, worst: float) -> float:
    """value 在 best（100 分）與 worst（0 分）之間線性插值。best 可大於或小於 worst。"""
    if best == worst:
        return 50.0
    return clamp((value - worst) / (best - worst) * 100.0)


# ────────────────────────────── ADL ──────────────────────────────


@dataclass
class AdlResult:
    date: str
    value: float | None          # 當前累計騰落值（滾動視窗淨家數）
    score: float | None          # 0~100 子分數
    label: str                   # 多頭擴散 / 中性盤整 / 空頭破底
    slope: float | None          # 均線斜率（家數／日）
    series: list[dict]           # [{date, up, down, adl}] 供前端畫圖


def _label_for(score: float | None) -> str:
    if score is None:
        return "資料不足"
    if score >= 60:
        return "多頭擴散"
    if score >= 40:
        return "中性盤整"
    return "空頭破底"


def _stdev(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    mean = sum(xs) / len(xs)
    return (sum((x - mean) ** 2 for x in xs) / len(xs)) ** 0.5


def adl_scores(breadth: list[dict], cfg: dict) -> list[AdlResult]:
    """把漲跌家數序列換算成每日的 ADL 子分數。

    breadth: [{'date':..., 'up':int, 'down':int}]，需已按日期排序。

    步驟
    ----
    1. 淨家數 net = 上漲 − 下跌。
    2. 騰落線取「滾動 window 日的 net 總和」（原站畫的就是這條線）。
    3. 對騰落線取 ma 日均線，再算 slope_days 日的每日平均斜率。
    4. 斜率除以「歷史斜率的標準差」做 z-score 標準化，再用 tanh 壓進 0~100。
       用標準差而不是絕對家數，是因為淨家數的量級會隨掛牌檔數改變，
       而且淨家數本身遠小於總家數；若直接除以總家數，分數會永遠黏在 50 附近，
       等於白白浪費 30% 的權重。
    """
    window = int(cfg.get("window", 23))
    ma_len = int(cfg.get("ma", 10))
    slope_days = int(cfg.get("slope_days", 5))
    sensitivity = float(cfg.get("sensitivity", 1.0))

    nets = [r["up"] - r["down"] for r in breadth]
    n = len(breadth)

    # 滾動視窗淨家數
    line: list[float] = []
    for i in range(n):
        line.append(float(sum(nets[max(0, i - window + 1) : i + 1])))

    # 均線
    ma: list[float | None] = [None] * n
    for i in range(ma_len - 1, n):
        ma[i] = sum(line[i - ma_len + 1 : i + 1]) / ma_len

    # 斜率
    slopes: list[float | None] = [None] * n
    for i in range(n):
        a, b = ma[i], ma[i - slope_days] if i - slope_days >= 0 else None
        if a is not None and b is not None:
            slopes[i] = (a - b) / slope_days

    valid = [s for s in slopes if s is not None]
    sigma = _stdev(valid)
    if sigma <= 0:
        sigma = max(sum(abs(x) for x in nets) / max(len(nets), 1), 1.0)

    out: list[AdlResult] = []
    for i in range(n):
        # 前端要畫的騰落線：視窗內從 0 起算的累計（與原站一致）
        seg = breadth[max(0, i - window + 1) : i + 1]
        cum = 0
        chart = []
        for row in seg:
            cum += row["up"] - row["down"]
            chart.append({
                "date": row["date"], "up": row["up"],
                "down": row["down"], "adl": cum,
            })

        slope = slopes[i]
        if slope is None or len(valid) < 12:
            score = None
        else:
            score = clamp(50.0 + 50.0 * math.tanh(slope / (1.5 * sigma) * sensitivity))
        out.append(AdlResult(
            date=breadth[i]["date"],
            value=line[i],
            score=score,
            label=_label_for(score),
            slope=slope,
            series=chart,
        ))
    return out


# ─────────────────────── 綜合分數與市場狀態 ───────────────────────


def composite(sub: dict[str, float | None], weights: dict[str, float]) -> float | None:
    """只用有分數的指標加權；缺哪一個就把它的權重按比例分給其他人。"""
    usable = {k: v for k, v in sub.items() if v is not None and k in weights}
    if not usable:
        return None
    total_w = sum(weights[k] for k in usable)
    if total_w <= 0:
        return None
    return sum(usable[k] * weights[k] for k in usable) / total_w


def market_state_for(score: float | None, states: list[dict]) -> dict:
    """溫度計下方的「市場現在是什麼樣子」標籤。

    和 regime_for 是兩套獨立命名：這裡講現象（強勢多頭／高波動），
    regime_for 講對策（高檔過熱→少扣一點）。同一個分數可以同時是兩者。
    """
    if score is None:
        return {"key": "unknown", "label": "資料不足", "tone": "neutral"}
    for band in sorted(states, key=lambda r: -r["min"]):
        if score >= band["min"]:
            return dict(band)
    return dict(states[-1])


def regime_for(score: float | None, regimes: list[dict]) -> dict:
    if score is None:
        return {
            "key": "unknown", "label": "資料不足", "tone": "neutral",
            "dca": 100, "dca_label": "100%",
        }
    for band in sorted(regimes, key=lambda r: -r["min"]):
        if score >= band["min"]:
            return dict(band)
    return dict(regimes[-1])


def score_day(values: dict, cfg: dict, adl_score: float | None) -> dict:
    """把一天的原始值換算成子分數與綜合分數。"""
    th = cfg["thresholds"]
    sub: dict[str, float | None] = {"adl": adl_score}
    sub["vix"] = (
        linear_score(values["vix"], th["vix"]["best"], th["vix"]["worst"])
        if values.get("vix") is not None else None
    )
    sub["vvix"] = (
        linear_score(values["vvix"], th["vvix"]["best"], th["vvix"]["worst"])
        if values.get("vvix") is not None else None
    )
    sub["pcr"] = (
        linear_score(values["pcr"], th["pcr"]["best"], th["pcr"]["worst"])
        if values.get("pcr") is not None else None
    )
    total = composite(sub, cfg["weights"])
    return {"sub": sub, "total": total}
