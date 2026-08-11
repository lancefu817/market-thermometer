"""主流程：抓資料 → 併入歷史 → 算分 → 寫出 docs/data/*.json

用法：
    python -m pipeline.build                  # 每日更新（抓最近幾天）
    python -m pipeline.build --backfill 240   # 歷史回補（第一次跑用這個）
    python -m pipeline.build --no-fetch       # 不連網，只用現有歷史重算（改參數後驗證用）
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from . import commentary, events, margin, scoring, sources
from .net import FetchError

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "docs" / "data"
SEED = ROOT / "pipeline" / "seed.csv"
VERSION = "1.0.0"

TPE = ZoneInfo("Asia/Taipei")

FIELDS = [
    "vix", "vvix", "pcr",
    "taiex_close", "taiex_open", "etf_close", "etf_open",
    "adv", "dec", "margin_kntd",
]
INT_FIELDS = {"adv", "dec", "margin_kntd"}
MAX_STALE_DAYS = 6  # 前值最多沿用幾個交易日


# ───────────────────────────── 設定檔 ─────────────────────────────


def load_config() -> dict:
    text = (ROOT / "config.yaml").read_text(encoding="utf-8")
    try:
        import yaml

        return yaml.safe_load(text)
    except ModuleNotFoundError:
        return _mini_yaml(text)


def _mini_yaml(text: str) -> dict:
    """PyYAML 沒裝時的備援解析器。

    只支援本專案 config.yaml 用到的語法：巢狀對應、行內流式對應的清單
    （`- {a: 1, b: x}`）、數字、以及帶不帶引號的字串。
    """

    def cast(raw: str):
        raw = raw.strip()
        if raw.startswith("{") and raw.endswith("}"):
            out = {}
            for part in raw[1:-1].split(","):
                if not part.strip():
                    continue
                key, _, val = part.partition(":")
                out[key.strip()] = cast(val)
            return out
        if raw.startswith("[") and raw.endswith("]"):
            return [cast(p) for p in raw[1:-1].split(",") if p.strip()]
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
            return raw[1:-1]
        low = raw.lower()
        if low in ("true", "false"):
            return low == "true"
        if low in ("null", "~", ""):
            return None
        try:
            return int(raw)
        except ValueError:
            pass
        try:
            return float(raw)
        except ValueError:
            return raw

    lines: list[tuple[int, str]] = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        body = raw_line.split(" #")[0].rstrip()
        lines.append((len(body) - len(body.lstrip()), body.strip()))

    def parse(idx: int, indent: int):
        if lines[idx][1].startswith("- "):
            items = []
            while idx < len(lines) and lines[idx][0] == indent \
                    and lines[idx][1].startswith("- "):
                items.append(cast(lines[idx][1][2:]))
                idx += 1
            return items, idx
        out: dict = {}
        while idx < len(lines) and lines[idx][0] == indent:
            key, _, rest = lines[idx][1].partition(":")
            key, rest = key.strip(), rest.strip()
            idx += 1
            if rest:
                out[key] = cast(rest)
            elif idx < len(lines) and lines[idx][0] > indent:
                out[key], idx = parse(idx, lines[idx][0])
            else:
                out[key] = None
        return out, idx

    return parse(0, lines[0][0])[0] if lines else {}


# ───────────────────────────── 歷史存取 ─────────────────────────────


def load_history() -> dict[str, dict]:
    path = DATA / "history.json"
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
        rows = raw.get("rows", raw) if isinstance(raw, dict) else raw
        return {r["date"]: {k: r.get(k) for k in ["date", *FIELDS]} for r in rows}
    return load_seed()


def load_seed() -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not SEED.exists():
        return out
    for line in SEED.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cells = line.split(",")
        if len(cells) < 11:
            continue
        row = {"date": cells[0]}
        for name, cell in zip(FIELDS, cells[1:]):
            cell = cell.strip()
            if not cell:
                row[name] = None
            else:
                row[name] = int(float(cell)) if name in INT_FIELDS else float(cell)
        out[row["date"]] = row
    return out


def blank(date: str) -> dict:
    return {"date": date, **{f: None for f in FIELDS}}


def merge(hist: dict[str, dict], date: str, **values) -> None:
    row = hist.setdefault(date, blank(date))
    for k, v in values.items():
        if v is not None:
            row[k] = v


# ───────────────────────────── 抓資料 ─────────────────────────────


def fetch_all(hist: dict[str, dict], days: int) -> dict:
    """抓最近 days 個日曆日的資料，併進 hist。回傳抓取狀態。"""
    today = dt.datetime.now(TPE).date()
    start = today - dt.timedelta(days=days)
    status = {"yahoo": None, "taifex": None, "twse_breadth": None, "twse_margin": None}

    # ── Yahoo（一次拿一整段，最便宜） ──
    rng = "1y" if days > 120 else "6mo" if days > 40 else "3mo"
    try:
        series = sources.yahoo_all(rng)
        for date, entry in series["vix"].items():
            merge(hist, date, vix=entry["close"])
        for date, entry in series["vvix"].items():
            merge(hist, date, vvix=entry["close"])
        for date, entry in series["taiex"].items():
            merge(hist, date, taiex_close=entry["close"], taiex_open=entry.get("open"))
        for date, entry in series["etf"].items():
            merge(hist, date, etf_close=entry["close"], etf_open=entry.get("open"))
        status["yahoo"] = "ok"
        print(f"  · Yahoo Finance：{len(series['vix'])} 個交易日")
    except (FetchError, KeyError, ValueError) as exc:
        status["yahoo"] = f"fail: {exc}"
        print(f"  ! Yahoo Finance 抓取失敗：{exc}")

    # ── 期交所 P/C Ratio ──
    try:
        pcr = sources.taifex_pcr(start, today)
        for date, value in pcr.items():
            merge(hist, date, pcr=value)
        status["taifex"] = "ok"
        print(f"  · 期交所 P/C Ratio：{len(pcr)} 個交易日")
    except FetchError as exc:
        status["taifex"] = f"fail: {exc}"
        print(f"  ! 期交所抓取失敗：{exc}")

    # ── 證交所（每天一個請求，必須節流；只補「還沒有的日子」） ──
    wanted = [
        start + dt.timedelta(days=i)
        for i in range((today - start).days + 1)
        if (start + dt.timedelta(days=i)).weekday() < 5
    ]
    got_b = got_m = 0
    for day in wanted:
        key = day.isoformat()
        row = hist.get(key) or {}
        need_b = row.get("adv") is None
        need_m = row.get("margin_kntd") is None
        if not (need_b or need_m):
            continue
        if need_b:
            res = sources.twse_breadth(day)
            if res:
                merge(hist, key, adv=res[0], dec=res[1])
                got_b += 1
        if need_m:
            mg = sources.twse_margin(day)
            if mg:
                merge(hist, key, margin_kntd=mg)
                got_m += 1
    status["twse_breadth"] = "ok" if got_b or not wanted else "no-new"
    status["twse_margin"] = "ok" if got_m or not wanted else "no-new"
    print(f"  · 證交所：新增漲跌家數 {got_b} 天、融資餘額 {got_m} 天")

    # ── 台指期隔夜跳空（只抓最新一天，歷史不需要）──
    tw_days = [d for d in sorted(hist) if hist[d].get("taiex_close") is not None]
    if len(tw_days) >= 2:
        try:
            night = sources.txf_night_gap(
                dt.date.fromisoformat(tw_days[-1]),
                dt.date.fromisoformat(tw_days[-2]),
            )
            if night:
                status["txf_night"] = "ok"
                status["_night"] = night
                print(f"  · 台指期夜盤：跳空 {night['gap']:+.0f} 點"
                      f"（{night['gapPct']:+.2f}%）")
            else:
                status["txf_night"] = "no-data"
        except FetchError as exc:
            status["txf_night"] = f"fail: {exc}"
            print(f"  ! 台指期夜盤抓取失敗：{exc}")
    return status


# ───────────────────────────── 算分 ─────────────────────────────


def _ffill(rows: list[dict], field: str) -> dict[str, float]:
    """回傳每個日期可用的（可能沿用前值的）數值，超過 MAX_STALE_DAYS 就不再沿用。"""
    out: dict[str, float] = {}
    last_val = None
    stale = 0
    for row in rows:
        val = row.get(field)
        if val is not None:
            last_val, stale = val, 0
        else:
            stale += 1
        if last_val is not None and stale <= MAX_STALE_DAYS:
            out[row["date"]] = last_val
    return out


def compute(hist: dict[str, dict], cfg: dict) -> tuple[list[dict], dict]:
    rows = [hist[d] for d in sorted(hist)]
    vix = _ffill(rows, "vix")
    vvix = _ffill(rows, "vvix")
    pcr = _ffill(rows, "pcr")

    breadth_all = [
        {"date": r["date"], "up": r["adv"], "down": r["dec"]}
        for r in rows
        if r.get("adv") is not None and r.get("dec") is not None
    ]

    # ADL 子分數：一次算完整個序列，再往後沿用到沒有家數資料的日子
    adl_all = scoring.adl_scores(breadth_all, cfg["thresholds"]["adl"])
    adl_cache = {r.date: (r.score, r.label) for r in adl_all}
    adl_detail = {r.date: r for r in adl_all}

    adl_score_by_date: dict[str, tuple[float | None, str, float | None]] = {}
    last = None
    stale = 0
    for row in rows:
        if row["date"] in adl_cache:
            score, label = adl_cache[row["date"]]
            value = adl_detail[row["date"]].value
            last, stale = (score, label, value), 0
        else:
            stale += 1
        if last is not None and stale <= MAX_STALE_DAYS:
            adl_score_by_date[row["date"]] = last

    series: list[dict] = []
    for row in rows:
        date = row["date"]
        # x 軸用台股交易日（要跟 0050 對得起來）
        if row.get("taiex_close") is None and row.get("etf_close") is None:
            continue
        adl = adl_score_by_date.get(date, (None, "資料不足", None))
        graded = scoring.score_day(
            {"vix": vix.get(date), "vvix": vvix.get(date), "pcr": pcr.get(date)},
            cfg,
            adl[0],
        )
        if graded["total"] is None:
            continue
        series.append({
            "date": date,
            "total": round(graded["total"], 2),
            "sub": {k: (round(v, 2) if v is not None else None)
                    for k, v in graded["sub"].items()},
            "vix": vix.get(date),
            "vvix": vvix.get(date),
            "pcr": pcr.get(date),
            "adl_value": adl[2],
            "adl_label": adl[1],
            "taiex_close": row.get("taiex_close"),
            "taiex_open": row.get("taiex_open"),
            "etf_close": row.get("etf_close"),
            "margin_kntd": row.get("margin_kntd"),
        })

    latest_adl_date = breadth_all[-1]["date"] if breadth_all else None
    detail = adl_detail.get(latest_adl_date) if latest_adl_date else None
    return series, {"adl_detail": detail, "rows": rows}


# ───────────────────────────── 輸出 ─────────────────────────────


def _delta(series: list[dict], key: str):
    if len(series) < 2:
        return None
    cur, prev = series[-1].get(key), series[-2].get(key)
    if cur is None or prev is None:
        return None
    return round(cur - prev, 2)


def write_outputs(series: list[dict], extra: dict, cfg: dict, status: dict) -> dict:
    DATA.mkdir(parents=True, exist_ok=True)
    rows = extra["rows"]
    today = series[-1]
    regime = scoring.regime_for(today["total"], cfg["regimes"])
    state = scoring.market_state_for(today["total"], cfg["market_states"])
    mg = margin.build(rows, cfg)
    adl_detail: scoring.AdlResult | None = extra["adl_detail"]
    # 夜盤跳空：這次抓到就用新的；抓不到（或 --no-fetch）就沿用上一次的值，
    # 免得單次抓取失敗就讓畫面上這一欄整個消失。
    night = status.pop("_night", None)
    if night is None:
        previous = DATA / "latest.json"
        if previous.exists():
            try:
                night = json.loads(previous.read_text("utf-8")).get("txfNight")
            except (json.JSONDecodeError, OSError):
                night = None
        if night:
            night = {**night, "stale": True}

    taiex_block = {
        "close": today.get("taiex_close"),
        "open": today.get("taiex_open"),
        "prevClose": series[-2].get("taiex_close") if len(series) > 1 else None,
    }
    gap = margin.gap_report(taiex_block, cfg)
    taiex_block.update(gap)

    ctx = {
        "total": today["total"],
        "sub": today["sub"],
        "vix": today["vix"],
        "vvix": today["vvix"],
        "pcr": today["pcr"],
        "adl_label": today["adl_label"],
        "taiex": today.get("taiex_close"),
        "etf": today.get("etf_close"),
        "regime_label": regime["label"],
        "regime_key": regime["key"],
        "dca_label": regime["dca_label"],
        "margin": mg,
        "night": night,
        "d_vix": _delta(series, "vix"),
        "d_vvix": _delta(series, "vvix"),
        "d_pcr": _delta(series, "pcr"),
        "d_adl": (
            adl_detail.series[-1]["adl"] - adl_detail.series[-2]["adl"]
            if adl_detail and len(adl_detail.series) > 1 else None
        ),
    }
    notes = commentary.generate(ctx, cfg)

    us_date = next(
        (r["date"] for r in reversed(rows) if r.get("vix") is not None), None
    )
    tw_date = next(
        (r["date"] for r in reversed(rows) if r.get("taiex_close") is not None), None
    )

    latest = {
        "date": today["date"],
        "totalScore": today["total"],
        "prevTotalScore": series[-2]["total"] if len(series) > 1 else None,
        "regime": regime,
        "marketState": state,
        "indicators": {
            "vix": {
                "value": today["vix"], "score": today["sub"]["vix"],
                "delta": _delta(series, "vix"),
                "weight": cfg["weights"]["vix"],
                "best": cfg["thresholds"]["vix"]["best"],
                "worst": cfg["thresholds"]["vix"]["worst"],
            },
            "vvix": {
                "value": today["vvix"], "score": today["sub"]["vvix"],
                "delta": _delta(series, "vvix"),
                "weight": cfg["weights"]["vvix"],
                "best": cfg["thresholds"]["vvix"]["best"],
                "worst": cfg["thresholds"]["vvix"]["worst"],
            },
            "pcr": {
                "value": today["pcr"], "score": today["sub"]["pcr"],
                "delta": _delta(series, "pcr"),
                "weight": cfg["weights"]["pcr"],
                "best": cfg["thresholds"]["pcr"]["best"],
                "worst": cfg["thresholds"]["pcr"]["worst"],
            },
            "adl": {
                "value": today["adl_value"], "score": today["sub"]["adl"],
                "label": today["adl_label"],
                # 用真正有家數資料的最後兩天算變化，而不是沿用前值後的序列
                "delta": (
                    adl_detail.series[-1]["adl"] - adl_detail.series[-2]["adl"]
                    if adl_detail and len(adl_detail.series) > 1 else None
                ),
                "weight": cfg["weights"]["adl"],
                "series": adl_detail.series if adl_detail else [],
                "slope": round(adl_detail.slope, 1)
                if adl_detail and adl_detail.slope is not None else None,
            },
        },
        "taiex": taiex_block,
        "etf": {
            "close": today.get("etf_close"),
            "prevClose": series[-2].get("etf_close") if len(series) > 1 else None,
        },
        "txfNight": night,
        "margin": mg,
        "commentary": notes,
        "regimes": cfg["regimes"],
        "marketStates": cfg["market_states"],
        "events": events.build(
            dt.datetime.now(TPE), int(cfg["events"].get("upcoming_count", 6))
        ),
        "eventsOnChart": bool(cfg["events"].get("show_on_chart", True)),
        "updatedAtUS": us_date,
        "updatedAtTW": tw_date,
    }

    history = {
        "generatedAt": dt.datetime.now(TPE).isoformat(timespec="seconds"),
        "rows": [
            {
                "date": r["date"],
                **{k: r.get(k) for k in FIELDS},
            }
            for r in rows
        ],
        "scores": [
            {
                "date": s["date"],
                "total": s["total"],
                "sub": s["sub"],
                "etf_close": s.get("etf_close"),
                "taiex_close": s.get("taiex_close"),
            }
            for s in series
        ],
    }

    now = dt.datetime.now(TPE)
    sched = cfg.get("schedule", {})
    grace = int(sched.get("grace_minutes", 90))
    run_url = None
    if os.environ.get("GITHUB_SERVER_URL") and os.environ.get("GITHUB_RUN_ID"):
        run_url = (
            f"{os.environ['GITHUB_SERVER_URL']}/{os.environ.get('GITHUB_REPOSITORY','')}"
            f"/actions/runs/{os.environ['GITHUB_RUN_ID']}"
        )
    status_doc = {
        "generatedAt": now.isoformat(timespec="seconds"),
        "version": VERSION,
        "timezone": "Asia/Taipei (UTC+8)",
        "runUrl": run_url,
        "schedule": {
            "graceMinutes": grace,
            "us": {"timeTpe": sched.get("us_time_tpe", "06:00"),
                   "next": _next_run(now, sched.get("us_time_tpe", "06:00"))},
            "tw": {"timeTpe": sched.get("tw_time_tpe", "22:00"),
                   "next": _next_run(now, sched.get("tw_time_tpe", "22:00"))},
        },
        "sources": status,
        # fresh = 手上的資料日期已經追到（或超過）最後一個收盤完成的交易日
        "us": {"date": us_date, "expected": _last_us_session(now),
               "fresh": bool(us_date and us_date >= _last_us_session(now))},
        "tw": {"date": tw_date, "expected": _last_tw_session(now),
               "fresh": bool(tw_date and tw_date >= _last_tw_session(now))},
        "counts": {"scored_days": len(series), "raw_days": len(rows)},
        "commentary": {"source": notes["source"], "model": notes["model"]},
    }

    (DATA / "latest.json").write_text(
        json.dumps(latest, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    (DATA / "history.json").write_text(
        json.dumps(history, ensure_ascii=False), encoding="utf-8"
    )
    (DATA / "status.json").write_text(
        json.dumps(status_doc, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return status_doc


def _next_run(now: dt.datetime, hhmm: str) -> str:
    """下一次排程時間（只算週一～週五，回傳含時區的 ISO 字串）。"""
    hour, minute = (int(x) for x in hhmm.split(":"))
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += dt.timedelta(days=1)
    while candidate.weekday() > 4:
        candidate += dt.timedelta(days=1)
    return candidate.isoformat(timespec="minutes")


def _last_us_session(now: dt.datetime) -> str:
    """以台北時間推算「最後一個已經收盤的美股交易日」。

    美股 D 日 16:00 ET 收盤，換算成台北是 D+1 的清晨 04:00~05:00，
    所以台北時間過了 06:00 之後，最後一個收完的美股交易日是「昨天」。
    只用來顯示『今日是否已更新』，不參與計算，不處理美國假日。
    """
    day = now.date() - dt.timedelta(days=1 if now.hour >= 6 else 2)
    while day.weekday() > 4:
        day -= dt.timedelta(days=1)
    return day.isoformat()


def _last_tw_session(now: dt.datetime) -> str:
    """台股 13:30 收盤，14:00 之後今天就算收完了。不處理台灣假日。"""
    day = now.date() if now.hour >= 14 else now.date() - dt.timedelta(days=1)
    while day.weekday() > 4:
        day -= dt.timedelta(days=1)
    return day.isoformat()


# ───────────────────────────── 入口 ─────────────────────────────


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="市場溫度計資料管線")
    ap.add_argument("--backfill", type=int, default=0,
                    help="回補最近 N 個日曆日（第一次部署時建議 240）")
    ap.add_argument("--no-fetch", action="store_true",
                    help="不連網，只用現有歷史重算（改 config.yaml 後驗證用）")
    args = ap.parse_args(argv)

    cfg = load_config()
    total_w = sum(cfg["weights"].values())
    if abs(total_w - 1.0) > 1e-6:
        print(f"! config.yaml 的權重總和是 {total_w}，不是 1.0", file=sys.stderr)

    hist = load_history()
    print(f"讀入歷史：{len(hist)} 天")

    status = {"mode": "offline"}
    if not args.no_fetch:
        days = args.backfill or 12
        print(f"抓取資料（最近 {days} 個日曆日）…")
        status = fetch_all(hist, days)
        status["mode"] = "backfill" if args.backfill else "daily"

    series, extra = compute(hist, cfg)
    if not series:
        print("! 沒有任何可評分的日期，停止", file=sys.stderr)
        return 1

    doc = write_outputs(series, extra, cfg, status)
    last = series[-1]
    print(
        f"完成：{last['date']} 綜合分數 {last['total']}"
        f"（{scoring.regime_for(last['total'], cfg['regimes'])['label']}）"
        f"｜評分天數 {doc['counts']['scored_days']}｜AI 評論來源 {doc['commentary']['source']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
