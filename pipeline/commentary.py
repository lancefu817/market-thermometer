"""每日文字評論。

預設走 Google Gemini（有免費額度）。金鑰從環境變數讀：
    GEMINI_API_KEY / ANTHROPIC_API_KEY / OPENAI_API_KEY
沒有金鑰、或呼叫失敗時，自動改用內建的規則式文字，網頁不會開天窗。

送給模型的內容只有本專案自己算出來的數字，不含任何外部抓進來的文字，
所以不存在把外部內容當指令執行的風險。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

TIMEOUT = 45

SYSTEM = (
    "你是一位務實的台股／美股市場觀察者，服務對象是用 0050 做定期定額的長期投資人。"
    "只根據使用者提供的數字說話，不要編造任何未提供的數據、新聞或個股名稱，"
    "不要給出保證性的預測，不要出現「必定」「保證」這類字眼。"
    "用繁體中文，語氣冷靜、具體，每段 120～180 字，不要用條列符號。"
)


def _post_json(url: str, payload: dict, headers: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ─────────────────────────── 各家 API ───────────────────────────


def _gemini(prompt: str, model: str, key: str) -> str:
    # 金鑰走 x-goog-api-key 標頭，不放在網址的 query string 裡：
    # Google AI Studio 現在發的是 auth key，官方範例也是用標頭帶。
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
    )
    payload = {
        "systemInstruction": {"parts": [{"text": SYSTEM}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 1200},
    }
    out = _post_json(url, payload, {"x-goog-api-key": key})
    parts = out["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts).strip()


def _anthropic(prompt: str, model: str, key: str) -> str:
    payload = {
        "model": model,
        "max_tokens": 1200,
        "system": SYSTEM,
        "messages": [{"role": "user", "content": prompt}],
    }
    out = _post_json(
        "https://api.anthropic.com/v1/messages",
        payload,
        {"x-api-key": key, "anthropic-version": "2023-06-01"},
    )
    return "".join(b.get("text", "") for b in out["content"]).strip()


def _openai(prompt: str, model: str, key: str) -> str:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompt},
        ],
    }
    out = _post_json(
        "https://api.openai.com/v1/chat/completions",
        payload,
        {"Authorization": f"Bearer {key}"},
    )
    return out["choices"][0]["message"]["content"].strip()


PROVIDERS = {
    "gemini": ("GEMINI_API_KEY", "gemini_model", _gemini),
    "anthropic": ("ANTHROPIC_API_KEY", "anthropic_model", _anthropic),
    "openai": ("OPENAI_API_KEY", "openai_model", _openai),
}


# ─────────────────────────── 提示與備援 ───────────────────────────


def _prompt(ctx: dict) -> str:
    sub = ctx["sub"]
    mg = ctx.get("margin") or {}
    lines = [
        "以下是今天收盤後算出來的市場溫度計數據：",
        f"- 綜合分數：{ctx['total']:.1f} / 100（狀態：{ctx['regime_label']}，"
        f"對應 0050 定期定額扣款倍率 {ctx['dca_label']}）",
        f"- VIX 美股恐慌指數 {ctx['vix']}（子分數 {sub['vix']}，權重 45%）",
        f"- VVIX 波動率的波動率 {ctx['vvix']}（子分數 {sub['vvix']}，權重 15%）",
        f"- 台指選擇權 Put/Call 未平倉量比 {ctx['pcr']}%（子分數 {sub['pcr']}，權重 10%，"
        "逆向解讀：比值高代表法人偏多防守）",
        f"- ADL 騰落指標 10 日均線斜率：{ctx['adl_label']}"
        f"（子分數 {sub['adl']}，權重 30%）",
        f"- 加權指數收盤 {ctx.get('taiex')}，0050 收盤 {ctx.get('etf')}",
    ]
    win = (mg.get("windows") or {}).get(str(mg.get("defaultWindow"))) or {}
    if mg.get("balance_e"):
        lines.append(
            f"- 市場融資餘額 {mg['balance_e']} 億元，位於近 {win.get('window')} 日的 "
            f"P{win.get('percentile')}（{win.get('percentile_label')}），"
            f"三層斷頭風險指標觸發 {win.get('risk_hits')}/3（{win.get('risk_label')}）"
        )
    if win.get("momentum"):
        mo = win["momentum"]
        lines.append(
            f"- 近 {mo['window']} 日槓桿動能：指數 {mo['taiex_pct']:+.1f}%、"
            f"融資 {mo['margin_pct']:+.1f}%、散戶超漲 {mo['excess_pp']:+.1f} 個百分點"
        )
    if ctx.get("night"):
        ni = ctx["night"]
        lines.append(
            f"- 台指期隔夜（盤後交易時段）跳空 {ni['gap']:+.0f} 點"
            f"（{ni['gapPct']:+.2f}%）"
        )
    lines += [
        "",
        "請輸出一個 JSON 物件，不要加上任何說明或程式碼標記，鍵如下：",
        '{"long_term": "...", "swing": "...", "deltas": {"vix": "...", "vvix": "...",'
        ' "pcr": "...", "adl": "...", "margin": "...", "longterm": "..."}}',
        "long_term：寫給 0050 定期定額的長期投資人，聚焦目前該維持、加碼或暫緩加碼，"
        "以及融資水位對長線的意義。",
        "swing：寫給看台指期的短線交易者，聚焦波動率環境與籌碼方向、部位控制。",
        "deltas：六句**各 15～30 字**的白話短句，說明該項今天的變化代表什麼。"
        "vix/vvix/pcr/adl 各對應上面那個指標；margin 講斷頭壓力；"
        "longterm 用一句話總結長投該怎麼做。",
    ]
    return "\n".join(lines)


def _rule_deltas(ctx: dict) -> dict:
    """沒有 AI 時的六句白話短句，用數值方向直接組出來。"""
    sub = ctx["sub"]
    mg = ctx.get("margin") or {}
    win = (mg.get("windows") or {}).get(str(mg.get("defaultWindow"))) or {}

    def move(delta, up_text, down_text, flat_text):
        if delta is None or abs(delta) < 1e-9:
            return flat_text
        return up_text if delta > 0 else down_text

    return {
        "vix": move(ctx.get("d_vix"),
                    "VIX 上升，投資人風險擔憂增強，波動度放大",
                    "VIX 下降，risk-on 氣氛回溫，波動度收斂",
                    "VIX 幾乎沒動，市場情緒維持原樣"),
        "vvix": move(ctx.get("d_vvix"),
                     "VVIX 上升，對波動的擔憂加速，不確定性升高",
                     "VVIX 下降，波動預期趨於平穩",
                     "VVIX 持平，波動預期沒有變化"),
        "pcr": move(ctx.get("d_pcr"),
                    "PCR 上升，法人 OI 偏多防守，逆向解讀偏正面",
                    "PCR 下降，法人偏多防守鬆動，籌碼轉弱",
                    "PCR 無變化，買賣力平衡，觀望情緒濃"),
        "adl": move(ctx.get("d_adl"),
                    "ADL 改善，上漲家數擴散，市場廣度轉強",
                    "ADL 轉弱，下跌家數增加，廣度背離加深",
                    "ADL 持平，市場廣度沒有明顯方向"),
        "margin": (
            f"觸發 {win.get('risk_hits')}/3 層，{win.get('risk_label')}，留意去槓桿賣壓"
            if win.get("risk_hits") else "三層皆未觸發，無實質斷頭壓力，槓桿環境穩定"
        ),
        "longterm": f"{ctx['regime_label']}區間，扣款維持 {ctx['dca_label']}",
    }


def _rules(ctx: dict) -> tuple[str, str]:
    total = ctx["total"]
    mg = ctx.get("margin") or {}
    regime = ctx["regime_label"]
    key = ctx["regime_key"]

    long_map = {
        "overheat": (
            f"綜合分數 {total:.1f} 落在高檔過熱區，市場情緒偏樂觀、位階與溢價都不低。"
            f"此時追高的期望報酬不划算，建議維持紀律性的基本扣款（{ctx['dca_label']}），"
            "把額外資金留到分數回落時再動用。長期投資的優勢來自持續在場，而不是在高位加大部位。"
        ),
        "standard": (
            f"綜合分數 {total:.1f} 位於標準累積區間，波動率與籌碼結構都沒有明顯異常。"
            f"依原訂計畫維持 {ctx['dca_label']} 的扣款金額穩健累積股數即可，"
            "不需要預測頂底，也不必因為短線震盪調整節奏。"
        ),
        "dip": (
            f"綜合分數 {total:.1f} 進入相對低檔，市場出現健康回檔、整體評價向下修正。"
            f"中長線價值開始浮現，可考慮啟動微幅加碼把扣款提高到 {ctx['dca_label']}，"
            "分批吸收較便宜的籌碼，但仍要保留後續下跌的加碼空間。"
        ),
        "panic": (
            f"綜合分數 {total:.1f} 落在恐慌超跌區，波動率飆高、散戶部位承壓。"
            f"歷史上這種區間是長線投資人的機會區，若計畫允許可動用備用金提高到 {ctx['dca_label']}，"
            "但務必分批進場、留足生活準備金，避免一次押上全部資源。"
        ),
    }
    long_text = long_map.get(key, f"綜合分數 {total:.1f}，市場狀態為{regime}。")

    win = (mg.get("windows") or {}).get(str(mg.get("defaultWindow"))) or {}
    if win.get("risk_hits"):
        long_text += (
            f" 融資面觸發 {win['risk_hits']}/3 層警戒（{win['risk_label']}），"
            "槓桿去化過程中的賣壓可能放大短線波動，扣款節奏不變但不宜追價。"
        )
    elif win.get("percentile") is not None:
        long_text += (
            f" 融資餘額位在近 {win['window']} 日的 P{win['percentile']:.0f}"
            f"（{win['percentile_label']}），槓桿環境尚未出現系統性壓力。"
        )

    sub = ctx["sub"]
    vol_state = "波動率環境平穩" if (sub["vix"] or 0) >= 70 else (
        "波動率明顯升溫" if (sub["vix"] or 0) < 40 else "波動率中性偏高"
    )
    chip_state = "籌碼偏多防守" if (sub["pcr"] or 0) >= 60 else "籌碼偏空"
    swing_text = (
        f"綜合分數 {total:.1f}（{regime}）。VIX {ctx['vix']}、VVIX {ctx['vvix']}，"
        f"{vol_state}；台指選擇權 P/C 未平倉量比 {ctx['pcr']}%，{chip_state}；"
        f"市場廣度為{ctx['adl_label']}。"
    )
    swing_text += (
        " 分數偏高時追多的風險報酬不對稱，建議縮小部位、以區間思維操作；"
        if total >= 80 else
        " 分數落在低檔時反而適合分批建立多單，但要嚴設停損；"
        if total < 40 else
        " 分數位於中性區間，順勢操作為主，避免預設方向重壓；"
    )
    swing_text += "本段僅為指標判讀，不構成投資建議。"
    return long_text, swing_text


def generate(ctx: dict, cfg: dict) -> dict:
    """回傳 {'long_term':..., 'swing':..., 'model':..., 'source':'llm'|'rules'}"""
    ccfg = cfg.get("commentary", {})
    provider = (ccfg.get("provider") or "none").lower()

    if provider in PROVIDERS:
        env_name, model_key, fn = PROVIDERS[provider]
        key = os.environ.get(env_name, "").strip()
        model = ccfg.get(model_key) or ""
        if key and model:
            try:
                raw = fn(_prompt(ctx), model, key)
                text = raw.strip()
                if text.startswith("```"):
                    text = text.split("```")[1]
                    text = text.split("\n", 1)[1] if "\n" in text else text
                start, end = text.find("{"), text.rfind("}")
                parsed = json.loads(text[start : end + 1])
                long_text = str(parsed["long_term"]).strip()
                swing = str(parsed["swing"]).strip()
                if long_text and swing:
                    fallback = _rule_deltas(ctx)
                    raw_deltas = parsed.get("deltas") or {}
                    deltas = {
                        key: (str(raw_deltas.get(key) or "").strip() or fallback[key])
                        for key in fallback
                    }
                    return {
                        "long_term": long_text,
                        "swing": swing,
                        "deltas": deltas,
                        "model": f"{provider}:{model}",
                        "source": "llm",
                    }
            except (urllib.error.URLError, KeyError, ValueError, IndexError) as exc:
                print(f"  ! AI 評論失敗（{type(exc).__name__}: {exc}），改用規則式文字")
        else:
            print(f"  · 沒有找到 {env_name}，AI 評論改用規則式文字")

    long_text, swing = _rules(ctx)
    return {
        "long_term": long_text,
        "swing": swing,
        "deltas": _rule_deltas(ctx),
        "model": "rules",
        "source": "rules",
    }
