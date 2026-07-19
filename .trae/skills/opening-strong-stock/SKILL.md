---
name: "opening-strong-stock"
description: "A股开盘30分钟强势股筛选程序调用指引。用户要求选股、筛选股票、跑选股程序时调用本 skill。支持实盘筛选、历史回测、失败诊断三种模式。"
---

# A股开盘30分钟强势股筛选

本 skill 是 [01_trading](file:///Users/ivan/Documents/trae_work/01_trading) 项目的调用指引。该项目在每日 10:00 自动筛选 3-5 只具备 3-5 日连续上涨潜力的 A 股个股，输出双维度（行业板块 + 概念板块）推荐结果。

## 何时调用本 Skill

**触发条件（满足任一即调用）：**
- 用户说："今天选什么股"、"跑一下选股程序"、"筛一下强势股"
- 用户说："回测 YYYY-MM-DD"、"测试某日的选股结果"
- 用户说："为什么没选出股"、"诊断筛选失败原因"

**不要调用本 Skill 的情况：**
- 用户只询问代码逻辑、不打算实际运行（用 Read/Grep 直接看代码即可）
- 用户要求修改筛选规则（直接编辑 config.py 等文件，无需调用本 skill）

## 项目位置

`/Users/ivan/Documents/trae_work/01_trading`

## 三种调用模式

### 模式 1：实盘筛选（默认）

每日 10:00 手动触发，扫描当日开盘后前 30 分钟符合条件的个股。

```bash
cd /Users/ivan/Documents/trae_work/01_trading && python3 main.py
```

**输出**：控制台表格，含股票名称、代码、当前涨跌幅、所属板块/概念、板块涨跌幅、乖离率、最近涨停日、仓位建议。日志写入 `stock_filter.log`。

### 模式 2：历史回测

测试指定历史日期 10:00 的选股结果（仅 K线/分时/流通股本可按日期回测，实时行情仍取最新交易日）。

```bash
cd /Users/ivan/Documents/trae_work/01_trading && python3 test_backtest.py YYYY-MM-DD
```

**示例**：
```bash
python3 test_backtest.py 2026-07-17   # 回测 7月17日
python3 test_backtest.py 2026-07-16   # 回测 7月16日
```

**输出**：板块维度通过数 + 概念维度通过数 + 个股详情。日志写入 `backtest.log`。

**已知局限**：新浪 spot 接口只返回最新交易日的实时涨幅，因此不同日期的回测会得到相同的市场环境（全A涨幅、板块涨幅相同），仅日线/周线/分时数据是真正按目标日期获取的。

### 成交量判断说明（关键）

**所有成交量判断都严格基于 09:30-10:00 的早盘数据，与执行时刻无关。**

| 执行时刻 | 新浪分时返回范围 | 实际用于判断的数据 |
|---------|----------------|------------------|
| 10:00 | 09:31-10:00（30 分钟） | 09:30-10:00（30 分钟） |
| 11:00 | 09:31-11:00（90 分钟） | **09:30-10:00**（过滤后） |
| 14:00 | 09:31-14:00（270 分钟） | **09:30-10:00**（过滤后） |

通过 [minute_data_processor.py](file:///Users/ivan/Documents/trae_work/01_trading/minute_data_processor.py) 的 `filter_trading_minutes(df, "09:30", "10:00")` 严格截断，10:01 之后的分钟不参与任何判断。

**三种成交量判断的数据来源**：

| 判断项 | 分子 | 分母 |
|--------|------|------|
| 早盘量能 | 09:30-10:00 成交量之和 | 昨日全天成交量 |
| 缩量回踩 | 09:30-10:00 内回踩段均量 vs 上涨段均量 | 无 |
| 补丁1 | 09:45-10:00 最低价、低于均价分钟数 | 无 |

**昨日成交量的智能取值**（关键修复）：

实盘 10:00 执行时，BaoStock 可能尚未更新今日行，导致 `iloc[-2]` 会取到前日量。`fetch_yesterday_volume` 通过比较日线最后一行日期与目标日期来智能选择：

| 场景 | 日线最后一行 | 取量位置 | 返回值 |
|------|------------|---------|-------|
| 实盘 BaoStock 未更新今日 | 昨日 | `iloc[-1]` | 昨日量 ✅ |
| 实盘 新浪已更新今日 | 今日 | `iloc[-2]` | 昨日量 ✅ |
| 回测模式 | 目标日期 | `iloc[-2]` | 目标日期前一日量 ✅ |

实盘盘前执行时日志会打印：
```
股票 002396 日线最后一行 2026-07-16 非目标日期 2026-07-19，昨日成交量取最后一行: 175864554
```

**修复代码位置**：[minute_data_processor.py](file:///Users/ivan/Documents/trae_work/01_trading/minute_data_processor.py) 的 `fetch_yesterday_volume` 方法

**修复前的代码（有 bug）**：

```python
def fetch_yesterday_volume(self, code, daily_df=None):
    # 优先复用已缓存的日线数据
    if daily_df is not None and len(daily_df) >= 2:
        try:
            # BUG: 无条件取倒数第二行，实盘盘前 BaoStock 未更新今日时
            # iloc[-1]=昨日, iloc[-2]=前日 → 昨日量被取成前日量
            return float(daily_df.iloc[-2]['volume'])
        except Exception:
            pass

    # 降级：独立请求日线数据
    try:
        # ... 请求逻辑略 ...
        if df is not None and len(df) >= 2:
            df = rename_columns(df, COLUMN_MAP_HIST)
            df = df.sort_values('date')
            yesterday_volume = df.iloc[-2]['volume']  # BUG: 同样的问题
            return float(yesterday_volume)
        return 0
    except Exception as e:
        logger.error(f"获取股票 {code} 昨日成交量异常: {e}")
        return 0
```

**修复后的代码（正确）**：

```python
def fetch_yesterday_volume(self, code, daily_df=None):
    """获取昨日全天成交量

    关键修复：判断日线最后一行是否为今日，避免实盘 10:00 执行时
    BaoStock 盘前未更新今日行导致昨日量取成前日量。

    判断逻辑：
      - 实盘模式（Config.TARGET_DATE=None）：target_date_str = 今日
        * BaoStock 盘前未更新：iloc[-1]=昨日 → 返回 iloc[-1]
        * 新浪已更新今日：iloc[-1]=今日 → 返回 iloc[-2]
      - 回测模式（Config.TARGET_DATE='YYYY-MM-DD'）：target_date_str = 目标日期
        * 数据已含目标日期行：iloc[-1]=目标日期 → 返回 iloc[-2]
        * 数据未含目标日期行（极少见）：iloc[-1]=目标日期前一日 → 返回 iloc[-1]
    """
    # 优先复用已缓存的日线数据
    if daily_df is not None and len(daily_df) >= 2:
        try:
            target_date_str = get_target_date_str()
            last_date_str = str(daily_df.iloc[-1]['date'])[:10]

            if last_date_str == target_date_str:
                # 最后一行是今日（或回测目标日期），昨日为倒数第二行
                return float(daily_df.iloc[-2]['volume'])
            else:
                # 最后一行不是今日（盘前未更新），最后一行即昨日
                logger.info(
                    f"股票 {code} 日线最后一行 {last_date_str} 非目标日期 {target_date_str}，"
                    f"昨日成交量取最后一行: {daily_df.iloc[-1]['volume']}"
                )
                return float(daily_df.iloc[-1]['volume'])
        except Exception:
            pass

    # 降级：独立请求日线数据（优先东财，失败用新浪）
    try:
        # ... 请求逻辑略 ...
        if df is not None and len(df) >= 2:
            df = rename_columns(df, COLUMN_MAP_HIST)
            df = df.sort_values('date')
            # 同样的判断逻辑：最后一行是否为今日
            target_date_str = get_target_date_str()
            last_date_str = str(df.iloc[-1]['date'])[:10]
            if last_date_str == target_date_str:
                return float(df.iloc[-2]['volume'])
            else:
                logger.info(
                    f"股票 {code} 降级日线最后一行 {last_date_str} 非目标日期 {target_date_str}，"
                    f"昨日成交量取最后一行: {df.iloc[-1]['volume']}"
                )
                return float(df.iloc[-1]['volume'])
        return 0
    except Exception as e:
        logger.error(f"获取股票 {code} 昨日成交量异常: {e}")
        return 0
```

**关键依赖**：`get_target_date_str()` 函数（来自 [config.py](file:///Users/ivan/Documents/trae_work/01_trading/config.py)），实盘模式返回今日 `YYYY-MM-DD`，回测模式返回 `Config.TARGET_DATE`。

**验证结果**：

| 场景 | 日线最后一行 | 返回值 | 是否正确 |
|------|------------|-------|---------|
| 回测模式 TARGET_DATE=2026-07-17 | 2026-07-17 | 175864554 (07-16量) | ✅ |
| 模拟实盘盘前未更新（移除最后一行） | 2026-07-16 | 175864554 (07-16量) | ✅ |

### 模式 3：诊断模式（排查 0 只通过）

当回测返回 0 只股票通过时，用此模式分析前 N 只股票的具体失败原因分布。

```bash
cd /Users/ivan/Documents/trae_work/01_trading && python3 test_backtest.py YYYY-MM-DD --debug-top N
```

**示例**：
```bash
python3 test_backtest.py 2026-07-17 --debug-top 20    # 诊断前 20 只
python3 test_backtest.py 2026-07-17 --debug-top 100   # 诊断前 100 只
```

**输出三部分**：
1. **失败原因分布**：归一化后聚合统计（如"流通市值不足 N 亿"出现 68 次）
2. **通过初筛的股票列表**：每只股票下一关具体卡在哪里
3. **通过 filter_stock 的股票 + 分钟条件诊断**：定位到具体的分钟条件失败原因

## 运行环境要求

- Python 3.9+
- 依赖：`akshare`、`baostock`、`pandas`、`numpy`（见 `requirements.txt`）
- 网络：需访问新浪、同花顺、BaoStock 接口（东财 push2 API 受 IP 风控，会自动降级）

## 运行耗时

- 实盘/回测模式：约 80-140 秒（降级模式下扫描涨幅前 100 只主板股票）
- 诊断模式 `--debug-top N`：N 越大耗时越长，100 只约 90 秒

## 输出解读

### 实盘/回测输出示例

```
【行业板块维度】筛选通过: 0 只
  暂无符合条件的股票

【概念板块维度】筛选通过: 2 只
  排名 股票名称  代码    当前涨跌幅  所属概念  概念涨跌幅  乖离率  最近涨停日  仓位建议
  1    星网锐捷  002396  +5.05%    -         -         +4.29% 2026-07-08  轻仓试错
```

### 诊断输出示例

```
--- 第一失败原因分布（归一化后） ---
    68 次  流通市值不足 N 亿（反推 X 亿）
    17 次  均线未呈多头排列

--- 通过市值+ST+主板 筛选的股票（共 27 只）---
  002396 星网锐捷  涨幅 5.05%  下一关失败: 通过  反推市值 226.76亿

--- 通过 filter_stock 的股票（共 1 只）---
  [002396] 星网锐捷 涨幅 5.05%
    filter_stock: 通过
    分钟条件: 失败
    分钟失败原因: ['放量回踩: 回踩均量 3439501 >= 上涨均量 3166249 * 0.8']
```

## 关键参数集中位置

所有阈值集中在 [config.py](file:///Users/ivan/Documents/trae_work/01_trading/config.py)，主要参数：

| 参数 | 含义 | 默认值 |
|------|------|--------|
| `MIN_CIRCULATION_MKT_CAP` | 流通市值下限（亿） | 150 |
| `BIAS_LOWER_BOUND` / `BIAS_UPPER_BOUND` | 乖离率范围（%） | -2.0 / 5.0 |
| `EARLY_VOLUME_RATIO_THRESHOLD` | 早盘量比阈值 | 0.35 |
| `ZT_THRESHOLD_MAIN` / `ZT_THRESHOLD_REGISTERED` | 涨停阈值（主板/注册制） | 9.5 / 19.5 |
| `PULLBACK_VOLUME_RATIO` | 缩量回踩比例 | 0.8 |
| `DEGRADED_SCAN_COUNT` | 降级模式扫描股票数 | 100 |
| `STOCK_PREFIX_MAIN` | 允许的股票代码前缀 | `['60', '00']` |

## 排除规则（硬约束）

- ST/*ST 股
- 退市整理期股票
- 上市不足 60 个交易日的新股
- 停牌股票
- 非沪深主板（创业板 30、科创板 68、北交所 43/83/87/88/92）

## 数据源说明

| 数据源 | 用途 | 节流间隔 |
|--------|------|---------|
| 新浪（akshare） | 实时行情、分时数据、日线数据 | 1.0s |
| 同花顺（akshare） | 行业板块、概念板块 | 1.0s |
| BaoStock | 日线/周线（回测首选，最稳定） | 0.2s |
| 东财 push2 | 板块成分股（受 IP 风控，启动时检测，不可用则降级） | 2.0s |

## 项目结构

```
01_trading/
├── config.py                 # 配置中心（所有阈值）
├── main.py                   # 主程序入口（实盘筛选）
├── test_backtest.py          # 回测+诊断脚本
├── plate_analyzer.py         # 板块分析
├── stock_filter.py           # 股票筛选
├── minute_data_processor.py  # 分时数据处理
├── data_source.py            # 数据源管理（节流、降级、BaoStock 单连接锁）
├── README.md
├── agent.md
└── requirements.txt
```
