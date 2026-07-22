# 修复分时数据获取 + 缩小筛选范围至前5板块/概念

## 概要

两个独立改动：
1. **分时数据修复**：在 `fetch_minute_data` 中新增东方财富接口 `ak.stock_zh_a_hist_min_em` 作为主接口，解决新浪接口今日价格字段全为 NaN 的问题。
2. **缩小筛选范围**：将板块/概念排名上限从 20 改为 5，新增概念板块成分股获取，将概念前5的成分股也纳入筛选范围。

---

## 当前状态分析

### 问题1：分时数据价格字段为 NaN

- `minute_data_processor.py:30` 的 `fetch_minute_data` 仅使用 `ak.stock_zh_a_minute`（新浪接口）
- 实测：新浪接口今日数据的 close/open/high/low 全为 NaN，仅 volume/amount 有值
- `agent.md:94` 记录"删除了不存在的备用接口 `stock_zh_a_minute_em`"——但 **`ak.stock_zh_a_hist_min_em` 是不同的函数，确实存在且返回有效价格数据**
- 实测 `ak.stock_zh_a_hist_min_em(symbol='603725', period='1', adjust='qfq')` 返回 1133 条数据，价格字段完整，还自带"均价"列
- 返回列名：`['时间', '开盘', '收盘', '最高', '最低', '成交量', '成交额', '均价']`
- 接受纯数字代码（如 `'603725'`），不需要 sh/sz 前缀

### 问题2：筛选范围过大，概念板块未纳入筛选

- `config.py:31` `MAX_PLATE_RANK = 20`：行业板块取前20名
- `config.py:96` `MAX_CONCEPT_RANK = 20`：仅用于热点概念列表和均值计算，**概念成分股从未获取**
- `main.py:248-263`：正常模式仅遍历行业板块成分股，概念板块完全不参与筛选
- `plate_analyzer.py:373-389` `is_plate_qualified`：双重条件（rank ≤ 阈值 **且** 涨幅≥5%家数≥3 或 涨停≥1），top5 板块可能因梯队不足被淘汰

---

## 改动方案

### 改动1：`minute_data_processor.py` — 新增东财分时接口

**文件**：`/workspace/minute_data_processor.py`
**方法**：`fetch_minute_data`（行 16-69）

**改动内容**：
1. 将 `ak.stock_zh_a_hist_min_em` 设为主接口（返回有效价格），新浪 `ak.stock_zh_a_minute` 设为降级接口
2. 东财接口使用纯数字代码，不需要 `code_to_symbol` 转换
3. 东财接口返回中文列名，复用现有 `COLUMN_MAP_MINUTE` 映射（已包含 '时间'→'time' 等映射）
4. 东财接口自带"均价"列，保留但不强制使用（现有 `calculate_avg_price_line` 逻辑不变）
5. 日期过滤逻辑保持不变（`get_target_date_str()` 过滤当日数据）

**关键代码逻辑**：
```python
def fetch_minute_data(self, code):
    code = str(code)
    if code in self.minute_data_cache:
        return self.minute_data_cache[code]

    df = None

    # 主接口：东财分时（返回有效价格数据）
    if is_eastmoney_available():
        try:
            throttle('eastmoney')
            df = ak.stock_zh_a_hist_min_em(symbol=code, period='1', adjust='qfq')
            # 列名映射：时间→time, 开盘→open, 收盘→close, ...
            df = rename_columns(df, COLUMN_MAP_MINUTE)
        except Exception as e:
            logger.warning(f"东财分时接口失败，降级新浪: {e}")
            df = None

    # 降级接口：新浪分时
    if df is None or df.empty:
        try:
            symbol = code_to_symbol(code)
            throttle('sina')
            df = ak.stock_zh_a_minute(symbol=symbol, period='1', adjust='qfq')
            df = rename_columns(df, COLUMN_MAP_MINUTE)
        except Exception as e:
            logger.error(f"获取股票 {code} 分时数据异常: {e}")
            return None

    if df is None or df.empty:
        return None

    # 过滤当日数据（原有逻辑保持不变）
    target_date_str = get_target_date_str()
    df = df.copy()
    df['_date_part'] = df['time'].astype(str).str[:10]
    today_df = df[df['_date_part'] == target_date_str].drop(columns=['_date_part'])

    if today_df.empty:
        logger.info(f"股票 {code} 当日({target_date_str})无分时数据")
        return None

    # 数据类型转换（原有逻辑）
    df = today_df.sort_values('time')
    df['volume'] = pd.to_numeric(df['volume'], errors='coerce')
    df['close'] = pd.to_numeric(df['close'], errors='coerce')
    # ... amount 处理保持不变 ...

    self.minute_data_cache[code] = df
    return df
```

**注意**：需在文件顶部增加 `from data_source import throttle, is_eastmoney_available` 的引用（当前已引用 `throttle, is_eastmoney_available`，无需修改导入）。

### 改动2：`config.py` — 调整排名阈值

**文件**：`/workspace/config.py`

| 参数 | 原值 | 新值 | 说明 |
|------|------|------|------|
| `MAX_PLATE_RANK`（行 31） | 20 | 5 | 行业板块取涨幅前5 |
| `MAX_CONCEPT_RANK`（行 96） | 20 | 5 | 概念板块取涨幅前5 |

### 改动3：`plate_analyzer.py` — 新增概念成分股获取

**文件**：`/workspace/plate_analyzer.py`

1. **`__init__`**：新增 `self.stock_concept_map = {}`（code → concept_name 映射）
2. **新增 `fetch_concept_constituents()` 方法**：
   - 仿照 `fetch_plate_constituents()`（行 223-319）的实现
   - 使用 `ak.stock_board_concept_cons_em(symbol=concept_name)` 获取概念成分股
   - 仅获取 `rank <= Config.MAX_CONCEPT_RANK`（前5）的概念
   - 东财不可用时跳过（概念成分股是东财独有接口），记录日志
   - 建立 `stock_concept_map`（code → concept_name）
3. **`run()` 方法**（行 587-594）：在 steps 列表中"分析概念板块涨幅"后新增"获取概念板块成分股"步骤

### 改动4：`main.py` — 合并概念成分股到筛选范围

**文件**：`/workspace/main.py`
**方法**：`run_filter`（行 248-263，正常模式分支）

**改动内容**：
1. 合并行业板块成分股 + 概念板块成分股，去重后作为筛选范围
2. 合并 `stock_plate_map` 和 `stock_concept_map` 为统一的 `plate_name_map`（行业优先，缺失时用概念）
3. 日志输出各来源股票数量

```python
# 正常模式：遍历行业前5 + 概念前5 的成分股
qualified_plates = {p for p, stats in plate_analyzer.plate_stats.items()
                   if stats['rank'] <= Config.MAX_PLATE_RANK}
qualified_codes = [code for code, plate in plate_analyzer.stock_plate_map.items()
                  if plate in qualified_plates]

# 合并概念前5的成分股
concept_codes = list(plate_analyzer.stock_concept_map.keys())
all_codes = set(qualified_codes) | set(concept_codes)

logger.info(f"行业板块 {len(qualified_plates)} 个，成分股 {len(qualified_codes)} 只，"
            f"概念成分股 {len(concept_codes)} 只，合并去重后 {len(all_codes)} 只")

scan_df = plate_analyzer.all_a_stocks[
    plate_analyzer.all_a_stocks['code'].isin(all_codes)
].copy()

# 合并 plate_name_map：行业板块优先，缺失时回退到概念
merged_map = dict(plate_analyzer.stock_plate_map)
for code, concept in plate_analyzer.stock_concept_map.items():
    if code not in merged_map:
        merged_map[code] = concept

passed_stocks = _filter_stocks_concurrent(
    scan_df, plate_analyzer, stock_filter, minute_processor,
    plate_name_map=merged_map
)
```

### 改动5：`plate_analyzer.py` — 放宽板块合格性判断

**文件**：`/workspace/plate_analyzer.py`
**方法**：`is_plate_qualified`（行 373-389）

**改动内容**：移除梯队条件（涨幅≥5%家数/涨停家数），仅保留 rank 判断。原因：用户要求"只取涨幅前5板块的个股"，板块本身已通过排名筛选，不需要额外的梯队门槛。

```python
def is_plate_qualified(self, plate_name):
    """判断板块是否符合条件（仅检查排名，梯队条件已移除）"""
    stats = self.plate_stats.get(plate_name)
    if not stats:
        # 概念板块无 plate_stats，直接通过（由 rank 控制）
        return True, "概念板块，跳过梯队检查"
    if stats['rank'] > Config.MAX_PLATE_RANK:
        return False, f"板块排名 {stats['rank']}，超过阈值 {Config.MAX_PLATE_RANK}"
    return True, "符合条件"
```

**影响**：`stock_filter.py:558-562` 调用 `is_plate_qualified` 时，概念板块股票（plate 为概念名）也能通过，不会被"板块数据不存在"淘汰。

---

## 假设与决策

1. **东财分时接口为主**：`stock_zh_a_hist_min_em` 返回的价格数据完整有效，设为主接口；新浪为降级。东财不可用时仍用新浪（价格可能为 NaN，但保留原有行为）。
2. **概念成分股仅东财源**：`stock_board_concept_cons_em` 是东财独有接口，无同花顺替代。东财不可用时概念成分股为空，仅靠行业板块成分股筛选。
3. **移除梯队门槛**：`is_plate_qualified` 不再检查"涨幅≥5%家数≥3 或 涨停≥1"，仅检查 rank。因为用户要求"只取前5"。
4. **降级模式不变**：东财成分股接口不可用时仍走降级模式（涨幅前100只），不在本次改动范围。
5. **概念维度展示不变**：`_select_by_dimension` 的概念维度仍通过股票名称关键词匹配，不依赖概念成分股映射。

---

## 验证步骤

1. **分时数据验证**：运行 `python3 main.py`，检查日志中是否出现"东财分时接口"相关日志，分时条件是否能正常计算（不再出现"无法获取分时数据"）
2. **筛选范围验证**：检查日志中"行业板块 X 个，成分股 Y 只，概念成分股 Z 只，合并去重后 N 只"，确认 N 远小于之前的 623
3. **结果验证**：观察筛选结果是否包含天安新材/艾艾精工等之前因分时数据缺失被淘汰的股票
4. **板块数量验证**：确认行业板块维度和概念维度各只取前5
