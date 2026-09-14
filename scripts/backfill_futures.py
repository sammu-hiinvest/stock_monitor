"""大盤期貨 (TX/MTX/TMF + 三大法人) 歷史資料一次性回溯下載。

用法：
    python scripts/backfill_futures.py                      # 預設回溯 2026/09/01 ~ 2026/09/04
    python scripts/backfill_futures.py 2026-08-01 2026-08-31 # 自訂區間
"""

import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.fetch_futures import backfill  # noqa: E402

DEFAULT_START = date(2026, 9, 1)
DEFAULT_END = date(2026, 9, 4)

if __name__ == "__main__":
    if len(sys.argv) >= 3:
        start = date.fromisoformat(sys.argv[1])
        end = date.fromisoformat(sys.argv[2])
    else:
        start, end = DEFAULT_START, DEFAULT_END
    backfill(start, end)
