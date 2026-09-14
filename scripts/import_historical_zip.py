"""把歷史資料 ZIP（如 Active_ETFs_All_Data.zip）匯入 data/etf.db。

用法：
    python scripts/import_historical_zip.py "C:\\path\\to\\Active_ETFs_All_Data.zip"
    (不帶參數則預設用專案根目錄下的 Active_ETFs_All_Data.zip)

ZIP 內容格式（每個 ETF 兩個檔案）：
  [代號]_[名稱]_歷史淨值與規模.csv   欄位：日期,淨值(NAV),基金規模(Fund Size),發行單位數(Outstanding Units)
  [代號]_[名稱]_歷史持股明細變動.csv 欄位：日期,股票代號,股票名稱,持股股數(千股/單位),持股權重
還有一個 最新持股明細_latest_holdings/ 資料夾（單日快照，與歷史檔重複，這裡不匯入）。

重要：資料日期定義的對齊問題
----------------------------
ZIP 裡的「日期」欄位是各投信「公告日」，公告當天揭露的其實是前一個交易日收盤的
資料（多數投信約在 14:45~16:30 後才更新為當天自己的收盤資料）。而本專案的即時爬蟲
(app/scrapers/*.py) 存進 DB 的 date 是資料實際反映的交易基準日 (trade date)，
不是公告日，兩者要對齊才能跟即時爬到的資料接在同一個時間軸上。

做法：不用曆法去猜「前一個營業日」(會被國定假日、連假打亂)，而是直接用 ZIP 本身
「連續公告」的順序來推：第 i 列資料，其交易基準日 = 第 (i-1) 列的公告日期。
也就是說每一列的日期整體往前平移一格。這個假設已經用 00981A、00982A 的資料實際比對
即時爬蟲抓到的數值驗證過（NAV 完全一致）。平移後最早一列 (2026-01-02 的公告) 沒有
更早一列可用，因此該列會被捨棄（每檔 ETF 只損失最早一天的資料）。

匯入策略：只補資料庫裡還沒有的 (ticker, date)，不會覆蓋既有的即時爬蟲資料
（即時爬蟲資料視為權威來源）。
"""

import csv
import io
import re
import sys
import zipfile
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import WATCHLIST_BY_TICKER  # noqa: E402
from app.db import get_connection, init_db, replace_holdings, session, upsert_fund, upsert_summary  # noqa: E402

DEFAULT_ZIP = PROJECT_ROOT / "Active_ETFs_All_Data.zip"
TICKER_RE = re.compile(r"\d{5}[A-Z]?")


def parse_fund_size(raw: str) -> float | None:
    if not raw:
        return None
    raw = raw.strip()
    mult = 1.0
    if raw.endswith("B"):
        mult, raw = 1e9, raw[:-1]
    elif raw.endswith("M"):
        mult, raw = 1e6, raw[:-1]
    elif raw.endswith("K"):
        mult, raw = 1e3, raw[:-1]
    raw = raw.replace(",", "")
    try:
        return float(raw) * mult
    except ValueError:
        return None


def parse_number(raw: str) -> float | None:
    if raw is None:
        return None
    raw = raw.replace(",", "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def parse_pct(raw: str) -> float | None:
    if raw is None:
        return None
    raw = raw.replace("%", "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def read_csv_rows(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> list[dict]:
    with zf.open(info) as f:
        text = f.read().decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def classify_entry(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> tuple[str, str] | None:
    """回傳 (ticker, kind)，kind 為 'nav' / 'holdings_hist' / 'skip'。
    投信官網 ZIP 檔名編碼常在跨平台解壓縮時損毀，因此不依賴檔名字串比對內容，
    只靠檔名中還能辨識的 ticker 代碼 + CSV 表頭欄位來判斷種類。
    """
    if info.is_dir():
        return None
    if info.filename.lower().endswith("readme.txt"):
        return None

    m = TICKER_RE.search(info.filename)
    if not m:
        return None
    ticker = m.group(0)
    if ticker not in WATCHLIST_BY_TICKER:
        return None

    if "latest_holdings" in info.filename or "latest" in info.filename.lower():
        return (ticker, "skip")  # 與歷史檔重複的單日快照，略過

    with zf.open(info) as f:
        header = f.readline().decode("utf-8-sig", errors="ignore")
    if "淨值" in header or "NAV" in header:
        return (ticker, "nav")
    if "股票代號" in header:
        return (ticker, "holdings_hist")
    return (ticker, "skip")


def import_zip(zip_path: Path) -> None:
    init_db()
    zf = zipfile.ZipFile(zip_path)

    nav_rows_by_ticker: dict[str, list[dict]] = {}
    holdings_rows_by_ticker: dict[str, list[dict]] = {}

    for info in zf.infolist():
        classified = classify_entry(zf, info)
        if not classified:
            continue
        ticker, kind = classified
        if kind == "nav":
            nav_rows_by_ticker[ticker] = read_csv_rows(zf, info)
        elif kind == "holdings_hist":
            holdings_rows_by_ticker[ticker] = read_csv_rows(zf, info)

    fetched_at = datetime.now().isoformat(timespec="seconds")

    with session() as conn:
        for ticker, etf in WATCHLIST_BY_TICKER.items():
            nav_rows = nav_rows_by_ticker.get(ticker)
            if not nav_rows:
                print(f"[skip] {ticker}: ZIP 內找不到歷史淨值資料")
                continue

            nav_rows.sort(key=lambda r: r["日期"])
            announce_dates = [r["日期"] for r in nav_rows]
            # 平移一格：第 i 筆資料的交易基準日 = 第 i-1 筆的公告日期
            trade_date_of = {announce_dates[i]: announce_dates[i - 1] for i in range(1, len(announce_dates))}

            upsert_fund(conn, ticker, etf.name, etf.issuer)

            existing_dates = {
                r["date"]
                for r in conn.execute("SELECT date FROM daily_summary WHERE ticker = ?", (ticker,)).fetchall()
            }

            inserted, skipped_existing = 0, 0
            prev_units = None
            for row in nav_rows:
                announce = row["日期"]
                total_units = parse_number(row.get("發行單位數 (Outstanding Units)"))
                trade_date = trade_date_of.get(announce)
                if trade_date is None:
                    prev_units = total_units
                    continue  # 最早一筆，沒有更早的公告日可平移，捨棄
                if trade_date in existing_dates:
                    skipped_existing += 1
                    prev_units = total_units
                    continue

                units_diff = None
                if prev_units is not None and total_units is not None:
                    units_diff = total_units - prev_units

                upsert_summary(
                    conn,
                    {
                        "ticker": ticker,
                        "date": trade_date,
                        "nav_per_unit": parse_number(row.get("淨值 (NAV)")),
                        "total_units": total_units,
                        "units_diff": units_diff,
                        "net_asset_value": parse_fund_size(row.get("基金規模 (Fund Size)")),
                        "beneficiaries_count": None,
                        "fetched_at": fetched_at,
                    },
                )
                inserted += 1
                prev_units = total_units

            # 持股明細：依公告日期分組後，同樣平移一格對齊到交易基準日
            holdings_rows = holdings_rows_by_ticker.get(ticker, [])
            by_announce: dict[str, list[dict]] = {}
            for row in holdings_rows:
                by_announce.setdefault(row["日期"], []).append(row)

            holdings_inserted = 0
            for announce, rows in by_announce.items():
                trade_date = trade_date_of.get(announce)
                if trade_date is None or trade_date in existing_dates:
                    continue
                holdings = [
                    {
                        "asset_type": "STOCK",
                        "code": (r.get("股票代號") or "").strip(),
                        "name": (r.get("股票名稱") or "").strip(),
                        "shares": (parse_number(r.get("持股股數 (千股/單位)")) or 0) * 1000,
                        "weight_pct": parse_pct(r.get("持股權重")),
                        "market_value": None,
                    }
                    for r in rows
                ]
                replace_holdings(conn, ticker, trade_date, holdings)
                holdings_inserted += 1

            print(
                f"[ok] {ticker} ({etf.name}): 新增 {inserted} 天淨值資料"
                f"（略過 {skipped_existing} 天既有資料）、{holdings_inserted} 天持股明細"
            )


if __name__ == "__main__":
    zip_arg = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_ZIP
    if not zip_arg.exists():
        print(f"找不到檔案: {zip_arg}")
        sys.exit(1)
    import_zip(zip_arg)
