"""台指選擇權 (TXO) 資料抓取主流程：呼叫期交所 scraper、寫入 SQLite。"""

import logging
from datetime import date, datetime, timedelta

from app.db import init_db, replace_txo_daily, session
from app.scrapers.taifex_txo import fetch_range, group_by_date

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("fetch_txo")


def _store(grouped: dict[str, list[dict]]) -> dict[str, int]:
    fetched_at = datetime.now().isoformat(timespec="seconds")
    result = {}
    with session() as conn:
        for trade_date, rows in grouped.items():
            for r in rows:
                r["fetched_at"] = fetched_at
            replace_txo_daily(conn, trade_date, rows)
            result[trade_date] = len(rows)
    return result


def backfill(start: date, end: date) -> dict[str, int]:
    """一次補齊 start~end (含) 區間的 TXO 資料 (期交所限制單次查詢區間不超過一個月)。"""
    init_db()
    log.info("回溯下載 TXO 資料 %s ~ %s ...", start, end)
    rows = fetch_range(start, end)
    grouped = group_by_date(rows)
    result = _store(grouped)
    log.info("TXO 回溯完成，共 %d 個交易日：%s", len(result), result)
    return result


def run_latest(lookback_days: int = 5) -> dict[str, int]:
    """排程用：抓最近 lookback_days 天 (含今天) 的資料並覆寫。
    抓一個小區間而不是只抓當天，是為了在排程偶爾漏跑、或期交所資料延後公布時，
    下一次執行仍能自動補齊，不需要人工介入重跑。
    """
    init_db()
    end = date.today()
    start = end - timedelta(days=lookback_days)
    log.info("抓取 TXO 最新資料 %s ~ %s ...", start, end)
    rows = fetch_range(start, end)
    grouped = group_by_date(rows)
    result = _store(grouped)
    log.info("TXO 更新完成：%s", result if result else "區間內無交易日資料 (可能遇假日)")
    return result


if __name__ == "__main__":
    import json

    print(json.dumps(run_latest(), ensure_ascii=False, indent=2))
