"""
SQLite 存取層。資料庫只有兩張主要表格：
- daily_summary：每檔基金、每個交易日一筆的淨值/規模摘要
- holdings：每檔基金、每個交易日的持股明細 (股票/期貨/其他資產)
"""

import os
import sqlite3
from contextlib import contextmanager

from app.config import DB_PATH, DATA_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS funds (
    ticker TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    issuer TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_summary (
    ticker TEXT NOT NULL,
    date TEXT NOT NULL,               -- 基準日 (YYYY-MM-DD)，即持股與淨值的資料日期
    nav_per_unit REAL,                -- 每受益權單位淨資產價值
    total_units REAL,                 -- 已發行受益權單位總數
    units_diff REAL,                  -- 與前日已發行單位差異數
    net_asset_value REAL,             -- 基金淨資產價值(元)
    beneficiaries_count REAL,         -- 受益人數 (若有提供)
    fetched_at TEXT NOT NULL,         -- 抓取時間 (ISO datetime)
    PRIMARY KEY (ticker, date)
);

CREATE TABLE IF NOT EXISTS holdings (
    ticker TEXT NOT NULL,
    date TEXT NOT NULL,
    asset_type TEXT NOT NULL,         -- STOCK / FUTURES / BOND / ETF / CASH / OTHER
    code TEXT NOT NULL,
    name TEXT,
    shares REAL,
    weight_pct REAL,
    market_value REAL,
    PRIMARY KEY (ticker, date, asset_type, code)
);

CREATE INDEX IF NOT EXISTS idx_holdings_ticker_date ON holdings(ticker, date);
CREATE INDEX IF NOT EXISTS idx_summary_ticker ON daily_summary(ticker);
CREATE INDEX IF NOT EXISTS idx_holdings_code ON holdings(code, asset_type);

-- 個股收盤價快取 (跨 ETF 個股比較頁用)，來源可能是 TWSE(上市) 或 TPEx(上櫃)
CREATE TABLE IF NOT EXISTS stock_prices (
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    close REAL,
    source TEXT,                      -- TWSE / TPEX
    PRIMARY KEY (code, date)
);

-- 記錄哪些「股票代碼+年月」已經查過(不論有沒有資料)，避免對查無資料的代碼
-- (例如海外股票代碼) 每次都重打一次外部 API
CREATE TABLE IF NOT EXISTS stock_price_fetch_log (
    code TEXT NOT NULL,
    year_month TEXT NOT NULL,          -- YYYY-MM
    found INTEGER NOT NULL,            -- 1 = 有查到資料, 0 = 查無 (可能非台股上市櫃)
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (code, year_month)
);

-- 台指選擇權 (TXO) 每日交易行情，來源：期交所「選擇權每日交易行情下載」
CREATE TABLE IF NOT EXISTS txo_daily (
    trade_date TEXT NOT NULL,          -- 交易日期 YYYY-MM-DD
    contract_month TEXT NOT NULL,      -- 到期月份(週別)，如 202609 / 202609W1
    strike_price REAL NOT NULL,        -- 履約價
    option_type TEXT NOT NULL,         -- C=買權 / P=賣權
    session TEXT NOT NULL,             -- REGULAR=一般 / AFTERHOURS=盤後
    open_price REAL,
    high_price REAL,
    low_price REAL,
    close_price REAL,
    volume REAL,                       -- 成交量
    settlement_price REAL,             -- 結算價
    open_interest REAL,                -- 未沖銷契約數
    change_price REAL,
    change_pct REAL,
    expiry_date TEXT,                  -- 契約到期日 YYYY-MM-DD
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, contract_month, strike_price, option_type, session)
);

CREATE INDEX IF NOT EXISTS idx_txo_date ON txo_daily(trade_date);
CREATE INDEX IF NOT EXISTS idx_txo_date_month ON txo_daily(trade_date, contract_month);

-- 大盤期貨：期貨每日交易行情 (TX 臺股期貨 / MTX 小型臺指 / TMF 微型臺指)，來源：期交所「期貨每日交易行情下載」
CREATE TABLE IF NOT EXISTS futures_daily (
    trade_date TEXT NOT NULL,
    product TEXT NOT NULL,             -- TX / MTX / TMF
    contract_month TEXT NOT NULL,      -- 到期月份(週別)
    session TEXT NOT NULL,             -- REGULAR=一般 / AFTERHOURS=盤後
    open_price REAL,
    high_price REAL,
    low_price REAL,
    close_price REAL,
    change_price REAL,
    change_pct REAL,
    volume REAL,                       -- 成交量
    settlement_price REAL,
    open_interest REAL,                -- 未沖銷契約數
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, product, contract_month, session)
);

CREATE INDEX IF NOT EXISTS idx_futures_date ON futures_daily(trade_date, product);

-- 三大法人期貨交易報告 (區分各期貨契約/依日期)，來源：期交所「三大法人-下載-區分各期貨契約-依日期」
CREATE TABLE IF NOT EXISTS institutional_futures (
    trade_date TEXT NOT NULL,
    product TEXT NOT NULL,             -- TX / MTX / TMF
    investor_type TEXT NOT NULL,       -- DEALER=自營商 / TRUST=投信 / FOREIGN=外資及陸資
    long_volume REAL, long_value REAL,
    short_volume REAL, short_value REAL,
    net_volume REAL, net_value REAL,
    long_oi REAL, long_oi_value REAL,
    short_oi REAL, short_oi_value REAL,
    net_oi REAL, net_oi_value REAL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, product, investor_type)
);

CREATE INDEX IF NOT EXISTS idx_inst_fut_date ON institutional_futures(trade_date, product);

-- 債市：美債殖利率、利差、公司債利差，來源：FRED (聖路易聯邦準備銀行) 公開 CSV，不需 API 金鑰
CREATE TABLE IF NOT EXISTS bond_series (
    series_id TEXT NOT NULL,       -- FRED 序列代碼，如 DGS10 / T10Y2Y / BAMLH0A0HYM2
    obs_date TEXT NOT NULL,        -- 觀測日期 YYYY-MM-DD
    value REAL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (series_id, obs_date)
);

CREATE INDEX IF NOT EXISTS idx_bond_series ON bond_series(series_id, obs_date);

-- Atlanta Fed「Market Probability Tracker」：CME 3個月期SOFR選擇權隱含的政策利率
-- 升息/降息機率，來源是官方公開下載的歷史資料 Excel (非隱藏端點)，只保留精簡的
-- 衍生序列(每個觀測日對應「最近一次到期窗口」的機率)，見 app/fetch_mpt.py 說明。
CREATE TABLE IF NOT EXISTS mpt_probability (
    obs_date TEXT NOT NULL,            -- 觀測日期 YYYY-MM-DD
    reference_meeting TEXT,            -- 對應的CME 3個月SOFR合約參考窗口起始日(近似「最近一次FOMC會議」)
    prob_cut REAL,                     -- 該窗口平均SOFR低於目前目標區間的機率(%)
    prob_hike REAL,                    -- 該窗口平均SOFR高於目前目標區間的機率(%)
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (obs_date)
);

-- 法人現貨：上市(TWSE T86)/上櫃(TPEx tpex_3insti_daily_trading) 三大法人個股買賣超，
-- 官方原始資料只給「股數」，跟 stock_shares_outstanding 的已發行股數一起才能算出
-- 「買超佔股本比重」。TPEx 的來源端點不支援指定日期查詢，只能拿到最新一個交易日，
-- 所以這張表的歷史深度會隨每天排程執行慢慢累積，不像上市可以用 T86 一次回溯多天。
CREATE TABLE IF NOT EXISTS institutional_stock_daily (
    trade_date TEXT NOT NULL,          -- 交易日期 YYYY-MM-DD
    market TEXT NOT NULL,              -- TWSE=上市 / TPEX=上櫃
    code TEXT NOT NULL,
    name TEXT,
    foreign_net_shares REAL,           -- 外資及陸資買賣超股數(不含外資自營商)
    trust_net_shares REAL,             -- 投信買賣超股數
    dealer_net_shares REAL,            -- 自營商買賣超股數
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, code)
);

CREATE INDEX IF NOT EXISTS idx_insti_stock_date ON institutional_stock_daily(trade_date);

-- 個股已發行股數(股本)，來源：上市/上櫃公司基本資料 OpenAPI，每次執行整包覆寫(資料
-- 量小、變動不頻繁，用最新一份覆寫最省事，不需要保留歷史)。
CREATE TABLE IF NOT EXISTS stock_shares_outstanding (
    code TEXT NOT NULL,
    market TEXT NOT NULL,
    name TEXT,
    shares_outstanding REAL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (code)
);
"""


def get_connection() -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(SCHEMA)


@contextmanager
def session():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def upsert_fund(conn: sqlite3.Connection, ticker: str, name: str, issuer: str) -> None:
    conn.execute(
        """
        INSERT INTO funds (ticker, name, issuer) VALUES (?, ?, ?)
        ON CONFLICT(ticker) DO UPDATE SET name = excluded.name, issuer = excluded.issuer
        """,
        (ticker, name, issuer),
    )


def upsert_summary(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO daily_summary
            (ticker, date, nav_per_unit, total_units, units_diff, net_asset_value,
             beneficiaries_count, fetched_at)
        VALUES (:ticker, :date, :nav_per_unit, :total_units, :units_diff, :net_asset_value,
                :beneficiaries_count, :fetched_at)
        ON CONFLICT(ticker, date) DO UPDATE SET
            nav_per_unit = excluded.nav_per_unit,
            total_units = excluded.total_units,
            units_diff = excluded.units_diff,
            net_asset_value = excluded.net_asset_value,
            beneficiaries_count = excluded.beneficiaries_count,
            fetched_at = excluded.fetched_at
        """,
        row,
    )


def get_fetched_year_months(conn: sqlite3.Connection, code: str) -> set[str]:
    rows = conn.execute(
        "SELECT year_month FROM stock_price_fetch_log WHERE code = ?", (code,)
    ).fetchall()
    return {r["year_month"] for r in rows}


def mark_year_month_fetched(conn: sqlite3.Connection, code: str, year_month: str, found: bool, fetched_at: str) -> None:
    conn.execute(
        """
        INSERT INTO stock_price_fetch_log (code, year_month, found, fetched_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(code, year_month) DO UPDATE SET found = excluded.found, fetched_at = excluded.fetched_at
        """,
        (code, year_month, 1 if found else 0, fetched_at),
    )


def upsert_stock_prices(conn: sqlite3.Connection, code: str, rows: list[dict]) -> None:
    conn.executemany(
        """
        INSERT INTO stock_prices (code, date, close, source)
        VALUES (:code, :date, :close, :source)
        ON CONFLICT(code, date) DO UPDATE SET close = excluded.close, source = excluded.source
        """,
        [{**r, "code": code} for r in rows],
    )


def replace_txo_daily(conn: sqlite3.Connection, trade_date: str, rows: list[dict]) -> None:
    conn.execute("DELETE FROM txo_daily WHERE trade_date = ?", (trade_date,))
    conn.executemany(
        """
        INSERT INTO txo_daily
            (trade_date, contract_month, strike_price, option_type, session,
             open_price, high_price, low_price, close_price, volume,
             settlement_price, open_interest, change_price, change_pct, expiry_date, fetched_at)
        VALUES
            (:trade_date, :contract_month, :strike_price, :option_type, :session,
             :open_price, :high_price, :low_price, :close_price, :volume,
             :settlement_price, :open_interest, :change_price, :change_pct, :expiry_date, :fetched_at)
        """,
        [{**r, "trade_date": trade_date} for r in rows],
    )


def upsert_bond_series(conn: sqlite3.Connection, series_id: str, rows: list[dict]) -> None:
    """rows 為 [{"obs_date","value","fetched_at"}, ...]；用 upsert 而非整段刪除重建，
    因為每天只補最近 lookback_days 天，不能把更早的歷史資料一起清掉。
    """
    conn.executemany(
        """
        INSERT INTO bond_series (series_id, obs_date, value, fetched_at)
        VALUES (:series_id, :obs_date, :value, :fetched_at)
        ON CONFLICT(series_id, obs_date) DO UPDATE SET value = excluded.value, fetched_at = excluded.fetched_at
        """,
        [{**r, "series_id": series_id} for r in rows],
    )


def upsert_mpt_probability(conn: sqlite3.Connection, rows: list[dict]) -> None:
    """rows 為 [{"obs_date","reference_meeting","prob_cut","prob_hike","fetched_at"}, ...]。"""
    conn.executemany(
        """
        INSERT INTO mpt_probability (obs_date, reference_meeting, prob_cut, prob_hike, fetched_at)
        VALUES (:obs_date, :reference_meeting, :prob_cut, :prob_hike, :fetched_at)
        ON CONFLICT(obs_date) DO UPDATE SET
            reference_meeting = excluded.reference_meeting,
            prob_cut = excluded.prob_cut,
            prob_hike = excluded.prob_hike,
            fetched_at = excluded.fetched_at
        """,
        rows,
    )


def replace_institutional_stock_daily(conn: sqlite3.Connection, trade_date: str, market: str, rows: list[dict]) -> None:
    """rows 為 [{"market","code","name","foreign_net_shares","trust_net_shares",
    "dealer_net_shares","fetched_at"}, ...]。用整天刪除重建(不是 upsert)，因為
    T86/TPEx 都是「當天完整名單」一次回傳，刪除重建比逐檔比對簡單。**刪除一定要
    連 market 一起篩選**：上市(TWSE)跟上櫃(TPEX)常常是同一個交易日，如果只用
    trade_date 刪除，後執行的那個市場會把先前另一個市場當天剛寫進去的資料整批
    清空(已經實測踩過這個坑)。
    """
    conn.execute("DELETE FROM institutional_stock_daily WHERE trade_date = ? AND market = ?", (trade_date, market))
    conn.executemany(
        """
        INSERT INTO institutional_stock_daily
            (trade_date, market, code, name, foreign_net_shares, trust_net_shares, dealer_net_shares, fetched_at)
        VALUES
            (:trade_date, :market, :code, :name, :foreign_net_shares, :trust_net_shares, :dealer_net_shares, :fetched_at)
        """,
        [{**r, "trade_date": trade_date} for r in rows],
    )


def upsert_shares_outstanding(conn: sqlite3.Connection, rows: list[dict]) -> None:
    """rows 為 [{"code","market","name","shares_outstanding","fetched_at"}, ...]。"""
    conn.executemany(
        """
        INSERT INTO stock_shares_outstanding (code, market, name, shares_outstanding, fetched_at)
        VALUES (:code, :market, :name, :shares_outstanding, :fetched_at)
        ON CONFLICT(code) DO UPDATE SET
            market = excluded.market,
            name = excluded.name,
            shares_outstanding = excluded.shares_outstanding,
            fetched_at = excluded.fetched_at
        """,
        rows,
    )


def replace_futures_daily(conn: sqlite3.Connection, trade_date: str, rows: list[dict]) -> None:
    conn.execute("DELETE FROM futures_daily WHERE trade_date = ?", (trade_date,))
    conn.executemany(
        """
        INSERT INTO futures_daily
            (trade_date, product, contract_month, session, open_price, high_price, low_price,
             close_price, change_price, change_pct, volume, settlement_price, open_interest, fetched_at)
        VALUES
            (:trade_date, :product, :contract_month, :session, :open_price, :high_price, :low_price,
             :close_price, :change_price, :change_pct, :volume, :settlement_price, :open_interest, :fetched_at)
        """,
        [{**r, "trade_date": trade_date} for r in rows],
    )


def replace_institutional_futures(conn: sqlite3.Connection, trade_date: str, rows: list[dict]) -> None:
    conn.execute("DELETE FROM institutional_futures WHERE trade_date = ?", (trade_date,))
    conn.executemany(
        """
        INSERT INTO institutional_futures
            (trade_date, product, investor_type, long_volume, long_value, short_volume, short_value,
             net_volume, net_value, long_oi, long_oi_value, short_oi, short_oi_value, net_oi, net_oi_value, fetched_at)
        VALUES
            (:trade_date, :product, :investor_type, :long_volume, :long_value, :short_volume, :short_value,
             :net_volume, :net_value, :long_oi, :long_oi_value, :short_oi, :short_oi_value, :net_oi, :net_oi_value, :fetched_at)
        """,
        [{**r, "trade_date": trade_date} for r in rows],
    )


def replace_holdings(conn: sqlite3.Connection, ticker: str, date: str, holdings: list[dict]) -> None:
    conn.execute("DELETE FROM holdings WHERE ticker = ? AND date = ?", (ticker, date))
    conn.executemany(
        """
        INSERT INTO holdings (ticker, date, asset_type, code, name, shares, weight_pct, market_value)
        VALUES (:ticker, :date, :asset_type, :code, :name, :shares, :weight_pct, :market_value)
        """,
        [{**h, "ticker": ticker, "date": date} for h in holdings],
    )
