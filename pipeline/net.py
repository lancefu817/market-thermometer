"""共用的 HTTP 工具：自訂 UA、重試、節流。

實作背景（已實測驗證，請勿隨意調鬆）：
* 台灣證交所 (TWSE) 對同一 IP 有流量限制，短時間內連發會回 HTTP 307 轉址而不是資料。
  歷史回補時必須節流，否則會拿到一堆空值卻以為「那天沒資料」。
* 台灣期交所 (TAIFEX) 的 Put/Call 下載端點若查詢區間超過約一個月，
  會回傳一頁 HTML 錯誤頁而不是 CSV（HTTP 狀態仍是 200），必須自己偵測。
"""

from __future__ import annotations

import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class FetchError(RuntimeError):
    pass


@dataclass
class Throttle:
    """每個網域各自維護「上次請求時間」，強制最小間隔。"""

    min_interval: float = 1.2
    _last: dict = field(default_factory=dict)

    def wait(self, host: str) -> None:
        prev = self._last.get(host)
        if prev is not None:
            gap = self.min_interval - (time.monotonic() - prev)
            if gap > 0:
                time.sleep(gap)
        self._last[host] = time.monotonic()


# TWSE / TAIFEX 共用；Yahoo 另開一個較寬鬆的
SLOW = Throttle(min_interval=1.3)
FAST = Throttle(min_interval=0.25)


def _request(
    url: str,
    *,
    data: bytes | None = None,
    headers: dict | None = None,
    timeout: int = 30,
) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "*/*")
    req.add_header("Accept-Language", "zh-TW,zh;q=0.9,en;q=0.8")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    # 不自動跟隨轉址：TWSE 用 307 表示「你太快了」，跟隨了就看不出來
    opener = urllib.request.build_opener(_NoRedirect)
    with opener.open(req, timeout=timeout) as resp:
        return resp.status, resp.read()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def fetch(
    url: str,
    *,
    form: dict | None = None,
    throttle: Throttle = SLOW,
    tries: int = 4,
    timeout: int = 30,
) -> bytes:
    """抓一個 URL，失敗會退避重試；回傳原始 bytes（不猜編碼）。"""
    host = urllib.parse.urlsplit(url).netloc
    data = None
    headers = {}
    if form is not None:
        data = urllib.parse.urlencode(form).encode("ascii")
        headers["Content-Type"] = "application/x-www-form-urlencoded"

    last = ""
    for attempt in range(tries):
        throttle.wait(host)
        try:
            status, body = _request(url, data=data, headers=headers, timeout=timeout)
            if status == 200:
                return body
            last = f"HTTP {status}"
        except urllib.error.HTTPError as exc:  # 4xx / 5xx
            last = f"HTTP {exc.code}"
            if exc.code in (400, 404):
                raise FetchError(f"{last} for {url}") from exc
        except Exception as exc:  # noqa: BLE001 連線層錯誤
            last = f"{type(exc).__name__}: {exc}"
        # 指數退避：1.5s → 4.5s → 13.5s
        if attempt < tries - 1:
            time.sleep(1.5 * (3**attempt))
    raise FetchError(f"{last} for {url}")
