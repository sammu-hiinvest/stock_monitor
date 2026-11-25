"""FRED (Federal Reserve Bank of St. Louis 經濟資料庫) 公開 CSV 下載。

每個資料序列(series)對應官網圖表頁「Download」按鈕背後的同一個網址，完全公開、
不需要申請 API 金鑰、也不需要像期交所那樣逆向工程找隱藏端點——這是目前能拿到
美債殖利率/利差資料最省事、最省 token 的方式：

    https://fred.stlouisfed.org/graph/fredgraph.csv?id=<SERIES_ID>&cosd=YYYY-MM-DD&coed=YYYY-MM-DD

回應是純文字 CSV：observation_date,<SERIES_ID>，遇到非交易日或尚未公布的日期，
數值欄位會是 "."，直接跳過即可。

注意：這裡刻意不用 app.scrapers.common.make_session()、也刻意不自訂任何 User-Agent。
實測發現只要帶「任何」自訂 User-Agent(不論是偽裝瀏覽器的字串、還是老實表明身份的
字串)，FRED 這邊就會直接不回應、卡到逾時；只有完全不覆寫、讓 requests 用它自己預設
的 User-Agent (python-requests/x.x) 才會秒回。推測是對方只放行這個已知的預設值，
其餘一律視為可疑流量延遲處理。所以這裡用最單純的 requests.get()，不加任何 headers。
"""

import csv
import io
import logging
import re
import time
from datetime import date

import requests

log = logging.getLogger("scrapers.fred")

BASE_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ATTEMPTS = 2


def _get_csv(series_id: str, start: date, end: date) -> csv.reader:
    """下載並確認回應「真的是 CSV」才回傳 reader，表頭已被取走。

    FRED 偶爾會對這個網址回 HTTP 200 但內容是 HTML 頁面(維護/擋流量/錯誤頁)。2026-10-21
    GitHub Actions 就遇到一次：原本的寫法不檢查表頭，把 HTML 的第一行
    `<link href="https://fonts.googleapis.com/css?family=Open+Sans:300,400,...">` 用逗號切開，
    當成「日期=<link href=...:300、數值=400」寫進資料庫，24 個序列各被塞進一筆垃圾。這種垃圾
    日期排序比 "2026-..." 大，會讓「最新日期」變成 HTML 字串、債市 API 直接 500，整批靜態
    頁面(債市/Macros/Bond Auction)因此停更超過一個月。
    """
    last_err = None
    for attempt in range(_ATTEMPTS):
        resp = requests.get(
            BASE_URL,
            params={"id": series_id, "cosd": start.isoformat(), "coed": end.isoformat()},
            timeout=20,
        )
        resp.raise_for_status()
        reader = csv.reader(io.StringIO(resp.text))
        header = next(reader, None) or []
        if (
            len(header) >= 2
            and header[0].strip().lower() in ("observation_date", "date")
            and header[1].strip().upper() == series_id.upper()
        ):
            return reader
        last_err = ValueError(
            f"{series_id}: FRED 回應不是預期的 CSV (表頭={header[:2]!r}, 內容開頭={resp.text[:80]!r})"
        )
        if attempt + 1 < _ATTEMPTS:
            log.warning("%s (第 %d 次)，稍後重試", last_err, attempt + 1)
            time.sleep(3)
    raise last_err


def fetch_series(series_id: str, start: date, end: date) -> list[dict]:
    reader = _get_csv(series_id, start, end)

    rows = []
    for cols in reader:
        if len(cols) < 2:
            continue
        obs_date = cols[0].strip()
        raw_value = cols[1].strip()
        if not _ISO_DATE_RE.match(obs_date) or raw_value in ("", "."):
            continue
        try:
            value = float(raw_value)
        except ValueError:
            continue
        rows.append({"obs_date": obs_date, "value": value})
    return rows
