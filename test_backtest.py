"""通用回测脚本：测试指定日期 10:00 时符合条件的股票

用法：
    # 回测 2026-07-17
    python3 test_backtest.py 2026-07-17

    # 回测并诊断前20只股票的失败原因
    python3 test_backtest.py 2026-07-17 --debug-top 20

    # 回测并诊断前10只股票（仅诊断不输出表格）
    python3 test_backtest.py 2026-07-17 --debug-top 10

设计原则：
    1. 单一脚本支持任意日期回测，避免按日期生成多个文件
    2. --debug-top 参数复用脚本做诊断，避免每次排错都创建临时诊断文件
    3. 诊断模式下输出每只股票的具体失败原因 + 失败原因分布统计

回测模式说明（Config.TARGET_DATE = 'YYYY-MM-DD'）：
  - 板块涨幅以指定日期为最新交易日计算
  - 日线/周线 end_date 设为该日期
  - 分时数据过滤到该日期 09:30-10:00

注意：
  1. A股实时数据（新浪 spot）返回的是该日期收盘价（无法获取10点实时价），
     涨跌幅为该日期全天涨幅，与10点时的实时涨幅可能有偏差。
  2. 板块成分股接口仍实时获取（无历史成分股接口），但与目标日期相差不大。
  3. 程序进入降级模式（东财板块成分股不可用），遍历涨幅前100只股票。
"""
import argparse
import logging
import re
from collections import Counter

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('backtest.log'),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)


def run_backtest(target_date):
    """执行回测主流程，返回 (result, elapsed)"""
    from config import Config
    Config.TARGET_DATE = target_date
    logger.info(f"=== 回测模式启动，目标日期: {Config.TARGET_DATE} 10:00 ===")

    # mock 交易日判断（绕过非交易日的限制）
    import main as main_module
    main_module.is_trading_day = lambda: True

    import time
    start_time = time.time()
    result = main_module.run_filter()
    elapsed = time.time() - start_time

    minutes = int(elapsed // 60)
    seconds = elapsed % 60
    logger.info(f"=== 回测完成 ===")
    logger.info(f"总执行时间: {elapsed:.2f} 秒 ({minutes}分{seconds:.2f}秒)")
    return result, elapsed


def print_result(result):
    """打印回测结果"""
    if isinstance(result, dict):
        plate_stocks = result.get('plate', [])
        concept_stocks = result.get('concept', [])
        logger.info(f"板块维度通过: {len(plate_stocks)} 只，"
                    f"概念维度通过: {len(concept_stocks)} 只")
        if plate_stocks:
            logger.info("=== 板块维度详情 ===")
            for i, s in enumerate(plate_stocks, 1):
                print(f"[板块] {i}. {s['name']} ({s['code']}) "
                      f"涨幅:{s['change_percent']:.2f}% "
                      f"价格:{s['current_price']:.2f} "
                      f"板块:{s.get('plate', '未知')} "
                      f"建议:{s.get('comment', '')}")
        if concept_stocks:
            logger.info("=== 概念维度详情 ===")
            for i, s in enumerate(concept_stocks, 1):
                concepts = s.get('matched_concepts', [])
                concept_str = '、'.join(concepts) if concepts else '-'
                print(f"[概念] {i}. {s['name']} ({s['code']}) "
                      f"涨幅:{s['change_percent']:.2f}% "
                      f"价格:{s['current_price']:.2f} "
                      f"板块:{s.get('plate', '未知')} "
                      f"匹配概念:{concept_str} "
                      f"建议:{s.get('comment', '')}")
    else:
        logger.info(f"符合条件股票数: {len(result)}")


def run_debug_top(target_date, top_n):
    """诊断模式：分析前 N 只主板股票的失败原因分布

    用于排查回测 0 只通过的具体原因。输出：
      1. 每只股票的第一失败原因（归一化后）的分布统计
      2. 通过初筛（市值+ST+主板）的股票列表及下一关失败原因
      3. 每只股票的详细信息（可选）
    """
    from config import Config
    Config.TARGET_DATE = target_date
    logger.info(f"=== 诊断模式：{target_date} 10:00，分析前 {top_n} 只主板股票 ===")

    import main as main_module
    main_module.is_trading_day = lambda: True

    from data_source import check_eastmoney_available
    check_eastmoney_available()

    from plate_analyzer import PlateAnalyzer
    from stock_filter import StockFilter
    from minute_data_processor import MinuteDataProcessor

    plate_analyzer = PlateAnalyzer()
    stock_filter = StockFilter(plate_analyzer)
    minute_processor = MinuteDataProcessor()

    if not plate_analyzer.run():
        logger.error("板块分析失败")
        return

    all_a_index_change = plate_analyzer.get_all_a_index_change()
    stock_filter.set_all_a_index_change(all_a_index_change)
    logger.info(f"全A指数涨幅: {all_a_index_change:.2f}%")

    # 取涨幅前 N 只主板股票
    sorted_stocks = plate_analyzer.all_a_stocks.sort_values(
        'change_percent', ascending=False)
    main_board = sorted_stocks[sorted_stocks['code'].apply(
        lambda c: str(c).strip().zfill(6).startswith(('60', '00')))]
    top_stocks = main_board.head(top_n)

    print("\n" + "=" * 100)
    print(f"前 {len(top_stocks)} 只主板股票失败原因诊断")
    print("=" * 100)

    first_reason_counter = Counter()        # 第一失败原因分布
    passed_market_cap = []                  # 通过市值+ST+主板初筛的股票
    passed_filter_stock = []                # 通过 filter_stock 的股票

    for _, stock_row in top_stocks.iterrows():
        code = str(stock_row.get('code', ''))
        name = stock_row.get('name', '')
        change = stock_row.get('change_percent', 0)
        price = stock_row.get('current_price', 0)
        mkt_cap = stock_row.get('circulating_market_cap', 0)

        stock_info = {
            'code': code, 'name': name, 'industry': '',
            'current_price': price, 'change_percent': change,
            'circulating_market_cap': mkt_cap
        }

        try:
            result = stock_filter.filter_stock(stock_info)
            first_reason = result['reasons'][0] if result['reasons'] else '通过'
            # 归一化：把具体数值替换为占位符，便于聚合统计
            normalized = re.sub(r'\d+\.\d+', 'X', first_reason)
            normalized = re.sub(r'\d+', 'N', normalized)
            first_reason_counter[normalized] += 1

            if '流通市值不足' not in first_reason and 'ST' not in first_reason \
                    and '非沪深主板' not in first_reason and '停牌' not in first_reason:
                passed_market_cap.append(
                    (code, name, change, first_reason, result.get('details', {})))

            if result['passed']:
                # 进一步检查分钟条件
                daily_df = stock_filter.get_cached_daily_data(code)
                minute_result = minute_processor.check_all_minute_conditions(
                    code, daily_df=daily_df)
                passed_filter_stock.append((code, name, change, result, minute_result))
        except Exception as e:
            print(f"  股票 {code} 异常: {e}")

    # 1. 失败原因分布
    print("\n--- 第一失败原因分布（归一化后） ---")
    for reason, count in first_reason_counter.most_common():
        print(f"  {count:4d} 次  {reason}")

    # 2. 通过初筛的股票
    print(f"\n--- 通过市值+ST+主板 筛选的股票（共 {len(passed_market_cap)} 只）---")
    for code, name, change, reason, details in passed_market_cap:
        est_mv = details.get('estimated_mv', '?')
        print(f"  {code} {name:10s} 涨幅 {change:.2f}%  "
              f"下一关失败: {reason}  反推市值 {est_mv}亿")

    # 3. 通过 filter_stock 的股票 + 分钟条件诊断
    print(f"\n--- 通过 filter_stock 的股票（共 {len(passed_filter_stock)} 只）---")
    for code, name, change, stock_result, minute_result in passed_filter_stock:
        print(f"\n  [{code}] {name} 涨幅 {change:.2f}%")
        print(f"    filter_stock: 通过  详情: {stock_result['details']}")
        print(f"    分钟条件: {'通过' if minute_result['passed'] else '失败'}")
        if minute_result['reasons']:
            print(f"    分钟失败原因: {minute_result['reasons']}")
        print(f"    分钟详情: {minute_result['details']}")



def main():
    parser = argparse.ArgumentParser(
        description='A股开盘30分钟强势股筛选 - 回测脚本',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  python3 test_backtest.py 2026-07-17                # 回测 2026-07-17
  python3 test_backtest.py 2026-07-17 --debug-top 20 # 诊断前20只失败原因
        """
    )
    parser.add_argument('date', help='回测目标日期，格式 YYYY-MM-DD')
    parser.add_argument('--debug-top', type=int, default=None,
                        help='诊断模式：分析前 N 只主板股票的失败原因分布')
    args = parser.parse_args()

    if args.debug_top:
        run_debug_top(args.date, args.debug_top)
    else:
        result, elapsed = run_backtest(args.date)
        print_result(result)
        return result, elapsed


if __name__ == '__main__':
    main()
