"""US Bureau of Labor Statistics (BLS) 公開 API v2 — CPI / PPI / 非農就業(CES) /
家戶調查(失業率、失業原因、失業週數、兼職工作) 都是用這個共用的抓取函式。

改用 BLS 本尊而不是 FRED 轉錄的版本，原因：

1. **BLS 官方公布 CPI/PPI 新聞稿的慣例**：「與去年同月比較(年增率/YoY)」用的是
   「未經季節調整(NSA)」指數；「與上月比較(月增率/MoM)」才是用「季節調整後(SA)」
   指數。這是因為 YoY 比較本來就會讓季節性效應互相抵消，用 SA 反而會引入季調模型
   本身的修訂雜訊；MoM 比較才需要靠季調去除當月的季節性波動。兩種調整方式對應
   不同用途，同一個序列不能拿來算兩種增率——這也是先前用 FRED 的 CPIAUCSL(SA)
   同時算 YoY 又算 MoM 時，年增率數字跟官方公布的對不太起來的其中一個原因。
2. 直接對齊官方一手資料源，避免透過 FRED 轉錄環節可能造成的版本/修訂延遲落差。

BLS API v2 端點：POST JSON 到 https://api.bls.gov/publicAPI/v2/timeseries/data/，
不需要註冊 API 金鑰也能用——未註冊的公開額度是每日 25 次查詢、單次最多 25 個序列、
最多 10 年區間。本專案追蹤的序列數(CPI+PPI+非農就業+家戶調查合計快 30 個)已經超過
單次 25 個序列的上限，所以 fetch_series() 會自動切成多批查詢，每批仍在額度內。

序列代碼格式舉例：
- CPI: CU + [S=季調/U=非季調] + R0000(美國城市平均) + 項目代碼，例如
  CUSR0000SA0(CPI綜合，季調) / CUUR0000SA0(CPI綜合，未季調)。
- PPI (Final Demand): WP + [S=季調/U=非季調] + FD4...(項目代碼)，例如
  WPSFD4(PPI Final Demand，季調) / WPUFD4(同，未季調)。
- 非農就業(CES)：CES(已季調) + 產業別代碼 + 資料類型代碼，例如
  CES0000000001(非農就業總數)。CES 前綴本身就已經是季調後數字，沒有對應的NSA版本
  好切換(要看未季調要換成 CEU 前綴，但本專案的非農就業一律只用官方慣例引用的SA)。
- 家戶調查(CPS)：LNS(已季調)，例如 LNS14000000(失業率)。同樣 LNS 前綴本身已經是
  季調後數字，本專案家戶調查相關的序列一律只用 SA。
"""

import logging
from datetime import date

import requests

log = logging.getLogger("scrapers.bls")

BLS_API_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"

_MONTH_PERIODS = {f"M{m:02d}" for m in range(1, 13)}  # 排除 M13(年度平均)，只留月資料
_MAX_SERIES_PER_QUERY = 25  # BLS 未註冊金鑰額度：單次查詢最多 25 個序列


def _fetch_batch(series_ids: list[str], start_year: int, end_year: int) -> dict[str, list[dict]]:
    resp = requests.post(
        BLS_API_URL,
        json={"seriesid": series_ids, "startyear": str(start_year), "endyear": str(end_year)},
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("status") != "REQUEST_SUCCEEDED":
        raise RuntimeError(f"BLS API 回傳失敗: {payload.get('status')} {payload.get('message')}")

    result: dict[str, list[dict]] = {sid: [] for sid in series_ids}
    for s in payload["Results"]["series"]:
        sid = s["seriesID"]
        rows = []
        for point in s["data"]:
            period = point["period"]
            if period not in _MONTH_PERIODS:
                continue
            raw_value = point["value"]
            if raw_value in ("-", "", None):
                continue
            try:
                value = float(raw_value)
            except ValueError:
                continue
            month = int(period[1:])
            obs_date = date(int(point["year"]), month, 1).isoformat()
            rows.append({"obs_date": obs_date, "value": value})
        result[sid] = sorted(rows, key=lambda r: r["obs_date"])
    return result


def fetch_series(series_ids: list[str], start_year: int, end_year: int) -> dict[str, list[dict]]:
    """回傳 {series_id: [{"obs_date","value"}, ...]}，每個序列按 obs_date 由舊到新排序。

    月份資料一律對應到該月 1 號 (YYYY-MM-01)。遇到資料缺漏(BLS 用 "-" 標記，例如
    2025年10月因美國政府關門、BLS 當月沒有發布報告)直接跳過，不補 0 或內插，讓
    下游用日期比對「12個月前/1個月前」的邏輯自然處理缺月(見 app/main.py 的
    _rates_from_index())。series_ids 超過 25 個時自動切成多批查詢。
    """
    result: dict[str, list[dict]] = {}
    for i in range(0, len(series_ids), _MAX_SERIES_PER_QUERY):
        batch = series_ids[i : i + _MAX_SERIES_PER_QUERY]
        result.update(_fetch_batch(batch, start_year, end_year))
    return result
