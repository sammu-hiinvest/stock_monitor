# 台股主動式 ETF 持股追蹤

追蹤 8 檔台股主動式 ETF 的每日淨值、規模與持股明細，資料源為各投信官網的「申購買回清單 (PCF)」公開 API：

| 代號 | 名稱 | 投信 |
|---|---|---|
| 00981A | 主動統一台股增長 | 統一投信 |
| 00403A | 主動統一升級50 | 統一投信 |
| 00988A | 主動統一全球創新 | 統一投信 |
| 00991A | 主動復華未來50 | 復華投信 |
| 00985A | 主動野村台灣50 | 野村投信 |
| 00980A | 主動野村台灣優選 | 野村投信 |
| 00982A | 主動群益台灣強棒 | 群益投信 |
| 00992A | 主動群益科技創新 | 群益投信 |

另外還有一個「台指選擇權 (TXO)」頁面，追蹤大盤選擇權的每日未平倉量/成交量與履約價分佈，
資料源為期交所「選擇權每日交易行情下載」公開下載功能。

## 架構

```
app/
  config.py        追蹤標的清單設定
  db.py             SQLite 存取 (data/etf.db)
  scrapers/         各資料源的抓取邏輯 (四家投信 + 期交所 TXO + 個股收盤價)
  fetch.py          ETF 每日抓取主流程 (寫入 DB + 備份原始 JSON 到 data/raw/)
  fetch_txo.py      TXO 每日抓取主流程 / 回溯下載
  main.py           FastAPI：資料 API + 提供儀表板首頁
  static/index.html 儀表板前端 (純 HTML/JS + Chart.js)
scripts/
  fetch_daily.py            排程呼叫的進入點 (ETF + TXO 都在這裡一起跑)
  backfill_txo.py           TXO 歷史資料一次性回溯下載
  setup_scheduled_task.ps1  註冊 Windows 工作排程器 (每交易日 17:00 自動抓取)
  run_server.ps1            啟動本機網頁伺服器
```

## 安裝

以下所有指令都要先切換到專案目錄再執行，否則 Python 會找不到 `app` 這個套件
(`ModuleNotFoundError: No module named 'app'`)：

```powershell
cd C:\Users\samfa\Claude_Project\tw-active-etf-tracker
pip install -r requirements.txt
```

## 首次使用

1. 手動抓一次資料（會建立 `data/etf.db`）：
   ```powershell
   cd C:\Users\samfa\Claude_Project\tw-active-etf-tracker
   python scripts/fetch_daily.py
   ```
2. 啟動網頁伺服器（這個腳本會自動切換到專案目錄，不用自己先 cd）：
   ```powershell
   powershell -ExecutionPolicy Bypass -File "C:\Users\samfa\Claude_Project\tw-active-etf-tracker\scripts\run_server.ps1"
   ```
   或手動下指令的話記得先 cd 到專案目錄：
   ```powershell
   cd C:\Users\samfa\Claude_Project\tw-active-etf-tracker
   python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
   ```
3. 瀏覽器開啟 http://127.0.0.1:8000

儀表板右上角也有「立即抓取最新資料」按鈕，效果等同執行 `fetch_daily.py`。

## 設定每日自動抓取 (Windows 工作排程器)

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup_scheduled_task.ps1
```

會建立一個名為 `TW-ActiveETF-DailyFetch` 的排程任務，每週一到週五 17:00 執行。
統一投信約 16:30 後才更新申購買回清單，其餘三家投信約在 14:45–16:30 間更新完成，17:00 執行可涵蓋四家都更新的情況。

- 手動測試排程：`schtasks /Run /TN "TW-ActiveETF-DailyFetch"`
- 查看排程狀態：`schtasks /Query /TN "TW-ActiveETF-DailyFetch" /V`
- 移除排程：`schtasks /Delete /TN "TW-ActiveETF-DailyFetch" /F`
- 執行紀錄：`data/fetch_task.log`

## 匯入歷史資料（一次性）

若拿到投研系統匯出的歷史資料 ZIP（如 `Active_ETFs_All_Data.zip`，內含各 ETF 的
「歷史淨值與規模」「歷史持股明細變動」CSV），可用以下指令匯入補齊每日排程抓取
之前的歷史區間：

```bash
python scripts/import_historical_zip.py "Active_ETFs_All_Data.zip"
```

- 只會補資料庫裡還沒有的日期，不會覆蓋既有的即時爬蟲資料。
- ZIP 內的「日期」欄位是投信公告日、不是資料實際反映的交易基準日，匯入時已自動
  對齊成與即時爬蟲一致的交易基準日（細節見腳本內註解）。
- 若某檔 ETF 沒有出現在 ZIP 裡（例如本專案的 00988A），會直接略過，維持原本的
  即時爬蟲資料不受影響。

## 法人現貨

近兩個交易日「外資買超佔股本比重(外本比)」「投信買超佔股本比重(投本比)」排行，
並標記兩份排行榜重複上榜的個股。

- 原始需求是比照 Goodinfo 的「外本比／投本比」排行頁，但該站由 Cloudflare
  Turnstile 人機驗證保護（直接 curl 回 403 + `Cf-Mitigated: challenge`
  header，瀏覽器打開也會看到「驗證您是人類」的檢查方塊）。這類人機驗證本專案
  不會嘗試繞過，因此改用證交所/櫃買中心官方公開 API 自己計算等價指標，資料來源
  更穩定、也不需要金鑰。
- 資料來源：
  - 上市三大法人買賣超日報(T86)：
    `https://www.twse.com.tw/rwd/zh/fund/T86?date=YYYYMMDD&selectType=ALL&response=json`
    （支援 `date` 參數回溯查詢；非交易日回傳空清單，不是錯誤）
  - 上市公司基本資料(股本)：
    `https://openapi.twse.com.tw/v1/opendata/t187ap03_L`
    （不支援日期參數，只有最新一版全量快照）
  - 上櫃三大法人買賣明細：
    `https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading`
    （**不支援任何日期參數，只能拿到最新一個交易日**，已在 Swagger UI 確認過）
  - 上櫃公司基本資料(股本)：
    `https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O`
- 指標算法：取資料庫裡最新的兩個交易日，將每檔個股在這兩天的
  外資買賣超股數／投信買賣超股數分別加總，再除以該股「已發行股數」
  得到外本比／投本比（百分比）；ETF/基金等沒有股本資料的代碼會被自動排除。
- 已知限制：上市(TWSE)因為 T86 支援 `date` 參數，第一次執行就能一口氣回溯
  補到 2 個交易日；上櫃(TPEx)沒有回溯能力，剛啟用這個功能時只會有 1 天資料，
  隔天排程再跑一次後才會自然累積成 2 天。
- 已知坑：`institutional_stock_daily` 的每日「先刪後插」邏輯，DELETE
  一定要同時用 `trade_date` **和** `market` 篩選 —— 因為上市和上櫃常常
  是同一個交易日，如果只用 `trade_date` 篩選，後執行的市場(如 TPEx)插入
  時的 DELETE 會把先前已經寫入、同一天的另一個市場(TWSE)資料整批砍掉。
  `stock_daily_quote`(見下方) 也比照同一個作法。
- 排行表格另外附上「當日漲跌幅」欄位，>9.5% 標記為漲停(紅底)、<=-9.5% 標記為
  跌停(綠底)。資料來源：
  - 上市：MI_INDEX「每日收盤行情」
    `https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date=YYYYMMDD&type=ALLBUT0999&response=json`
    漲跌方向藏在「漲跌(+/-)」欄位的 HTML 顏色(`color:red`=漲 / `color:green`=跌)，
    要自己配合「漲跌價差」還原正負號、算出百分比。
  - 上櫃：`https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes`
    （`Change` 欄位本身就帶正負號；同樣只能拿到最新一個交易日）
  - 只抓「最新一個交易日」存進 `stock_daily_quote` 表，跟外本比/投本比的兩日
    累計視窗是分開的概念，純粹顯示當天股價表現。

## 台指選擇權 (TXO)

- 首次建立資料庫時需手動回溯下載一次（預設抓 2026/09/01~2026/09/04）：
  ```powershell
  cd C:\Users\samfa\Claude_Project\tw-active-etf-tracker
  python scripts/backfill_txo.py
  ```
  也可自訂區間 `python scripts/backfill_txo.py 2026-08-01 2026-08-31`（期交所單次查詢區間上限一個月）。
- 之後 `scripts/fetch_daily.py`（含排程）每次都會順便抓最近 5 天的 TXO 資料並覆寫，
  就算某天排程漏跑，下一次執行也會自動補齊，不需要另外設定排程。
- 資料表 `txo_daily`：每個 (交易日期, 到期月份, 履約價, 買賣權, 交易時段) 一筆，同時保留
  「一般」(日盤)與「盤後」(夜盤) 兩種時段；儀表板上的統計一律只採用「一般」時段。
- 到期月份下拉選單會套用到本頁全部三張圖(合併走勢圖/區間軌跡/履約價比較)，
  對應後端 `/api/txo/summary`、`/api/txo/oi-range`、`/api/txo/compare` 三個
  端點都支援 `contract_month` 參數；留空(全部合約)時同一履約價會加總所有
  到期月份。
- 已知坑(已修正)：GitHub Pages 靜態展示站(`scripts/export_static.py`)原本
  只匯出「全部合約」那組 TXO 資料，切換到期月份下拉選單時，前端會打一個
  沒有對應靜態 JSON 檔案的網址、靜默 404、圖表完全不會動(本機用真的
  FastAPI 開發伺服器測試時看不出來，因為即時 API 本來就什麼組合都查得到)。
  修正方式：額外把「預設日期範圍 x 每個到期月份」的組合也匯出成靜態檔案，
  涵蓋使用者只切到期月份、不改日期範圍的最常見操作；如果日期範圍也一起改，
  靜態展示站仍然沒有對應資料(這個維度的組合數會爆炸，跟 `compare` 的
  日期兩兩配對一樣沒有窮舉)。

## 大盤期貨 (TX/MTX/TMF + 三大法人)

- 首次建立資料庫時需手動回溯下載一次（預設抓 2026/09/01~2026/09/04）：
  ```powershell
  cd C:\Users\samfa\Claude_Project\tw-active-etf-tracker
  python scripts/backfill_futures.py
  ```
  也可自訂區間 `python scripts/backfill_futures.py 2026-08-01 2026-08-31`。
- 之後 `scripts/fetch_daily.py`（含排程）每次都會順便抓最近 5 天的資料並覆寫。
- 兩個資料源、兩張表：
  - `futures_daily`：期交所「期貨每日交易行情下載」，TX/MTX/TMF 各契約月份、一般/盤後兩種時段。
  - `institutional_futures`：期交所「三大法人-下載-區分各期貨契約-依日期」，自營商/投信/外資及陸資
    三類法人的多空交易量與未平倉量。
- 散戶部位為**反推估算**（`市場整體未平倉量(全部到期月份加總、一般盤) − 三大法人合計`），多空兩邊分別計算，
  不是官方公布數字。
- MTX(小型臺指)、TMF(微型臺指)換算成 TX(臺股期貨)約當口數的倍數，是由期交所公布的「未平倉契約金額」
  除以口數、再除以當日結算價回推驗證得出：TX=200元/點、MTX=50元/點(TX的1/4)、TMF=10元/點(TX的1/20)。
- 注意：「期貨每日交易行情下載」與「三大法人」兩份報表，商品代碼不是同一套（例如小型臺指一邊叫
  `MTX`、另一邊叫 `MXF`），`app/scrapers/taifex_futures.py` 與 `app/scrapers/taifex_institutional.py`
  各自維護自己的代碼對照表，不要混用。

## 債市 (美債殖利率曲線 / 利差 / 公司債利差 / 5Y5Y遠期通膨預期)

- 資料源是 **FRED**（美國聖路易聯邦準備銀行）的公開 CSV 下載端點，不需要申請 API 金鑰，
  是目前四個資料源裡最省事、最省 token 的一個（不用逆向工程找隱藏端點，就是官網「Download」
  按鈕背後那個網址：`https://fred.stlouisfed.org/graph/fredgraph.csv?id=<SERIES_ID>`）。
- 首次建立資料庫需先回溯下載一次（預設抓最近 2 年）：
  ```powershell
  cd C:\Users\samfa\Claude_Project\tw-active-etf-tracker
  python scripts/backfill_bonds.py
  python -m app.fetch_cpi
  python -m app.fetch_macro
  ```
  也可自訂區間 `python scripts/backfill_bonds.py 2024-01-01 2026-09-07`。CPI/總經(PPI/
  非農就業/家戶調查) 資料(BLS 來源)沒有日期區間參數，`python -m app.fetch_cpi` /
  `python -m app.fetch_macro` 固定各抓最近 4 年並覆寫。
- 之後 `scripts/fetch_daily.py`（含排程）每次都會順便抓最近 10 天的資料並覆寫。
- 追蹤的 24 個 FRED 序列定義在 `app/fetch_bonds.py` 的 `BOND_SERIES`：11 個殖利率曲線年期
  (DGS1MO ~ DGS30)、2Y10Y/3M10Y 利差 (T10Y2Y/T10Y3M)、BBB 投等/高收益公司債利差
  (BAMLC0A4CBBB/BAMLH0A0HYM2)、5Y5Y 遠期通膨預期 (T5YIFR)、5Y 損益兩平通膨率 (T5YIE)、
  聯邦資金目標區間上下限 (DFEDTARU/DFEDTARL)、泰勒法則計算用的實質GDP/CBO潛在GDP/核心PCE
  物價指數 (GDPC1/GDPPOT/PCEPILFE)、FOMC經濟展望摘要(SEP)長期聯邦資金利率中位數預測
  (FEDTARMDLR，當中性利率估計用)、PCE物價指數 (PCEPI)。其中 `GDPC1`/`GDPPOT`/
  `PCEPILFE`/`PCEPI` 為了算年增率，實際回溯區間會比其他序列多抓 1 年當基期
  (`EXTRA_LOOKBACK_SERIES`)，不然圖表最前面約 1 年會因為找不到「12個月前」的資料
  而算不出年增率。CPI/PPI/非農就業/家戶調查改用 BLS (美國勞工統計局) 官方 API，
  見下方「## Macros」章節。
- 頁面頂部統計卡片除了上述序列的最新值，還會即時算出「高收益債－BBB投等利差」(兩者
  相減的衍生指標，`app/main.py` 的 `_hy_minus_bbb_series()`)，每張卡片下方並以藍字顯示
  該數值相較過去 2 年歷史的百分位 (`_percentile_of_series()` / `_percentile_rank()`)，
  百分位數字依四分位數分色：0-25% 紅、25-50% 黃、50-75% 綠、75-100% 藍。
- 殖利率曲線圖同時疊加「最新／近一週／近一月／近三個月／YTD」五條線，方便比較整條
  曲線隨時間的位移；投資等級 vs 高收益公司債利差圖表則額外用右側 Y2 軸疊一條「高收益債
  －BBB利差」陰影面積，凸顯信用利差分層走闊/收斂的幅度。
- 美國 CPI/PCE 通膨率(含 CPI 九大分項)已搬到獨立的「Macros」頁面，跟 PPI、非農就業、
  家戶調查資料放在一起，見下方「## Macros」章節。
- **殖利率曲線隱含的政策利率預期走勢**：CME FedWatch 那種 Fed funds futures 逐次FOMC
  會議機率分布，沒有免費、免金鑰的公開資料源可用(FRED 不提供期貨報價，CME 官方要付費
  訂閱且有反爬蟲保護，實測直接連線會被拒絕)。所以改用「預期假說」近似：3個月期／1年期
  美債殖利率視為市場預期該期間內的平均政策利率，畫在同一張圖上跟實際聯邦資金目標區間
  (上限/下限/中位數，灰色帶狀區域) 比較，落差可以粗略反映市場對升降息的預期方向——
  **這不是官方期貨機率，只是用現有免費資料做的近似值**，頁面上有標註提醒。見
  `/api/bonds/policy-expectation`。
- **Atlanta Fed Market Probability Tracker (真正的官方升息/降息機率)**：緊接在上面那張
  近似圖下方，同一個面板內、共用同一條時間軸。背後是 CME 3個月期SOFR選擇權隱含的機率
  分布，Atlanta Fed 官方整理成公開歷史資料 Excel 提供下載（`app/fetch_mpt.py` 的
  `MPT_URL`，是官網頁面上明列的「MPT Historical Data」下載連結，不是逆向工程端點）。
  對每個觀測日，取「reference_start(CME 3個月合約參考窗口起始日) >= 觀測日」中最小的
  那個當作「最近一次FOMC會議」的近似，抓 `Prob: cut` / `Prob: hike` 兩個欄位存成精簡
  時間序列 (`mpt_probability` 表)，供 `/api/bonds/mpt-probability` 使用。原始 Excel
  有 30 萬多列、60多MB，用 pandas 讀取要 20~30 秒，所以只在排程/手動抓取時整包重抓一次、
  處理成精簡序列存 DB，API 端點本身讀 DB 很快。**授權限制**：Excel 內 LICENSE 分頁
  （文字內容是圖片文字框，openpyxl 讀不到 cell value，要直接解析
  `xl/drawings/drawing1.xml` 才看得到）寫明「Use of this data is permitted for
  personal and educational purposes only」，本專案是本機個人使用的儀表板、只存算出
  的精簡衍生序列，符合這個授權範圍；不要把這份資料重新整包對外發布。
- **泰勒法則(Taylor Rule)隱含利率**：`隱含利率 = 中性名目利率 + 1.5×(核心PCE年增率 − 2%)
  + 0.5×產出缺口`，產出缺口 = (實質GDP − CBO估計潛在GDP) / CBO估計潛在GDP。GDP 為季頻
  資料，這條線一季只變動一次；跟實際聯邦資金目標區間(上限/下限/中位數)畫在同一張圖比較。
  見 `/api/bonds/taylor-rule`。
  - **「中性名目利率」用 FEDTARMDLR (FOMC經濟展望摘要對長期聯邦資金利率的中位數預測)，
    不是教科書固定 2%+2%=4%**。最早的版本假設均衡實質利率與通膨目標都固定 2%，結果算出
    來的隱含利率比 [Atlanta Fed 官方 Taylor Rule Utility](https://www.atlantafed.org/research-and-data/data/taylor-rule)
    高出約 1.5~2 個百分點。查證後發現：Atlanta Fed 的工具預設用「時變」的均衡實質利率
    估計(Laubach-Williams / Holston-Laubach-Williams 模型、或 FOMC SEP 預測)，不是固定
    2%；FRED 沒有 Laubach-Williams 模型估計值的公開序列，但 FEDTARMDLR 正是 Atlanta Fed
    本身也支援的其中一種 r* 資料來源(FOMC participant projections)，改用它之後兩者
    落差縮小到約 0.6~1.4 個百分點，且都是這個模型設計上就有的合理範圍。
  - 剩餘落差主因：Atlanta Fed 首頁預設顯示的「Alternative 1/2」(Shortfalls Rule /
    Balanced Approach) 用「兩倍失業率缺口」而非 GDP 產出缺口衡量資源閒置程度、且錨定
    FOMC SEP 的長期失業率估計，是跟這裡的 CBO GDP 缺口版本(對應 Atlanta Fed 的
    "Alternative 3"，最接近 Taylor 1993 原始版)不同的公式，不會完全對得上是預期中的
    正常現象，不是計算錯誤。
  （泰勒法則圖表本身已依使用者要求從前端頁面移除，但 `/api/bonds/taylor-rule`
  端點與底層 FRED 資料抓取都還在，之後要恢復顯示不需要重新設計。）
- **重要坑**：`app/scrapers/fred.py` 刻意不用共用的 `make_session()`、也不自訂任何
  User-Agent。實測發現只要帶「任何」自訂 User-Agent（不管是偽裝瀏覽器的字串、還是老實
  表明身份的字串），FRED 這邊就會直接不回應、卡到逾時；只有完全不覆寫、讓 `requests`
  用它自己預設的 `python-requests/x.x` 才會秒回。所以這個 scraper 用最單純的
  `requests.get()`，不加任何 headers，跟其他 scraper 的寫法不一樣是刻意的，別「順手」
  把它改成統一風格。
- **美債10年期 Term Premium 與 ACM Fitted Yield**：資料源是紐約聯邦準備銀行(NY Fed)
  官方公開研究資料頁面「Term Premia」的整包下載連結
  (`https://www.newyorkfed.org/medialibrary/media/research/data_indicators/ACMTermPremium.xls`，
  頁面：https://www.newyorkfed.org/research/data_indicators/term-premia-tabs#/interactive，
  不是逆向工程端點，就是官網互動圖表頁「Download」按鈕背後的網址)。背後是
  Adrian-Crump-Moench (ACM) 期限結構模型，把美債殖利率拆解成「風險中立利率預期
  路徑」+「期限溢酬(term premium)」，「ACM fitted yield」是模型配適出來的殖利率。
  見 `app/fetch_acm.py`：
  - 檔案是舊版 Excel (`.xls`，OLE2/CDFV2 二進位格式，不是 `.xlsx`)，要另外裝
    `xlrd` 套件才能用 pandas 讀取(openpyxl 只支援新版 `.xlsx`)。
  - 用「ACM Daily」分頁(日頻資料，從 1961 年至今)，只取 10 年期的 `ACMY10`(fitted
    yield)、`ACMTP10`(term premium) 兩欄，篩選最近 800 天存進 DB，避免 60 幾年的
    歷史資料整包塞進去。
  - 存進跟其他債市資料共用的 `bond_series` 表，series_id 直接用 `ACMY10`/`ACMTP10`，
    所以前端可以直接沿用既有的通用端點 `/api/bonds/series?ids=DGS10,ACMY10,ACMTP10`
    跟實際殖利率畫在同一張圖，不需要新增專屬 API。
  - 已用實際資料驗證：`ACMY10`(模型配適殖利率) 應該要非常貼近 `DGS10`(FRED 的
    實際10年期殖利率)，實測兩者逐日數值只差幾個bp，符合模型設計預期(模型配適
    誤差極小)，確認欄位對應無誤。

## Macros (美國 CPI／PPI／非農就業／家戶調查)

獨立頁面，跟「債市」頁分開；資料源是 **BLS(美國勞工統計局)官方 API v2**
(`https://api.bls.gov/publicAPI/v2/timeseries/data/`，POST JSON、不需要金鑰)，
PCE 例外維持用 FRED(BEA 沒有對應的公開查詢 API)。

- 頁面最上方有統計卡片 (`/api/macro/stats`)：CPI/核心CPI/PPI/核心PPI 顯示最新一期
  YoY、MoM，以及跟前一期(上個月自己的YoY/MoM)相比的變化(百分點)；非農就業總數/
  失業率/勞動力參與率顯示最新值與前一期的值、變化量。漲跌顏色沿用全站慣例(紅漲/
  綠跌)。
- 首次建立資料庫需先抓一次(BLS API 是整段年份查詢，沒有日期區間參數，固定抓最近
  4 年並覆寫)：
  ```powershell
  cd C:\Users\samfa\Claude_Project\tw-active-etf-tracker
  python -m app.fetch_cpi
  python -m app.fetch_macro
  ```
- 追蹤的序列定義在 `app/fetch_cpi.py` 的 `CPI_SERIES` (22個) 跟 `app/fetch_macro.py`
  的 `MACRO_SERIES` (28個)，抓取邏輯共用 `app/scrapers/bls.py`。BLS 未註冊金鑰的額度
  是單次查詢最多 25 個序列，兩個檔案加起來遠超過這個上限，所以 `bls.py` 的
  `fetch_series()` 會自動切成多批查詢。
- **CPI／PPI**：跟 CPI 一樣的官方慣例——年增率(YoY)用未季調(NSA)序列算、月增率(MoM)
  用季調後(SA)序列算，兩種調整方式不能混用同一條序列，所以每個項目都各自抓 SA/NSA
  兩條序列。PPI 用的是 BLS「Final Demand」headline 指數 (`WPSFD4`/`WPUFD4`) 跟排除
  食品與能源的核心版本 (`WPSFD49104`/`WPUFD49104`)——**這組序列代碼容易搞混**：
  `WPSFD49207`(Finished Goods，舊分類)跟真正的 headline「Final Demand」數值差了快
  一倍(157 vs 280的指數水準)，是先用 BLS 官方「PPI Final Demand/Intermediate Demand
  Aggregation Indexes by Title and Series ID」頁面查到正確代碼、再用 FRED 對應序列
  (PPIFID/PPICOR)的數值交叉驗證過完全一致才採用的，不要憑印象猜代碼。
- **非農就業月增減 (依產業別)**：`CES` 開頭的序列本身就是季調後的就業「水準」(千人)，
  BLS 沒有直接發布「月增減」，是後端逐月相減算出來的 (`app/main.py` 的
  `_level_changes()`，用日期而非陣列位置比對「上個月」，理由同 CPI 的 bug 修復)。
  11 個主要產業別 (採礦與伐木業/營建業/製造業/批發零售運輸倉儲公用事業/資訊業/
  金融活動/專業與商業服務/教育與醫療服務/休閒與旅宿業/其他服務業/政府部門) 加總會
  精確等於非農就業總數 (已驗證：兩者水準跟月增減都完全對得上，因為這是 BLS 官方
  互斥且完整的分類設計)。圖表用「堆疊長條(各產業) + 一條總數折線」呈現，見
  `/api/macro/payroll`。
- **失業率／勞動力參與率／就業人口比**：`LNS` 開頭的序列本身就是季調後的「家戶調查」
  (Current Population Survey) 統計，三條線畫在同一張圖，見 `/api/macro/household-rates`。
- **失業原因** (遭資遣/解僱、主動離職、重返勞動市場、初次尋職) 與**兼職原因** (經濟
  因素 vs 非經濟因素)：BLS 官方分類本身就互斥完整，直接疊成堆疊面積圖，分別見
  `/api/macro/unemployment-reason`、`/api/macro/parttime`。
- **失業週數**：BLS 直接發布的是「<5週」「5-14週」「15週以上(累計)」「27週以上」，
  沒有獨立的「15-26週」序列；後端把「15週以上」減掉「27週以上」算出「15-26週」，
  湊成四個互斥區間才能疊圖 (`app/main.py` 的 `/api/macro/unemployment-duration`)。
- 這幾類序列 (`CES`/`LNS` 開頭) 都已經是官方季調後數字，不像 CPI/PPI 需要額外抓
  未季調(NSA)版本；本專案就只存/只用官方慣例引用的 SA 版本。

## Bond Auction (美債標售：10年期公債 Note／30年期公債 Bond)

獨立頁面。資料源是美國財政部 **FiscalData 公開 API**「Treasury Securities Auctions
Data」，不需要金鑰：
```
https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/od/auctions_query
```
(資料集頁面：https://fiscaldata.treasury.gov/datasets/treasury-securities-auctions-data/treasury-securities-auctions-data)

- 首次建立資料庫需先抓一次(API 沒有增量端點，`python -m app.fetch_auctions`
  固定抓最近 11 年並覆寫)：
  ```powershell
  cd C:\Users\samfa\Claude_Project\tw-active-etf-tracker
  python -m app.fetch_auctions
  ```
- 見 `app/fetch_auctions.py`。兩張圖各是「標售金額(長條，左軸)」+「Bid-to-Cover
  比率(折線，右軸)」的雙Y軸混合圖：
  - **用 `original_security_term` 篩選年期，不是 `security_term`**：加碼發行
    (reopening) 的標售在 `security_term` 會顯示成「9-Year 10-Month」這種剩餘
    年期字串，只有 `original_security_term` 在整個發行週期都穩定顯示
    「10-Year」／「30-Year」，已用實際資料驗證過。
  - **Bid-to-Cover 比率是自己算的**：`total_tendered`(總投標金額) ÷
    `total_accepted`(實際得標金額)，不是直接拿 API 本身的 `bid_to_cover_ratio`
    欄位——已跟該欄位交叉比對過數值一致，自己算是為了計算方式透明、不依賴
    上游欄位是否每筆都有填(較舊的標售資料這欄常是 null)。
  - 標售金額用 `total_accepted`(實際得標金額，換算成十億美元)而不是
    `offering_amt`(公告發行額)，因為實際得標金額有時會因為 SOMA(聯準會)、
    海外官方帳戶等加碼認購而略高於公告發行額，是更貼近「這次標售真正賣出
    多少」的數字。
  - 存進跟其他債市資料共用的 `bond_series` 表，series_id 用
    `AUCTION_10Y_AMOUNT`/`AUCTION_10Y_BTC`/`AUCTION_30Y_AMOUNT`/`AUCTION_30Y_BTC`，
    前端直接沿用既有的通用端點 `/api/bonds/series?ids=...`，沒有新增專屬 API。

## 資料保存方式

- `data/etf.db`：SQLite，`daily_summary`（每日淨值/規模）與 `holdings`（每日持股明細）兩張表，以 (ticker, date) 為主鍵，每天新增一筆，不會覆蓋歷史。
- `data/raw/<ticker>/<date>.json`：每次抓取的原始 API 回應備份，供未來投信改版時比對欄位用。
- 因為多數投信官網只公開「最新一期」申購買回清單、沒有歷史查詢功能，本系統只能從安裝當天開始逐日累積歷史，無法回溯抓取更早的資料。

## 已知限制

- 各投信網站為非公開 API（前端呼叫的內部端點），未來若網站改版，對應的 `app/scrapers/<issuer>.py` 可能需要更新。
- 若當天抓取失敗（改版、網路問題等），`data/fetch_task.log` 與程式輸出會記錄錯誤訊息，不會讓其他基金的抓取跟著失敗。
