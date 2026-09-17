"""法人現貨：個股三大法人買賣超 + 已發行股數 (股本)，用來算「買超佔股本比重」排行。

原本要抓 Goodinfo 的「外資/投信買超佔發行張數比重」排行頁，但該站有 Cloudflare
Turnstile 人機驗證擋住(plain requests 跟瀏覽器都進不去，回應是 403 + `Cf-Mitigated:
challenge`)，改用兩個官方公開 API 自己算同樣的指標：

- 上市(TWSE)：T86「每日三大法人買賣超日報」
  https://www.twse.com.tw/rwd/zh/fund/T86?date=YYYYMMDD&selectType=ALL&response=json
  支援指定日期查詢，可以回溯多天；假日/非交易日回傳空清單。
- 上櫃(TPEx)：tpex_3insti_daily_trading「上櫃股票三大法人買賣明細資訊」OpenAPI
  https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading
  **這個端點不支援指定日期查詢，只能拿到「最新一個交易日」**，所以上櫃的歷史
  深度只能靠每天排程執行慢慢累積，沒辦法像 T86 一樣一次回溯補齊多天。

股本(已發行股數)來源：
- 上市：t187ap03_L 上市公司基本資料
  https://openapi.twse.com.tw/v1/opendata/t187ap03_L
- 上櫃：mopsfin_t187ap03_O 上櫃公司基本資料
  https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O

個股當日收盤價/漲跌幅(用來標記漲停/跌停)來源：
- 上市：MI_INDEX「每日收盤行情」(type=ALLBUT0999，取欄位為證券代號的那張表)
  https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date=YYYYMMDD&type=ALLBUT0999&response=json
  漲跌方向藏在「漲跌(+/-)」欄位的 HTML 顏色(color:red=漲 / color:green=跌)，
  要自己配合「漲跌價差」還原正負號。
- 上櫃：tpex_mainboard_daily_close_quotes OpenAPI，Change 欄位本身就帶正負號，
  只能拿到最新一個交易日(同 tpex_3insti_daily_trading 的限制)。
  https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes

兩邊 API 都不需要金鑰、沒有反爬蟲保護，用最單純的 requests.get() 就能拿到資料。
"""

import logging
from datetime import date

from app.scrapers.common import make_session, to_float

log = logging.getLogger("scrapers.institutional_stock")

T86_URL = "https://www.twse.com.tw/rwd/zh/fund/T86"
TWSE_COMPANY_INFO_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_INSTI_URL = "https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading"
TPEX_COMPANY_INFO_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"
MI_INDEX_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
TPEX_QUOTE_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"


def _roc_to_iso(s: str) -> str | None:
    """民國年日期字串(如 "1150917")轉成 ISO 格式("2026-09-17")。"""
    s = (s or "").strip()
    if len(s) < 6:
        return None
    try:
        year = int(s[:-4]) + 1911
        month = int(s[-4:-2])
        day = int(s[-2:])
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def fetch_twse_t86(on_date: date) -> list[dict]:
    """上市個股三大法人買賣超，指定日期查詢。非交易日回傳空清單(不是錯誤)。"""
    session = make_session()
    resp = session.get(
        T86_URL,
        params={"date": on_date.strftime("%Y%m%d"), "selectType": "ALL", "response": "json"},
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("stat") != "OK":
        return []

    rows = []
    for cols in payload.get("data", []):
        if len(cols) < 19:
            continue
        rows.append(
            {
                "market": "TWSE",
                "code": cols[0].strip(),
                "name": cols[1].strip(),
                "foreign_net_shares": to_float(cols[4]),
                "trust_net_shares": to_float(cols[10]),
                "dealer_net_shares": to_float(cols[11]),
            }
        )
    return rows


def fetch_tpex_daily() -> tuple[str | None, list[dict]]:
    """上櫃個股三大法人買賣超，只能拿「最新一個交易日」，不支援指定日期。
    回傳 (該筆資料實際對應的交易日 ISO 字串, rows)。
    """
    session = make_session()
    resp = session.get(TPEX_INSTI_URL, timeout=30)
    resp.raise_for_status()
    payload = resp.json()
    if not payload:
        return None, []

    trade_date = _roc_to_iso(payload[0].get("Date"))
    rows = []
    for r in payload:
        code = (r.get("SecuritiesCompanyCode") or "").strip()
        if not code:
            continue
        rows.append(
            {
                "market": "TPEX",
                "code": code,
                "name": (r.get("CompanyName") or "").strip(),
                "foreign_net_shares": to_float(
                    r.get("Foreign Investors include Mainland Area Investors (Foreign Dealers excluded)-Difference")
                ),
                "trust_net_shares": to_float(r.get("SecuritiesInvestmentTrustCompanies-Difference")),
                "dealer_net_shares": to_float(r.get("Dealers-Difference")),
            }
        )
    return trade_date, rows


def fetch_twse_shares_outstanding() -> list[dict]:
    """上市公司已發行普通股股數(股本)，整包回傳、沒有日期參數。"""
    session = make_session()
    resp = session.get(TWSE_COMPANY_INFO_URL, timeout=30)
    resp.raise_for_status()
    payload = resp.json()

    rows = []
    for r in payload:
        code = (r.get("公司代號") or "").strip()
        if not code:
            continue
        rows.append(
            {
                "market": "TWSE",
                "code": code,
                "name": (r.get("公司簡稱") or "").strip(),
                "shares_outstanding": to_float(r.get("已發行普通股數或TDR原股發行股數")),
            }
        )
    return rows


def fetch_tpex_shares_outstanding() -> list[dict]:
    """上櫃公司已發行股數(股本)，整包回傳、沒有日期參數。"""
    session = make_session()
    resp = session.get(TPEX_COMPANY_INFO_URL, timeout=30)
    resp.raise_for_status()
    payload = resp.json()

    rows = []
    for r in payload:
        code = (r.get("SecuritiesCompanyCode") or "").strip()
        if not code:
            continue
        rows.append(
            {
                "market": "TPEX",
                "code": code,
                "name": (r.get("CompanyAbbreviation") or "").strip(),
                "shares_outstanding": to_float(r.get("IssueShares")),
            }
        )
    return rows


def fetch_twse_daily_quotes(on_date: date) -> list[dict]:
    """上市個股當日收盤價/漲跌幅，指定日期查詢。非交易日回傳空清單。"""
    session = make_session()
    resp = session.get(
        MI_INDEX_URL,
        params={"date": on_date.strftime("%Y%m%d"), "type": "ALLBUT0999", "response": "json"},
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("stat") != "OK":
        return []

    quote_table = None
    for t in payload.get("tables", []):
        fields = t.get("fields") or []
        if fields and fields[0] == "證券代號":
            quote_table = t
            break
    if quote_table is None:
        return []

    rows = []
    for cols in quote_table.get("data", []):
        if len(cols) < 11:
            continue
        code = cols[0].strip()
        close = to_float(cols[8])
        change_abs = to_float(cols[10])
        if not code or close is None:
            continue
        if change_abs is None:
            change_abs = 0.0
        sign = -1 if "green" in cols[9] else 1
        change_price = sign * change_abs
        prev_close = close - change_price
        change_pct = round(change_price / prev_close * 100, 2) if prev_close else None
        rows.append({"code": code, "close": close, "change_pct": change_pct})
    return rows


def fetch_tpex_daily_quotes() -> tuple[str | None, list[dict]]:
    """上櫃個股當日收盤價/漲跌幅，只能拿「最新一個交易日」，不支援指定日期。
    回傳 (該筆資料實際對應的交易日 ISO 字串, rows)。
    """
    session = make_session()
    resp = session.get(TPEX_QUOTE_URL, timeout=30)
    resp.raise_for_status()
    payload = resp.json()
    if not payload:
        return None, []

    trade_date = _roc_to_iso(payload[0].get("Date"))
    rows = []
    for r in payload:
        code = (r.get("SecuritiesCompanyCode") or "").strip()
        close = to_float(r.get("Close"))
        change_price = to_float(r.get("Change"))
        if not code or close is None:
            continue
        if change_price is None:
            change_price = 0.0
        prev_close = close - change_price
        change_pct = round(change_price / prev_close * 100, 2) if prev_close else None
        rows.append({"code": code, "close": close, "change_pct": change_pct})
    return trade_date, rows
