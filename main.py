import logging
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from config import Config
from data_source import throttle, check_eastmoney_available

logging.basicConfig(
    level=getattr(logging, Config.LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('stock_filter.log'),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)

# 输出个股数量上限（统一从 Config 读取，便于集中调整）
TOP_STOCK_MAX = Config.TOP_STOCK_MAX

# 降级模式下遍历的涨幅前N只股票数量（东财板块成分股不可用时使用）
# 注：东财 UA 修复后大部分场景可走正常模式，降级模式仅在东财完全不可用时触发
# 按数据源分组节流后并发性能良好，可适当提高扫描数量
DEGRADED_SCAN_COUNT = 100


def is_trading_day():
    """判断是否为交易日

    优先使用 akshare 交易日历接口，失败时降级为硬编码节假日列表。
    """
    today = datetime.now()

    if today.weekday() >= 5:
        logger.info(f"今日是{today.strftime('%A')}，非交易日")
        return False

    today_str = today.strftime('%Y-%m-%d')

    # 优先使用 akshare 交易日历
    try:
        import akshare as ak
        throttle('sina')
        trade_dates = ak.tool_trade_date_hist_sina()
        # trade_date 可能是 datetime 对象或字符串，统一转为 YYYY-MM-DD
        trade_date_strs = [str(d)[:10] for d in trade_dates['trade_date'].tolist()]
        if today_str not in trade_date_strs:
            logger.info(f"今日{today_str}不在交易日历中（akshare接口）")
            return False
        return True
    except Exception as e:
        logger.warning(f"获取 akshare 交易日历失败，降级为硬编码列表: {e}")

    # 降级：硬编码节假日列表（2024-2026）
    holidays = [
        # 2024
        '2024-01-01', '2024-02-10', '2024-02-11', '2024-02-12', '2024-02-13', '2024-02-14',
        '2024-04-04', '2024-04-05', '2024-05-01', '2024-05-02', '2024-05-03',
        '2024-06-10', '2024-06-11', '2024-06-12',
        '2024-09-15', '2024-09-16', '2024-09-17',
        '2024-10-01', '2024-10-02', '2024-10-03', '2024-10-04', '2024-10-05',
        '2024-10-06', '2024-10-07', '2024-12-31',
        # 2025
        '2025-01-01',
        '2025-01-28', '2025-01-29', '2025-01-30', '2025-01-31',
        '2025-02-01', '2025-02-02', '2025-02-03', '2025-02-04',
        '2025-04-04', '2025-04-05', '2025-04-06',
        '2025-05-01', '2025-05-02', '2025-05-03', '2025-05-04', '2025-05-05',
        '2025-05-31', '2025-06-01', '2025-06-02',
        '2025-10-01', '2025-10-02', '2025-10-03', '2025-10-04',
        '2025-10-05', '2025-10-06', '2025-10-07', '2025-10-08',
        # 2026（预估，以官方公告为准）
        '2026-01-01', '2026-01-02', '2026-01-03',
        '2026-02-15', '2026-02-16', '2026-02-17', '2026-02-18',
        '2026-02-19', '2026-02-20', '2026-02-21',
        '2026-04-04', '2026-04-05', '2026-04-06',
        '2026-05-01', '2026-05-02', '2026-05-03', '2026-05-04', '2026-05-05',
        '2026-06-19', '2026-06-20', '2026-06-21',
        '2026-09-25', '2026-09-26', '2026-09-27',
        '2026-10-01', '2026-10-02', '2026-10-03', '2026-10-04',
        '2026-10-05', '2026-10-06', '2026-10-07',
    ]

    if today_str in holidays:
        logger.info(f"今日{today_str}是节假日")
        return False

    return True


def format_market_cap(value):
    """格式化流通市值显示

    akshare 返回的流通市值单位为元，统一转为亿元显示。
    兼容：数值（元）、带"亿"字符串、纯数值字符串（元）。
    """
    try:
        if isinstance(value, str):
            value = value.replace(',', '').strip()
            if '亿' in value:
                # 已是亿元单位
                return f"{float(value.replace('亿', '')):.2f}亿"
            # 元单位字符串
            return f"{float(value) / 1e8:.2f}亿"
        # 数值型：元为单位
        return f"{float(value) / 1e8:.2f}亿"
    except Exception:
        return str(value)


def _filter_one_stock(code, stock_row, plate_name, stock_filter, minute_processor):
    """单只股票筛选（线程内执行）

    返回 (code, stock_result, minute_result)。
    始终返回 stock_result（即使失败也含 reasons/details），便于记录分析。
    stock_result 为 None 仅在异常且无法构建结果时。
    线程安全说明：stock_filter 的缓存字典是 Python dict，
    多线程写入可能有竞争，但 GIL 下单次赋值操作原子性足够，
    且最坏情况只是重复请求一次数据，不影响正确性。
    """
    try:
        stock_info = {
            'code': code,
            'name': stock_row.get('name', ''),
            'industry': plate_name if plate_name else '',
            'current_price': stock_row.get('current_price', 0),
            'change_percent': stock_row.get('change_percent', 0),
            'circulating_market_cap': stock_row.get('circulating_market_cap', 0)
        }

        stock_result = stock_filter.filter_stock(stock_info)
        minute_result = None

        if stock_result['passed']:
            daily_df = stock_filter.get_cached_daily_data(code)
            minute_result = minute_processor.check_all_minute_conditions(
                code, daily_df=daily_df)

            if minute_result['passed']:
                stock_result['comment'] = stock_filter.generate_comment(stock_result)
                stock_result['minute_details'] = minute_result['details']
                return code, stock_result, minute_result

        return code, stock_result, minute_result
    except Exception as e:
        logger.error(f"处理股票 {code} 异常: {e}")
        return code, None, None


def _filter_stocks_concurrent(scan_list, plate_analyzer, stock_filter, minute_processor,
                              plate_name_map=None):
    """并发筛选股票

    参数:
        scan_list: 待筛选的股票 DataFrame（已排序）
        plate_analyzer: 板块分析器
        stock_filter: 股票筛选器
        minute_processor: 分时处理器
        plate_name_map: 可选，code→plate_name 映射（正常模式用）

    返回: 通过筛选的股票列表。同时将所有分析过的股票（含指标与失败原因）
          写入 stock_analysis_records.csv。
    """
    passed_stocks = []
    all_records = []  # 所有股票的筛选记录（含失败原因）
    total = len(scan_list)
    logger.info(f"开始并发筛选 {total} 只股票（并发数 {Config.STOCK_FILTER_MAX_WORKERS}）...")

    with ThreadPoolExecutor(max_workers=Config.STOCK_FILTER_MAX_WORKERS) as executor:
        futures = {}
        for _, stock_row in scan_list.iterrows():
            code = str(stock_row.get('code', ''))
            plate_name = plate_name_map.get(code, '') if plate_name_map else ''
            future = executor.submit(
                _filter_one_stock, code, stock_row, plate_name,
                stock_filter, minute_processor
            )
            futures[future] = code

        completed = 0
        for future in as_completed(futures):
            code, stock_result, minute_result = future.result()
            completed += 1
            if stock_result is not None and stock_result.get('passed') \
                    and minute_result is not None and minute_result.get('passed'):
                passed_stocks.append(stock_result)
            # 收集所有记录（含失败）
            if stock_result is not None:
                all_records.append((stock_result, minute_result))
            if completed % 50 == 0:
                logger.info(f"已处理 {completed}/{total} 只，通过 {len(passed_stocks)} 只")

    # 写入分析记录文件
    _write_analysis_records(all_records)

    return passed_stocks


def _write_analysis_records(records):
    """将所有分析过的股票记录写入 CSV 文件

    记录每只股票的代码、名称、板块、涨幅、市值、各项指标、是否通过、失败原因。
    文件名: stock_analysis_records.csv（追加模式，含日期时间列）
    """
    import csv
    from datetime import datetime

    filename = 'stock_analysis_records.csv'
    scan_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # CSV 列定义（失败原因前置，仅保留筛选阶段即可确定的基础字段）
    fieldnames = [
        '分析时间', '代码', '名称', '所属板块', '当前涨跌幅(%)', '流通市值(亿)',
        '失败原因',
        '最近涨停日', '综合评分', 'filter_stock是否通过',
        '分钟条件是否通过', '分钟失败原因',
    ]

    try:
        with open(filename, 'a', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            # 文件为空时写表头
            if f.tell() == 0:
                writer.writeheader()

            for stock_result, minute_result in records:
                details = stock_result.get('details', {}) or {}
                reasons = stock_result.get('reasons', []) or []

                # 格式化市值为亿元
                mkt_cap = stock_result.get('circulating_market_cap', 0)
                try:
                    mkt_cap_yi = float(mkt_cap) / 1e8 if mkt_cap else 0
                except (ValueError, TypeError):
                    mkt_cap_yi = 0

                # 最近涨停日（仅通过涨停基因检查的股票才有）
                zt_dates = details.get('zt_dates', []) or []
                last_zt = zt_dates[0]['date'] if zt_dates else ''

                # 分钟条件失败原因
                minute_passed = ''
                minute_fail = ''
                if minute_result is not None:
                    minute_passed = '是' if minute_result.get('passed') else '否'
                    if not minute_result.get('passed'):
                        minute_fail = '; '.join(minute_result.get('reasons', []) or [])

                writer.writerow({
                    '分析时间': scan_time,
                    '代码': stock_result.get('code', ''),
                    '名称': stock_result.get('name', ''),
                    '所属板块': stock_result.get('plate', ''),
                    '当前涨跌幅(%)': round(stock_result.get('change_percent', 0), 2),
                    '流通市值(亿)': round(mkt_cap_yi, 2),
                    '失败原因': '; '.join(reasons) if reasons else ('通过' if stock_result.get('passed') else ''),
                    '最近涨停日': last_zt,
                    '综合评分': round(details.get('ranking_score', 0), 1) if details.get('ranking_score') is not None else '',
                    'filter_stock是否通过': '是' if stock_result.get('passed') else '否',
                    '分钟条件是否通过': minute_passed,
                    '分钟失败原因': minute_fail,
                })

        logger.info(f"分析记录已写入 {filename}（共 {len(records)} 条）")
    except Exception as e:
        logger.error(f"写入分析记录文件失败: {e}")


def run_filter():
    """执行筛选流程，返回前 3-5 只强势个股

    流程：
        1. 板块分析（排名、梯队、全A指数、成分股映射、概念涨幅）
        2. 并发遍历股票，逐只执行日线/周线/基因等筛选
        3. 对通过初筛的个股执行分时量价确认
        4. 按综合评分降序，分别输出"板块维度"和"概念维度"前 3-5 只
    """
    logger.info("=" * 60)
    logger.info("开始执行A股开盘30分钟强势股筛选程序")
    logger.info("=" * 60)

    if not is_trading_day():
        logger.info("非交易日，跳过筛选")
        return []

    try:
        from plate_analyzer import PlateAnalyzer
        from stock_filter import StockFilter
        from minute_data_processor import MinuteDataProcessor

        # 启动时检测东财 push2 API 可用性（一次性，避免每只股票等超时）
        logger.info("检测东财 push2 API 可用性...")
        check_eastmoney_available()

        logger.info("初始化模块...")

        plate_analyzer = PlateAnalyzer()
        stock_filter = StockFilter(plate_analyzer)
        minute_processor = MinuteDataProcessor()

        logger.info("执行板块分析...")
        if not plate_analyzer.run():
            logger.error("板块分析失败")
            return []

        all_a_index_change = plate_analyzer.get_all_a_index_change()
        stock_filter.set_all_a_index_change(all_a_index_change)
        logger.info(f"全A指数涨幅: {all_a_index_change:.2f}%")

        if plate_analyzer.all_a_stocks is None or plate_analyzer.all_a_stocks.empty:
            logger.error("A股实时数据为空")
            return []

        # 构建 code→stock_row 查找表
        stock_lookup = {}
        for _, row in plate_analyzer.all_a_stocks.iterrows():
            code = str(row.get('code', ''))
            if code:
                stock_lookup[code] = row

        # 并发筛选股票
        if plate_analyzer.degraded_mode:
            # 降级模式：遍历涨幅前N只股票
            logger.warning("=== 降级模式：遍历涨幅前 %d 只股票 ===" % DEGRADED_SCAN_COUNT)
            sorted_stocks = plate_analyzer.all_a_stocks.sort_values(
                'change_percent', ascending=False)
            scan_list = sorted_stocks.head(DEGRADED_SCAN_COUNT)
            passed_stocks = _filter_stocks_concurrent(
                scan_list, plate_analyzer, stock_filter, minute_processor)
        else:
            # 正常模式：遍历申万二级行业前N的成分股（仅沪深主板 60/00 开头）
            qualified_plates = {p for p, stats in plate_analyzer.plate_stats.items()
                               if stats['rank'] <= Config.MAX_PLATE_RANK}
            qualified_codes = [code for code, plate in plate_analyzer.stock_plate_map.items()
                              if plate in qualified_plates]

            logger.info(f"行业板块 {len(qualified_plates)} 个，成分股 {len(qualified_codes)} 只")

            scan_df = plate_analyzer.all_a_stocks[
                plate_analyzer.all_a_stocks['code'].isin(qualified_codes)
            ].copy()
            # 过滤非沪深主板（60/00开头），排除创业板/科创板/北交所
            scan_df['code'] = scan_df['code'].astype(str).str.zfill(6)
            scan_df = scan_df[scan_df['code'].apply(
                lambda c: c.startswith(tuple(Config.STOCK_PREFIX_MAIN)))].reset_index(drop=True)
            logger.info(f"过滤非沪深主板后，实际筛选 {len(scan_df)} 只")

            passed_stocks = _filter_stocks_concurrent(
                scan_df, plate_analyzer, stock_filter, minute_processor,
                plate_name_map=plate_analyzer.stock_plate_map
            )

        logger.info(f"筛选完成: 通过 {len(passed_stocks)} 只")

        # 按综合评分降序排序
        passed_stocks.sort(key=lambda x: x['details'].get('ranking_score', 0), reverse=True)

        # 行业板块维度：取前 TOP_STOCK_MAX 只
        plate_stocks = _select_plate_stocks(passed_stocks, plate_analyzer)

        print_results(plate_stocks, all_a_index_change, plate_analyzer)

        return plate_stocks

    except Exception as e:
        logger.error(f"执行筛选程序异常: {e}", exc_info=True)
        return []


def _select_plate_stocks(passed_stocks, plate_analyzer):
    """选择前 TOP_STOCK_MAX 只行业板块维度的股票

    参数:
        passed_stocks: 通过筛选的股票列表（已按 ranking_score 降序）
        plate_analyzer: 板块分析器

    返回: 前 TOP_STOCK_MAX 只股票列表，每只额外填充展示字段：
        - dimension_name: 所属板块名称
        - dimension_change: 板块当前涨跌幅
        - bias: 乖离率
        - last_zt_date: 最近涨停日期
    """
    def _fill_display_fields(stock, dim_name, dim_change):
        s = dict(stock)
        s['dimension_name'] = dim_name or '-'
        s['dimension_change'] = dim_change
        details = s.get('details', {}) or {}
        s['bias'] = details.get('bias', 0)
        zt_dates = details.get('zt_dates', []) or []
        s['last_zt_date'] = zt_dates[0]['date'] if zt_dates else '-'
        return s

    selected = []
    for s in passed_stocks:
        plate_name = s.get('plate', '')
        if plate_name:
            plate_change = plate_analyzer.get_plate_change_percent(plate_name)
            selected.append(_fill_display_fields(s, plate_name, plate_change))
    return selected[:TOP_STOCK_MAX]


def print_results(plate_stocks, all_a_index_change, plate_analyzer):
    """以表格形式打印行业板块维度筛选结果"""
    print("\n" + "=" * 130)
    print("A股开盘30分钟强势股筛选结果（行业板块维度）")
    print("=" * 130)
    print(f"全A指数涨幅: {all_a_index_change:.2f}%    "
          f"行业板块排名上限: {Config.MAX_PLATE_RANK}")
    print("-" * 130)

    # 列出选中的前N行业板块
    top_plates = []
    if plate_analyzer.plate_rankings is not None:
        top_plates = plate_analyzer.plate_rankings[
            plate_analyzer.plate_rankings['rank'] <= Config.MAX_PLATE_RANK
        ]['name'].tolist()
    print(f"\n【前{Config.MAX_PLATE_RANK}行业板块】（申万二级行业，按涨幅降序）")
    if top_plates:
        for i, name in enumerate(top_plates, 1):
            change = plate_analyzer.get_plate_change_percent(name)
            print(f"  {i}. {name}（{change:+.2f}%）")
    else:
        print("  暂无板块数据")
    print("-" * 130)

    # 行业板块维度筛选结果
    print(f"\n【前{Config.MAX_PLATE_RANK}行业板块】筛选通过: {len(plate_stocks)} 只")
    print("-" * 130)
    if plate_stocks:
        _print_stock_table(plate_stocks, dimension_label='板块')
    else:
        print("  暂无符合条件的股票")

    print("-" * 130)
    print("=" * 130 + "\n")


def _print_stock_table(stocks, dimension_label='板块'):
    """打印股票表格

    列：排名 | 股票名称 | 代码 | 当前涨跌幅 | 所属{板块/概念} | {板块/概念}涨跌幅 | 乖离率 | 最近涨停日 | 仓位建议

    参数:
        stocks: 股票列表（已通过 _select_by_dimension 填充展示字段）
        dimension_label: '板块' 或 '概念'，用于列头显示

    说明：中文字符在 Python 格式化中按 1 个字符计宽，但显示宽度为 2，
    故列宽设置为内容最大长度 + 缓冲，列间以空格分隔以提升可读性。
    """
    # 列头（中文按 2 倍宽度估算列宽）
    header = (f"{'排名':<6}"
              f"{'股票名称':<14}"
              f"{'代码':<10}"
              f"{'当前涨跌幅':<12}"
              f"{'所属' + dimension_label:<18}"
              f"{dimension_label + '涨跌幅':<14}"
              f"{'乖离率':<10}"
              f"{'最近涨停日':<14}"
              f"{'仓位建议'}")
    print(header)
    print("-" * 130)

    for i, stock in enumerate(stocks, 1):
        # 当前涨跌幅
        change = stock.get('change_percent', 0)
        change_str = f"{change:+.2f}%"

        # 所属板块/概念名称（截断到 8 个中文字符宽度）
        dim_name = str(stock.get('dimension_name', '-') or '-')
        if dim_name != '-':
            # 截断长名称（按字符数，8 个中文字符约等于 16 显示宽度）
            dim_name_display = dim_name[:8]
        else:
            dim_name_display = '-'

        # 板块/概念涨跌幅
        dim_change = stock.get('dimension_change', 0) or 0
        dim_change_str = f"{dim_change:+.2f}%"

        # 乖离率
        bias = stock.get('bias', 0) or 0
        bias_str = f"{bias:+.2f}%"

        # 最近涨停日
        last_zt = str(stock.get('last_zt_date', '-') or '-')

        # 仓位建议
        comment = stock.get('comment', '') or ''

        line = (f"{str(i):<6}"
                f"{str(stock.get('name', '')):<14}"
                f"{str(stock.get('code', '')):<10}"
                f"{change_str:<12}"
                f"{dim_name_display:<18}"
                f"{dim_change_str:<14}"
                f"{bias_str:<10}"
                f"{last_zt:<14}"
                f"{comment}")
        print(line)


def main():
    """主函数：手动触发执行筛选"""
    run_filter()


if __name__ == '__main__':
    main()
