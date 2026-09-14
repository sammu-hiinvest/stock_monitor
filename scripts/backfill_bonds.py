"""債市 (美債殖利率曲線/利差/公司債利差/5Y5Y遠期通膨預期) 歷史資料一次性回溯下載。
資料源 FRED 沒有查詢區間上限，預設直接抓最近 2 年，足夠算 YTD 變化與畫較長的走勢圖。

用法：
    python scripts/backfill_bonds.py                      # 預設回溯最近 2 年
    python scripts/backfill_bonds.py 2024-01-01 2026-09-07 # 自訂區間
"""

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.fetch_bonds import DEFAULT_BACKFILL_DAYS, backfill  # noqa: E402

if __name__ == "__main__":
    if len(sys.argv) >= 3:
        start = date.fromisoformat(sys.argv[1])
        end = date.fromisoformat(sys.argv[2])
    else:
        end = date.today()
        start = end - timedelta(days=DEFAULT_BACKFILL_DAYS)
    backfill(start, end)
