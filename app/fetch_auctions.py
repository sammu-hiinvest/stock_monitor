"""美債標售 (10年期公債Note／30年期公債Bond) 資料抓取主流程。

資料源是美國財政部 FiscalData 公開 API「Treasury Securities Auctions Data」，
不需要金鑰：
    https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/od/auctions_query
    (資料集頁面：https://fiscaldata.treasury.gov/datasets/treasury-securities-auctions-data/treasury-securities-auctions-data)

用 `original_security_term`(而不是 `security_term`) 篩選 10年期／30年期，因為
「加碼發行(reopening)」的標售在 `security_term` 會顯示成「9-Year 10-Month」這種
剩餘年期字串，只有 `original_security_term` 在整個發行週期(含加碼)都會穩定顯示
「10-Year」／「30-Year」，是正確的篩選欄位(已用實際資料驗證過)。

**Bid-to-cover 比率是自己算的**，不是直接拿 API 的 `bid_to_cover_ratio` 欄位：
用 `total_tendered`(總投標金額) / `total_accepted`(實際得標金額)。已跟 API
本身提供的 `bid_to_cover_ratio` 欄位交叉比對過，兩者數值一致，這裡仍選擇自己
算是為了計算方式透明、不依賴上游欄位是否每一筆都有填值(較舊的標售資料這個
欄位常是 null)。

存進跟其他債市資料共用的 `bond_series` 表，每個年期各拆成「標售金額」跟
「bid-to-cover比率」兩條序列 (`AUCTION_10Y_AMOUNT`/`AUCTION_10Y_BTC`、
`AUCTION_30Y_AMOUNT`/`AUCTION_30Y_BTC`)，讓前端可以直接沿用既有的通用端點
`/api/bonds/series?ids=...`，不需要新增專屬 API。標售金額存成「十億美元」
(除以 1e9)方便圖表顯示，不存原始美元數字。
"""

import logging
from datetime import date, datetime, timedelta

import requests

from app.db import init_db, session, upsert_bond_series

log = logging.getLogger("fetch_auctions")

AUCTIONS_URL = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/od/auctions_query"

# key -> (security_type, original_security_term, 標售金額序列代碼, BTC比率序列代碼)
AUCTION_TERMS: dict[str, tuple[str, str, str, str]] = {
    "10Y_NOTE": ("Note", "10-Year", "AUCTION_10Y_AMOUNT", "AUCTION_10Y_BTC"),
    "30Y_BOND": ("Bond", "30-Year", "AUCTION_30Y_AMOUNT", "AUCTION_30Y_BTC"),
}

# 只保留最近這麼多天的標售資料，跟 ACM Term Premium 圖表一樣至少涵蓋10年(10年+1年緩衝)。
KEEP_DAYS = 365 * 11


def _fetch_term(security_type: str, original_term: str, start: date) -> list[dict]:
    """分頁抓完某個年期的全部標售紀錄(通常一年期一次 API 查詢就抓得完，這裡保留
    分頁邏輯是為了保險，避免哪天資料量變多超過單頁上限時默默漏資料)。
    """
    rows: list[dict] = []
    page = 1
    while True:
        resp = requests.get(
            AUCTIONS_URL,
            params={
                "filter": (
                    f"security_type:eq:{security_type},"
                    f"original_security_term:eq:{original_term},"
                    f"auction_date:gte:{start.isoformat()}"
                ),
                "fields": "auction_date,total_accepted,total_tendered",
                "sort": "auction_date",
                "page[number]": page,
                "page[size]": 200,
            },
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        rows.extend(payload["data"])
        if page >= payload["meta"]["total-pages"]:
            break
        page += 1
    return rows


def _process(security_type: str, original_term: str, start: date) -> tuple[list[dict], list[dict]]:
    raw = _fetch_term(security_type, original_term, start)
    amount_rows, btc_rows = [], []
    for r in raw:
        obs_date = r["auction_date"]
        accepted = r.get("total_accepted")
        tendered = r.get("total_tendered")
        if accepted in (None, "null", ""):
            continue
        accepted = float(accepted)
        amount_rows.append({"obs_date": obs_date, "value": round(accepted / 1e9, 3)})
        if tendered not in (None, "null", "") and accepted:
            btc_rows.append({"obs_date": obs_date, "value": round(float(tendered) / accepted, 3)})
    return amount_rows, btc_rows


def run_latest() -> dict:
    """排程/手動用：FiscalData API 沒有增量端點，每次都整段區間重抓並覆寫。"""
    init_db()
    start = date.today() - timedelta(days=KEEP_DAYS)
    fetched_at = datetime.now().isoformat(timespec="seconds")
    result = {}
    with session() as conn:
        for key, (sec_type, term, amount_sid, btc_sid) in AUCTION_TERMS.items():
            log.info("下載美債標售資料 %s (%s / %s) ...", key, sec_type, term)
            amount_rows, btc_rows = _process(sec_type, term, start)
            for r in amount_rows:
                r["fetched_at"] = fetched_at
            for r in btc_rows:
                r["fetched_at"] = fetched_at
            upsert_bond_series(conn, amount_sid, amount_rows)
            upsert_bond_series(conn, btc_sid, btc_rows)
            result[amount_sid] = len(amount_rows)
            result[btc_sid] = len(btc_rows)
    log.info("美債標售資料更新完成：%s", result)
    return result


if __name__ == "__main__":
    import json

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print(json.dumps(run_latest(), ensure_ascii=False, indent=2))
