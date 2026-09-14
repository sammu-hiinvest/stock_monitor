"""排程/手動執行的進入點。

用法：
    python scripts/fetch_daily.py

會抓取 app/config.py 的 WATCHLIST 內所有 ETF 的最新申購買回清單，寫入 data/etf.db，
並把每檔基金每日原始回應備份到 data/raw/；同時也會抓取台指選擇權(TXO)、大盤期貨
(TX/MTX/TMF + 三大法人)、債市(美債殖利率曲線/利差/公司債利差/泰勒法則/PCE通膨)、
Atlanta Fed Market Probability Tracker(政策利率升息/降息機率)、美國CPI(BLS官方
API)最近的資料
(見 app/fetch_txo.py、app/fetch_futures.py、app/fetch_bonds.py、app/fetch_mpt.py、
app/fetch_cpi.py)，一次排程涵蓋六種資料。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.fetch import run_all  # noqa: E402
from app.fetch_acm import run_latest as run_acm_latest  # noqa: E402
from app.fetch_auctions import run_latest as run_auctions_latest  # noqa: E402
from app.fetch_bonds import run_latest as run_bonds_latest  # noqa: E402
from app.fetch_cpi import run_latest as run_cpi_latest  # noqa: E402
from app.fetch_futures import run_latest as run_futures_latest  # noqa: E402
from app.fetch_macro import run_latest as run_macro_latest  # noqa: E402
from app.fetch_mpt import run_latest as run_mpt_latest  # noqa: E402
from app.fetch_txo import run_latest as run_txo_latest  # noqa: E402

if __name__ == "__main__":
    run_all()
    try:
        run_txo_latest()
    except Exception as exc:  # noqa: BLE001 - TXO 抓取失敗不影響其他資料已經寫入
        print(f"TXO 抓取失敗: {exc}")
    try:
        run_futures_latest()
    except Exception as exc:  # noqa: BLE001 - 大盤期貨抓取失敗不影響其他資料已經寫入
        print(f"大盤期貨抓取失敗: {exc}")
    try:
        run_bonds_latest()
    except Exception as exc:  # noqa: BLE001 - 債市抓取失敗不影響其他資料已經寫入
        print(f"債市抓取失敗: {exc}")
    try:
        run_mpt_latest()
    except Exception as exc:  # noqa: BLE001 - Atlanta Fed MPT 抓取失敗不影響其他資料已經寫入
        print(f"Atlanta Fed 政策利率機率抓取失敗: {exc}")
    try:
        run_cpi_latest()
    except Exception as exc:  # noqa: BLE001 - CPI 抓取失敗不影響其他資料已經寫入
        print(f"美國CPI(BLS) 抓取失敗: {exc}")
    try:
        run_macro_latest()
    except Exception as exc:  # noqa: BLE001 - 總經(PPI/非農/家戶調查) 抓取失敗不影響其他資料已經寫入
        print(f"美國總經數據(BLS) 抓取失敗: {exc}")
    try:
        run_acm_latest()
    except Exception as exc:  # noqa: BLE001 - ACM Term Premium 抓取失敗不影響其他資料已經寫入
        print(f"NY Fed ACM Term Premium 抓取失敗: {exc}")
    try:
        run_auctions_latest()
    except Exception as exc:  # noqa: BLE001 - 美債標售資料抓取失敗不影響其他資料已經寫入
        print(f"美債標售資料抓取失敗: {exc}")
