"""法人現貨(個股三大法人買賣超 + 股本)每日抓取主流程。

上市(TWSE T86)可以指定日期回溯查詢，所以用「回溯 lookback_days 個日曆日、逐日嘗試」
的方式，非交易日自然拿到空清單直接跳過(不是錯誤)。上櫃(TPEx)的來源端點只能拿到
「最新一個交易日」，沒辦法回溯，所以每次執行只抓得到一天，歷史深度要靠每天執行
排程慢慢累積。

股本(已發行股數)資料變動不頻繁，兩邊都整包重抓、用 upsert 覆寫最新版本。
"""

import logging
from datetime import date, datetime, timedelta

from app.db import (
    init_db,
    replace_institutional_stock_daily,
    session,
    upsert_shares_outstanding,
)
from app.scrapers.institutional_stock import (
    fetch_tpex_daily,
    fetch_tpex_shares_outstanding,
    fetch_twse_shares_outstanding,
    fetch_twse_t86,
)

log = logging.getLogger("fetch_institutional_stock")

DEFAULT_LOOKBACK_DAYS = 5  # 足夠涵蓋週末/連假，抓到最近2個交易日就夠算比重排行


def run_latest(lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> dict:
    init_db()
    fetched_at = datetime.now().isoformat(timespec="seconds")
    result: dict = {}

    # --- 上市(TWSE)：回溯 lookback_days 天，逐日嘗試，非交易日跳過 ---
    twse_days_fetched = []
    with session() as conn:
        for offset in range(lookback_days):
            d = date.today() - timedelta(days=offset)
            try:
                rows = fetch_twse_t86(d)
            except Exception as exc:  # noqa: BLE001 - 單日失敗不影響其他天
                log.exception("TWSE T86 %s 抓取失敗: %s", d, exc)
                continue
            if not rows:
                continue
            for r in rows:
                r["fetched_at"] = fetched_at
            replace_institutional_stock_daily(conn, d.isoformat(), "TWSE", rows)
            twse_days_fetched.append(d.isoformat())
    result["twse_days"] = twse_days_fetched

    # --- 上櫃(TPEx)：只能拿最新一個交易日 ---
    try:
        tpex_date, tpex_rows = fetch_tpex_daily()
        if tpex_date and tpex_rows:
            for r in tpex_rows:
                r["fetched_at"] = fetched_at
            with session() as conn:
                replace_institutional_stock_daily(conn, tpex_date, "TPEX", tpex_rows)
            result["tpex_date"] = tpex_date
            result["tpex_rows"] = len(tpex_rows)
        else:
            result["tpex_date"] = None
    except Exception as exc:  # noqa: BLE001
        log.exception("TPEx 三大法人個股買賣超抓取失敗: %s", exc)
        result["tpex_error"] = str(exc)

    # --- 股本(已發行股數)：兩邊整包重抓覆寫 ---
    shares_rows = []
    try:
        shares_rows.extend(fetch_twse_shares_outstanding())
    except Exception as exc:  # noqa: BLE001
        log.exception("TWSE 股本資料抓取失敗: %s", exc)
    try:
        shares_rows.extend(fetch_tpex_shares_outstanding())
    except Exception as exc:  # noqa: BLE001
        log.exception("TPEx 股本資料抓取失敗: %s", exc)

    if shares_rows:
        for r in shares_rows:
            r["fetched_at"] = fetched_at
        with session() as conn:
            upsert_shares_outstanding(conn, shares_rows)
    result["shares_outstanding_rows"] = len(shares_rows)

    log.info("法人現貨資料更新完成：%s", result)
    return result


if __name__ == "__main__":
    import json

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print(json.dumps(run_latest(), ensure_ascii=False, indent=2))
