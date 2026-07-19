# Agent 指南

本文件为 AI 代理（Agent）在本仓库中工作时的指引，描述项目目标、架构约定、修改规范与注意事项。任何代理在动手修改代码前，请先完整阅读本文件。

## 1. 项目定位

本项目是一个 **A股开盘30分钟强势股筛选程序**，手动触发运行，筛选出具备 3-5 日持续上涨潜力的个股，运行后直接在控制台输出前 3-5 只结果（不生成 HTML 报告、不自动定时调度）。

核心交易哲学：
- 指数不可测，但资金抱团的板块永远不会缺席。
- 个股必须背靠强势板块，且具备"涨停基因"，才能在短线中持续获得增量资金接力。
- 持股周期 3-5 天，属短线策略。

## 2. 技术栈

- 语言：Python 3.8+
- 数据源：akshare（主）、tushare（备，需 token）
- 数据处理：pandas、numpy
- 日期处理：python-dateutil
- 日志：logging（同时输出到文件 `stock_filter.log` 与控制台）

## 3. 目录结构

```
01_trading/
├── config.py                  # 集中式配置（所有阈值参数）
├── plate_analyzer.py          # 板块分析模块
├── stock_filter.py            # 个股筛选模块
├── minute_data_processor.py   # 分时数据处理模块
├── main.py                    # 主程序入口（手动触发、结果输出）
├── requirements.txt           # 依赖清单
└── stock_filter.log           # 运行日志（运行时自动创建）
```

## 4. 模块职责

### config.py
- 集中管理所有可调阈值与路径参数。
- **修改筛选规则时，优先调整此文件，不要在业务代码中硬编码阈值。**
- 关键参数分组：时间、市值、量能、乖离率、板块、涨停基因、突破、阻力位、日志、分时阈值、仓位建议阈值、输出数量、并发控制、网络超时、股票范围、概念板块。
- 新增列名重命名映射（COLUMN_MAP_HIST / COLUMN_MAP_SPOT / COLUMN_MAP_PLATE / COLUMN_MAP_MINUTE / COLUMN_MAP_INDEX / COLUMN_MAP_CONCEPT）和 `rename_columns()` 工具函数，统一将 akshare 返回的中文列名重命名为英文，供所有模块复用。
- 新增阈值分组：
  - 分时条件：`PULLBACK_VOLUME_RATIO`、`PATCH1_MIN_PRICE_RATIO`、`PATCH1_MAX_BELOW_AVG_PCT`
  - 仓位建议：`POSITION_PLATE_LIMIT_UP_HEAVY`、`POSITION_PLATE_ABOVE_5PCT_NORMAL`、`POSITION_PLATE_ABOVE_5PCT_LIGHT`
  - 输出控制：`TOP_STOCK_MAX`（每个维度各自的上限）
  - 并发控制：`PLATE_CONS_MAX_WORKERS`、`STOCK_FILTER_MAX_WORKERS`
  - 网络超时：`REQUEST_SOCKET_TIMEOUT`（15s）、`PLATE_CONS_PREFLIGHT`（预检开关）
  - 股票范围：`STOCK_PREFIX_MAIN`（仅沪深主板 60/00）、`STOCK_PREFIX_EXCLUDE`
  - 概念板块：`CONCEPT_MIN_CHANGE`、`CONCEPT_TOP_PERCENTILE`、`MAX_CONCEPT_RANK`、`CONCEPT_FETCH_LIMIT`
- 工具函数：`rename_columns()`、`code_to_symbol()`、`symbol_to_code()`、`get_end_date()`、`get_target_date_str()`

### plate_analyzer.py — `PlateAnalyzer`
- 获取同花顺二级行业板块数据与全市场 A 股实时数据。
- 计算板块涨幅排名与梯队结构（涨幅≥5% 家数、涨停家数、板块均涨幅）。
- 判断板块是否合格（排名 ≤ `MAX_PLATE_RANK` 且梯队达标）。
- 获取全 A 指数涨幅，供个股相对强度计算使用。
- `stock_plate_map` 属性（code→plate_name 映射）。
- `fetch_plate_constituents()` 方法：通过 `ak.stock_board_industry_cons_em(symbol=plate_name)` 获取成分股，建立 code→plate 映射（因 `stock_zh_a_spot_em` 不含行业列）。**线程池并发获取**，并发度由 `Config.PLATE_CONS_MAX_WORKERS` 控制。
  - **预检优化**（`Config.PLATE_CONS_PREFLIGHT=True`）：先单独请求第一个板块，若失败（说明东财整体不可用）直接进入降级模式，跳过剩余 29 个板块请求，节省 ~110s 等待时间。
  - **socket 超时**（`Config.REQUEST_SOCKET_TIMEOUT=15`）：在 `__init__` 中全局设置，避免每个请求等待 2 分钟默认超时。
- `get_stock_plate(code)` 方法：返回个股所属板块。
- **板块/概念涨跌幅查询**（新增，用于结果展示）：
  - `get_plate_change_percent(plate_name)`：从 `plate_rankings` 查询板块指数涨幅（反映板块本身强度）。
  - `get_concept_change_percent(concept_name)`：从 `concept_rankings` 查询概念板块涨幅。
  - 与 `get_plate_avg_change`（基于成分股均值）的区别：`get_plate_change_percent` 取板块指数本身的涨幅，更准确反映板块当日强度。
- **概念板块分析**（新增）：
  - `fetch_concept_rankings()`：东财概念接口失败时降级同花顺，逐个获取概念涨幅。通过 `Config.CONCEPT_FETCH_LIMIT=100` 限制获取数量（从 371 个减至 100），显著减少请求量。
  - `concept_hot_list`：热点概念名称列表（前 `MAX_CONCEPT_RANK=20` 个）。
  - `concept_avg_change`：热点概念平均涨幅。
  - `is_concept_active()`：`concept_avg_change >= Config.CONCEPT_MIN_CHANGE` 判断市场概念是否活跃。
- `run()` 方法串行执行 6 步流程（板块数据 → 全A股 → 排名 → 成分股 → 梯队 → 概念涨幅），任一步失败即返回 `False`。
- `analyze_plate_tier()` 基于成分股映射匹配板块；涨停家数判断使用 `Config.ZT_THRESHOLD_MAIN` / `Config.ZT_THRESHOLD_REGISTERED`。

### stock_filter.py — `StockFilter`
- 依赖 `PlateAnalyzer` 实例。
- 执行个股多级筛选：ST/停牌/市值/板块资格/板块地位 → 日线趋势 → 周线趋势 → 乖离率/突破 → 涨停基因 → 相对强度 → 长上影线。
- 计算 `ranking_score` 综合排名分数。
- `generate_comment()` 根据板块助攻情况生成仓位建议，阈值由 `Config.POSITION_PLATE_*` 控制；概念热点冷清时会附加提示。
- 日线数据有缓存（`stock_data_cache`），周线数据有缓存（`weekly_data_cache`），避免重复请求。
- `get_cached_daily_data(code)` 方法：暴露已缓存的日线数据供外部模块复用。
- `fetch_daily_data` 和 `fetch_weekly_data` 在获取数据后使用 `rename_columns` 重命名列名。
- `check_market_cap()` 单位处理：akshare `stock_zh_a_spot_em` 返回的流通市值单位为**元**，自动除以 1e8 转为亿元后再与 `Config.MIN_CIRCULATION_MKT_CAP` 比较；兼容带"亿"字符串输入。
- **主板过滤**（新增）：`is_main_board_stock(code)` 判断是否为沪深主板（60/00 开头），由 `Config.STOCK_PREFIX_MAIN` 控制。`filter_stock()` 在排除条件中先过滤掉非主板股票。
- **乖离率为必须条件**（修复）：乖离率超出 `[-2%, 5%]` 范围直接淘汰，不再与"突破近 10 日新高"做 OR。突破作为加分项记录在 details，但不作为筛选门槛。
- 日线日期范围：`days*2` 个自然日（确保足够交易日数据）；缓存检查改为 `len(cached) >= days`。
- 周线默认 `weeks=25`（确保足够计算 MA20）；东财周线失败时从日线重采样（`_resample_to_weekly`，W-FRI）。

### minute_data_processor.py — `MinuteDataProcessor`
- 获取当日 1 分钟分时数据与昨日成交量。
- 检查分时条件：早盘量能爆发、股价在均价线上、回踩缩量、9:45-10:00 补丁条件。
- 分时数据有缓存（`minute_data_cache`）。
- 移除了竞价检查（`fetch_auction_data` 和 `check_auction_volume` 方法已删除）。
- **关键修复**：`fetch_minute_data` 在获取数据后按当日日期过滤（`stock_zh_a_minute` 返回最近5个交易日数据，必须过滤），避免早盘量能、9:45-10:00 等条件混入历史数据；非交易时段当日无数据时返回 None 且不缓存（允许下次重试）。
- 删除了不存在的备用接口 `stock_zh_a_minute_em`（akshare 无此函数）。
- `fetch_yesterday_volume(code, daily_df=None)` 新增可选参数，优先复用传入的日线数据。
- `check_all_minute_conditions(code, daily_df=None)` 新增可选参数，传递给 `fetch_yesterday_volume`。
- `fetch_minute_data` 使用 `rename_columns` 重命名列名（`day`→`time` 等）。
- `filter_trading_minutes` 增加了对时间格式的兼容处理（提取 HH:MM）。
- `check_volume_on_pullback` 缩量系数使用 `Config.PULLBACK_VOLUME_RATIO`。
- `check_patch_1` 最低价比例使用 `Config.PATCH1_MIN_PRICE_RATIO`，低于均价线时间占比上限使用 `Config.PATCH1_MAX_BELOW_AVG_PCT`。

### main.py
- 入口文件，包含交易日判断、`run_filter()` 主流程、结果打印。
- **手动触发**：`python main.py` 直接运行一次，不自动定时调度。
- `is_trading_day()` 优先使用 `ak.tool_trade_date_hist_sina()` 获取交易日历，失败时降级为硬编码节假日列表（已更新至 2025-2026）。
- **`run_filter()` 返回 dict**（双维度）：`{'plate': [...], 'concept': [...]}`，每个维度各取前 `TOP_STOCK_MAX` 只。旧版返回 list 已废弃。
- **并发筛选**（新增）：`_filter_stocks_concurrent()` 使用 `ThreadPoolExecutor`（`Config.STOCK_FILTER_MAX_WORKERS=10`）并发执行个股筛选，单股异常隔离。`_filter_one_stock()` 为线程内执行的单股筛选函数。
- **双维度输出**（新增）：
  - `_select_by_dimension(passed_stocks, plate_analyzer, dimension)`：按维度选股，并为每只股票预填展示字段（`dimension_name`、`dimension_change`、`bias`、`last_zt_date`）。
    - `dimension='plate'`：优先选择有板块归属的股票，`dimension_change` 由 `get_plate_change_percent()` 查询。
    - `dimension='concept'`：通过股票名称与热点概念关键词（概念名前 2-4 字）匹配，`matched_concepts` 为 `[{'name': str, 'change': float}, ...]`，取涨幅最高的匹配概念作为 `dimension_name`/`dimension_change`。
    - `_fill_display_fields()`：内部辅助函数，把 `details['bias']` 和 `details['zt_dates'][0]['date']` 复制到顶层字段，供打印函数使用。
  - `print_results_dual()`：以表格形式打印双维度结果，含全A指数涨幅、概念热点均值、市场概念活跃度、板块/概念排名上限。
  - `_print_stock_table(stocks, dimension_label)`：打印单个维度的表格，列：排名 | 股票名称 | 代码 | 当前涨跌幅 | 所属{板块/概念} | {板块/概念}涨跌幅 | 乖离率 | 最近涨停日 | 仓位建议。
    - `dimension_label='板块'` 或 `'概念'`，用于列头显示。
    - 中文字符按 2 倍宽度估算列宽，列间以空格分隔。
- 降级模式（东财板块成分股不可用）：遍历涨幅前 `DEGRADED_SCAN_COUNT`(200) 只股票，跳过板块归属筛选。此时板块维度可能为空，仅概念维度有结果。
- 输出数量由 `Config.TOP_STOCK_MAX`（5）控制（每个维度各自的上限）。
- `format_market_cap()` 流通市值从元转为亿元显示，与 `check_market_cap` 保持一致。
- 通过 `stock_filter.get_cached_daily_data(code)` 复用日线数据，传递给 `minute_processor.check_all_minute_conditions(code, daily_df=daily_df)`。
- 不生成 HTML 报告，不依赖 Jinja2。

## 5. 数据流

```
main.run_filter()
  │
  ├─ PlateAnalyzer.run()
  │     ├─ fetch_plate_data()         # 板块数据（列名重命名）
  │     ├─ fetch_all_a_stocks()       # 全A股实时（列名重命名）
  │     ├─ calculate_plate_rankings() # 板块排名
  │     ├─ fetch_plate_constituents() # 成分股映射（含预检优化，东财不可用时快速降级）
  │     ├─ analyze_plate_tier()       # 梯队分析（基于映射）
  │     └─ fetch_concept_rankings()   # 概念板块涨幅（限制前100个）
  │
  ├─ get_all_a_index_change()         # 全A指数涨幅
  │
  ├─ _filter_stocks_concurrent()      # 并发筛选（ThreadPoolExecutor, 10线程）
  │     └─ _filter_one_stock() per stock:
  │          ├─ StockFilter.filter_stock()              # 日线/周线/基因
  │          └─ MinuteDataProcessor.check_all_minute_conditions(daily_df)  # 分时
  │
  ├─ 按 ranking_score 降序排序
  └─ 双维度输出:
       ├─ _select_by_dimension(dimension='plate')    # 板块维度: 有板块归属的优先
       │    └─ _fill_display_fields(): 填充 dimension_name/dimension_change(=板块指数涨幅)/bias/last_zt_date
       ├─ _select_by_dimension(dimension='concept')  # 概念维度: 名称匹配热点概念优先
       │    └─ _fill_display_fields(): 填充 dimension_name(=涨幅最高的匹配概念)/dimension_change(=概念涨幅)/bias/last_zt_date
       └─ print_results_dual()                       # 控制台打印双维度表格
            └─ _print_stock_table(dimension_label='板块'/'概念')
                 列: 排名|股票名称|代码|当前涨跌幅|所属板块/概念|板块/概念涨跌幅|乖离率|最近涨停日|仓位建议
```

## 6. 修改规范

### 6.1 阈值调整
- **所有阈值必须通过 `config.py` 修改**，禁止在业务模块中硬编码数值。
- 新增阈值时，在 `config.py` 中添加带注释的类属性，并在对应模块引用 `Config.XXX`。

### 6.2 新增筛选条件
- 个股日线级别条件 → 加到 `StockFilter.filter_stock()`，遵循"返回 `(ok, msg)` 元组"的既有模式。
- 分时级别条件 → 加到 `MinuteDataProcessor.check_all_minute_conditions()`，并在 `details` 中记录说明。
- 板块级别条件 → 加到 `PlateAnalyzer`，通过 `plate_stats` 传递。
- 每个新条件都应有独立的 `check_xxx` 方法，保持单一职责。

### 6.3 数据接口
- 主数据源为 akshare。如某接口不稳定，参照已有"备用接口"模式（try 主接口 → except → try 备用接口）。
- 新增数据获取方法必须包含 try-except 与日志，失败返回 `None` 或 `False`，不得抛出未捕获异常。
- tushare 需在 `config.py` 配置 `TU_SHARE_TOKEN`。
- 所有 akshare 数据在获取后统一使用 `rename_columns()` 重命名中文列为英文，确保下游代码字段引用一致。新增数据源时，在 `config.py` 中添加对应的 COLUMN_MAP 并在 fetch 方法中调用 `rename_columns`。

### 6.4 错误处理
- 所有外部数据调用（网络请求）必须有 try-except。
- 单只股票处理异常不应中断整体流程（参照 `main.py` 中遍历的 try-except-continue 模式）。
- 日志使用 `logger.error(..., exc_info=True)` 记录异常堆栈。

### 6.5 结果输出
- 修改输出字段或格式时，编辑 `main.py` 中的 `print_results_dual()` 和 `_print_stock_table()` 函数。
- **双维度输出**：`run_filter()` 返回 `{'plate': [...], 'concept': [...]}` dict，每个维度各取前 `TOP_STOCK_MAX` 只。
  - 板块维度：`_select_by_dimension(dimension='plate')` 优先选择有板块归属的股票，`dimension_name` = 板块名，`dimension_change` = 板块指数涨幅（`get_plate_change_percent`）。
  - 概念维度：`_select_by_dimension(dimension='concept')` 优先选择名称匹配热点概念关键词的股票，`dimension_name` = 涨幅最高的匹配概念名，`dimension_change` = 该概念涨幅（`get_concept_change_percent`）。
- **表格列**（`_print_stock_table`，按用户要求定义）：
  1. 排名
  2. 股票名称
  3. 股票代码
  4. 当前涨跌幅（`change_percent`）
  5. 所属板块/概念（`dimension_name`，板块维度显示行业板块名，概念维度显示匹配的概念名）
  6. 板块/概念当前涨跌幅（`dimension_change`）
  7. 乖离率（`bias`，来自 `details['bias']`）
  8. 最近涨停日（`last_zt_date`，来自 `details['zt_dates'][0]['date']`，无则 `-`）
  9. 仓位建议（`comment`，由 `generate_comment` 生成）
- 表格新增列时，需同步在 `_select_by_dimension` 的 `_fill_display_fields()` 中填充对应字段，否则打印取值会异常。
- 输出数量上限由 `Config.TOP_STOCK_MAX` 控制（每个维度各自的上限，默认 5）。
- 流通市值显示由 `format_market_cap()` 处理，输入单位为元（akshare 原始单位），函数内自动转为亿元显示。
- 板块/概念排名上限由 `Config.MAX_PLATE_RANK`（行业板块，默认 20）和 `Config.MAX_CONCEPT_RANK`（概念板块，默认 20）控制。

## 7. 重要约束

1. **交易日判断**：`is_trading_day()` 优先使用 akshare 交易日历接口（`tool_trade_date_hist_sina`），失败时降级为硬编码节假日列表（已覆盖 2024-2026）。2026 年日期为预估，以官方公告为准。
2. **手动触发**：程序不再自动定时调度，需手动执行 `python main.py`。建议在交易日 10:00 后运行以保证分时数据完整。
3. **输出形式**：结果直接打印到控制台，不生成 HTML 文件。如需恢复报告生成能力，需重新引入 `HTMLGenerator` 与 Jinja2 依赖。
4. **缓存策略**：日线与分时数据在单次运行内缓存，不持久化。如需跨日复用，需额外实现持久化层。
5. **数据字段兼容**：akshare 不同版本字段名可能变化，代码中已用 `df.get('A', df.get('B', default))` 模式做兼容，新增代码应延续此风格。
6. **分时数据当日过滤**：`stock_zh_a_minute` 返回最近5个交易日数据，`fetch_minute_data` 必须按当日日期过滤后再使用，否则所有分时条件会混入历史数据。新增分时相关代码时注意此约束。
7. **流通市值单位**：akshare `stock_zh_a_spot_em` 返回的 `流通市值` 单位为**元**，所有比较与显示前必须除以 1e8 转为亿元（已在 `check_market_cap` 和 `format_market_cap` 中处理）。
8. **阈值集中管理**：所有数值阈值（含分时系数、仓位建议阈值、输出数量、并发数等）必须定义在 `config.py` 的 `Config` 类中，禁止业务模块硬编码。
9. **性能**（已全面优化）：
    - 板块成分股：线程池并发（`PLATE_CONS_MAX_WORKERS=5`）+ 预检机制（东财不可用时快速降级，节省 ~110s）
    - 概念涨幅：线程池并发 + 数量限制（`CONCEPT_FETCH_LIMIT=100`，从 371 减至 100）
    - 个股筛选：线程池并发（`STOCK_FILTER_MAX_WORKERS=10`），单股异常隔离
    - 网络超时：`REQUEST_SOCKET_TIMEOUT=15` 全局 socket 超时，避免默认 2 分钟等待
    - 实测：2026-07-17 回测从 216s 降至 76s（减少 65%）
10. **数据源降级机制**：东财接口不可用时，各模块自动降级到新浪/同花顺接口：
    - 板块数据：东财 `stock_board_industry_name_em` → 同花顺 `stock_board_industry_name_ths` + `stock_board_industry_index_ths`（遍历计算涨幅）
    - 全A股：东财 `stock_zh_a_spot_em` → 新浪 `stock_zh_a_spot`（代码统一转为纯数字，无流通市值列时市值筛选跳过）
    - 日线：东财 `stock_zh_a_hist` → 新浪 `stock_zh_a_daily`（symbol 需带 sh/sz/bj 前缀，无涨跌幅列时自动计算）
    - 周线：东财周线接口 → 日线重采样为周线（W-FRI）
    - 分时：新浪 `stock_zh_a_minute`（symbol 需带前缀，`code_to_symbol()` 工具函数负责转换）
    - 全A指数：东财指数接口 → 全A股平均涨幅
    - 板块成分股：东财不可用时无替代，进入**降级模式**（`degraded_mode=True`），遍历涨幅前 `DEGRADED_SCAN_COUNT`(200) 只股票，跳过板块归属与市值筛选
11. **symbol 前缀转换**：新浪接口（`stock_zh_a_minute`/`stock_zh_a_daily`）要求 symbol 带 `sh`/`sz`/`bj` 前缀，东财接口使用纯数字代码。`config.py` 的 `code_to_symbol()` 和 `symbol_to_code()` 负责两种格式互转。

## 8. 测试与验证

- 本项目无自动化测试。修改后建议用 `python main.py` 手动触发验证。
- 非交易时段运行时，分时数据可能为空，属正常现象。
- 数据源接口偶发失败时，查看 `stock_filter.log` 排查。

## 9. 常见问题排查

| 现象 | 可能原因 | 处理方式 |
|------|---------|---------|
| 板块数据为空 | akshare 接口变更 | 检查 `ak.stock_board_industry()` 返回字段 |
| 分时数据获取失败 | 非交易时段或接口限流 | 确认在交易时段运行；查看日志 |
| 报告生成失败 | 结果字段缺失 | 检查 `filter_stock()` 结果字段是否完整填充 |
| 全 A 指数为 0 | 接口异常 | 查看 `get_all_a_index_change` 日志 |
| 节假日仍执行 | 节假日列表未更新 | 更新 `main.py` 中 `holidays` 列表 |
