"""主動 ETF 歷史資料回補：補上資料庫裡缺的交易日，並可指定區間「先刪後重抓」修正日期錯位的列。

四家投信的 PCF API 都支援指定查詢日期，但「查詢日」是公告日：回傳的是查詢日之前某個
交易日(統一/復華/野村/群益多為前一個交易日，統一全球 00988A 因海外資產估值時差落後兩個
交易日)的資料，真正的資料日期以回應裡的日期欄位為準。所以回補「交易日 T」的做法是：
從 T 之後的交易日依序當查詢日，直到回應的資料日期剛好等於 T 為止。

用法：
    python scripts/backfill_etf.py                                   # 補 2026-01-02 ~ 最新之間所有缺的交易日
    python scripts/backfill_etf.py --tickers 00981A --start 2026-08-01
    python scripts/backfill_etf.py --replace 00981A,00403A:2026-08-25   # 該區間先刪掉再重抓(修正日期錯位)
    python scripts/backfill_etf.py --replace 00985A:2026-01-15..2026-01-15
    python scripts/backfill_etf.py --dry-run                         # 只列出會補哪些日期，不寫入

交易日曆：用「至少 50 檔個股有收盤價」的日期(台股實際開盤日)，另外併入期貨/選擇權/
法人現貨資料表出現過的交易日。國定假日不在日曆內，不會被當成缺口。
"""

import argparse
import os
import shutil
import sys
import tempfile
import time
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import DB_PATH, RAW_DIR, WATCHLIST, WATCHLIST_BY_TICKER  # noqa: E402
from app.db import get_connection, init_db, replace_holdings, session, upsert_fund, upsert_summary  # noqa: E402
from app.fetch import _save_raw  # noqa: E402
from app.scrapers import fetch_for  # noqa: E402

DEFAULT_START = "2026-01-02"
REQUEST_GAP_SECONDS = 0.25
MAX_QUERY_TRIES = 5  # 每個目標交易日最多往後試幾個查詢日


def trading_calendar() -> list[str]:
    conn = get_connection()
    try:
        days = {r[0] for r in conn.execute("SELECT date FROM stock_prices GROUP BY date HAVING COUNT(*) >= 50")}
        for tbl in ("futures_daily", "txo_daily", "institutional_stock_daily"):
            days |= {r[0] for r in conn.execute(f"SELECT DISTINCT trade_date FROM {tbl}")}
        return sorted(days)
    finally:
        conn.close()


def _call(issuer: str, fund_code: str, query_date: date) -> dict | None:
    for attempt in range(3):
        try:
            time.sleep(REQUEST_GAP_SECONDS)
            return fetch_for(issuer, fund_code, query_date)
        except Exception as exc:  # noqa: BLE001 - 網路逾時等，重試後仍失敗就當這個查詢日沒資料
            if attempt == 2:
                print(f"    查詢 {query_date} 失敗: {str(exc)[:80]}")
            time.sleep(1.5)
    return None


def fetch_trade_day(etf, target: str, cal: list[str]) -> dict | None:
    """回傳資料日期剛好等於 target 的結果；查不到回傳 None。"""
    later = [d for d in cal if d > target][:MAX_QUERY_TRIES]
    # 最新的交易日之後還沒有「之後的交易日」可當查詢日：用未來的平日日期查(API 允許，回傳最新一期)
    probe = date.fromisoformat(later[-1] if later else target)
    while len(later) < 3:
        probe += timedelta(days=1)
        if probe.weekday() < 5:
            later.append(probe.isoformat())
    for q in later:
        result = _call(etf.issuer_code, etf.fund_code, date.fromisoformat(q))
        if not result:
            continue
        got = result.get("date")
        if got == target and result.get("nav_per_unit") is not None:
            return result
        if got and got > target:
            return None  # 已經查過頭，這個交易日來源端沒有資料
    return None


def parse_replace(specs: list[str]) -> dict[str, list[tuple[str, str]]]:
    out: dict[str, list[tuple[str, str]]] = {}
    for spec in specs or []:
        tickers, _, rng = spec.partition(":")
        lo, _, hi = rng.partition("..")
        lo, hi = lo or DEFAULT_START, hi or "9999-12-31"
        for t in tickers.split(","):
            if t not in WATCHLIST_BY_TICKER:
                raise SystemExit(f"未知的 ETF 代號: {t}")
            out.setdefault(t, []).append((lo, hi))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default=DEFAULT_START)
    ap.add_argument("--end", default=None)
    ap.add_argument("--tickers", default=None, help="逗號分隔，預設全部")
    ap.add_argument("--replace", action="append", help="TICKER[,TICKER]:FROM[..TO]  該區間先刪除再重抓")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    init_db()
    cal = trading_calendar()
    end = args.end or cal[-1]
    replace = parse_replace(args.replace)
    tickers = args.tickers.split(",") if args.tickers else [e.ticker for e in WATCHLIST]

    conn = get_connection()
    plan: dict[str, list[str]] = {}
    for t in tickers:
        have = {r[0] for r in conn.execute("SELECT date FROM daily_summary WHERE ticker = ?", (t,))}
        for lo, hi in replace.get(t, []):
            have = {d for d in have if not (lo <= d <= hi)}
        plan[t] = [d for d in cal if args.start <= d <= end and d not in have]
    conn.close()

    for t, days in plan.items():
        print(f"{t}: 需回補 {len(days)} 天 {days if len(days) <= 12 else str(days[:6]) + ' ... ' + str(days[-3:])}")
    if args.dry_run:
        return 0

    # 備份放在 data/ 之外，避免被 `git add data/` 連同幾十 MB 的備份檔一起 commit
    backup = os.path.join(tempfile.gettempdir(), f"etf.db.bak-{datetime.now():%Y%m%d%H%M%S}")
    shutil.copy2(DB_PATH, backup)
    print(f"已備份資料庫到 {backup}")

    fetched_at = datetime.now().isoformat(timespec="seconds")
    failed: dict[str, list[str]] = {}
    for t, days in plan.items():
        etf = WATCHLIST_BY_TICKER[t]
        results: dict[str, dict] = {}
        for d in days:
            r = fetch_trade_day(etf, d, cal)
            if r is None:
                failed.setdefault(t, []).append(d)
            else:
                results[d] = r
        with session() as db:
            upsert_fund(db, t, etf.name, etf.issuer)
            for lo, hi in replace.get(t, []):
                db.execute("DELETE FROM holdings WHERE ticker = ? AND date BETWEEN ? AND ?", (t, lo, hi))
                db.execute("DELETE FROM daily_summary WHERE ticker = ? AND date BETWEEN ? AND ?", (t, lo, hi))
                raw_dir = os.path.join(RAW_DIR, t)
                if os.path.isdir(raw_dir):
                    for fn in os.listdir(raw_dir):
                        if lo <= fn[:-5] <= hi:
                            os.remove(os.path.join(raw_dir, fn))
            for d, r in results.items():
                upsert_summary(db, {
                    "ticker": t, "date": d,
                    "nav_per_unit": r.get("nav_per_unit"), "total_units": r.get("total_units"),
                    "units_diff": r.get("units_diff"), "net_asset_value": r.get("net_asset_value"),
                    "beneficiaries_count": r.get("beneficiaries_count"), "fetched_at": fetched_at,
                })
                replace_holdings(db, t, d, r.get("holdings", []))
                _save_raw(t, d, r.get("raw", {}))
        print(f"{t}: 成功 {len(results)}/{len(days)}" + (f"，失敗 {failed[t]}" if t in failed else ""))

    if failed:
        print("\n以下日期來源端查不到(可能該投信本來就沒有公布)：")
        for t, ds in failed.items():
            print(f"  {t}: {ds}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
