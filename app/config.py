"""
追蹤標的設定 (Watchlist)
每檔 ETF 對應到發行投信的內部代碼，供 scraper 呼叫各投信官網 API 使用。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class EtfConfig:
    ticker: str        # 股票代號，如 00981A
    name: str          # 中文簡稱
    issuer: str        # 投信公司
    issuer_code: str   # 內部使用：對應 app/scrapers/<issuer_code>.py 的 scraper
    fund_code: str      # 該投信網站用來識別此基金的代碼


WATCHLIST: list[EtfConfig] = [
    EtfConfig("00981A", "主動統一台股增長", "統一投信", "uni", "49YTW"),
    EtfConfig("00403A", "主動統一升級50", "統一投信", "uni", "63YTW"),
    EtfConfig("00988A", "主動統一全球創新", "統一投信", "uni", "61YTW"),
    EtfConfig("00991A", "主動復華未來50", "復華投信", "fuhwa", "ETF23"),
    EtfConfig("00985A", "主動野村台灣50", "野村投信", "nomura", "00985A"),
    EtfConfig("00980A", "主動野村台灣優選", "野村投信", "nomura", "00980A"),
    EtfConfig("00982A", "主動群益台灣強棒", "群益投信", "capital", "399"),
    EtfConfig("00992A", "主動群益科技創新", "群益投信", "capital", "500"),
]

WATCHLIST_BY_TICKER: dict[str, EtfConfig] = {e.ticker: e for e in WATCHLIST}

# 各投信官網共用的瀏覽器 UA，避免被當成 bot 擋下
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}

import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
RAW_DIR = os.path.join(DATA_DIR, "raw")
DB_PATH = os.path.join(DATA_DIR, "etf.db")
