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
from datetime import date

import requests

BASE_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"


def fetch_series(series_id: str, start: date, end: date) -> list[dict]:
    resp = requests.get(
        BASE_URL,
        params={"id": series_id, "cosd": start.isoformat(), "coed": end.isoformat()},
        timeout=20,
    )
    resp.raise_for_status()

    reader = csv.reader(io.StringIO(resp.text))
    next(reader, None)  # 表頭：observation_date,<series_id>

    rows = []
    for cols in reader:
        if len(cols) < 2:
            continue
        obs_date = cols[0].strip()
        raw_value = cols[1].strip()
        if not obs_date or raw_value in ("", "."):
            continue
        try:
            value = float(raw_value)
        except ValueError:
            continue
        rows.append({"obs_date": obs_date, "value": value})
    return rows
