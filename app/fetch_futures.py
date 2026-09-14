"""大盤期貨資料抓取主流程：期貨每日行情 (TX/MTX/TMF) + 三大法人期貨交易報告。"""

import logging
from datetime import date, datetime, timedelta

from app.db import init_db, replace_futures_daily, replace_institutional_futures, session
from app.scrapers.taifex_futures import fetch_all_products_range as fetch_futures_range
from app.scrapers.taifex_futures import group_by_date as group_futures_by_date
from app.scrapers.taifex_institutional import fetch_all_products_range as fetch_inst_range
from app.scrapers.taifex_institutional import group_by_date as group_inst_by_date

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("fetch_futures")


def _store(grouped_futures: dict, grouped_inst: dict) -> dict:
    fetched_at = datetime.now().isoformat(timespec="seconds")
    result = {}
    with session() as conn:
        for trade_date in sorted(set(grouped_futures) | set(grouped_inst)):
            frows = grouped_futures.get(trade_date, [])
            irows = grouped_inst.get(trade_date, [])
            for r in frows:
                r["fetched_at"] = fetched_at
            for r in irows:
                r["fetched_at"] = fetched_at
            replace_futures_daily(conn, trade_date, frows)
            replace_institutional_futures(conn, trade_date, irows)
            result[trade_date] = {"futures_rows": len(frows), "institutional_rows": len(irows)}
    return result


def backfill(start: date, end: date) -> dict:
    """一次補齊 start~end (含) 區間的大盤期貨資料。"""
    init_db()
    log.info("回溯下載大盤期貨資料 %s ~ %s ...", start, end)
    futures_rows = fetch_futures_range(start, end)
    inst_rows = fetch_inst_range(start, end)
    result = _store(group_futures_by_date(futures_rows), group_inst_by_date(inst_rows))
    log.info("大盤期貨回溯完成，共 %d 個交易日：%s", len(result), result)
    return result


def run_latest(lookback_days: int = 5) -> dict:
    """排程用：抓最近 lookback_days 天 (含今天) 的資料並覆寫。"""
    init_db()
    end = date.today()
    start = end - timedelta(days=lookback_days)
    log.info("抓取大盤期貨最新資料 %s ~ %s ...", start, end)
    futures_rows = fetch_futures_range(start, end)
    inst_rows = fetch_inst_range(start, end)
    result = _store(group_futures_by_date(futures_rows), group_inst_by_date(inst_rows))
    log.info("大盤期貨更新完成：%s", result if result else "區間內無交易日資料 (可能遇假日)")
    return result


if __name__ == "__main__":
    import json

    print(json.dumps(run_latest(), ensure_ascii=False, indent=2))
