"""诊断行业前50成分股的筛选失败原因分布

用法：python3 diagnose_filter.py
"""
import logging
import sys
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter

logging.basicConfig(
    level=logging.ERROR,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler('diagnose.log'), logging.StreamHandler()]
)
sys.stdout = open(sys.stdout.fileno(), 'w', buffering=1)

from config import Config
from data_source import check_eastmoney_available
from plate_analyzer import PlateAnalyzer
from stock_filter import StockFilter


def main():
    print("=" * 80, flush=True)
    print("诊断行业前50成分股筛选失败原因", flush=True)
    print("=" * 80, flush=True)

    check_eastmoney_available()
    plate_analyzer = PlateAnalyzer()
    stock_filter = StockFilter(plate_analyzer)

    if not plate_analyzer.run():
        print("板块分析失败", flush=True)
        return

    all_a_index_change = plate_analyzer.get_all_a_index_change()
    stock_filter.set_all_a_index_change(all_a_index_change)
    print(f"全A指数涨幅: {all_a_index_change:.2f}%", flush=True)

    # 复用 main.py 的筛选范围：行业前50成分股
    qualified_plates = {p for p, stats in plate_analyzer.plate_stats.items()
                       if stats['rank'] <= Config.MAX_PLATE_RANK}
    qualified_codes = [code for code, plate in plate_analyzer.stock_plate_map.items()
                      if plate in qualified_plates]
    print(f"行业板块 {len(qualified_plates)} 个，成分股 {len(qualified_codes)} 只", flush=True)

    scan_df = plate_analyzer.all_a_stocks[
        plate_analyzer.all_a_stocks['code'].isin(qualified_codes)
    ].copy()
    total = len(scan_df)
    print(f"实际参与筛选: {total} 只", flush=True)

    # 第一失败原因分布 + 通过初筛的股票
    first_reason_counter = Counter()
    all_reasons_counter = Counter()  # 所有失败原因（不只第一个）
    passed_market_cap = []  # 通过市值+ST+主板初筛
    passed_filter = []      # 通过 filter_stock
    near_miss = []          # 接近通过（只差1-2个条件）

    def _filter_one(stock_row):
        code = str(stock_row.get('code', ''))
        stock_info = {
            'code': code,
            'name': stock_row.get('name', ''),
            'industry': '',
            'current_price': stock_row.get('current_price', 0),
            'change_percent': stock_row.get('change_percent', 0),
            'circulating_market_cap': stock_row.get('circulating_market_cap', 0)
        }
        try:
            result = stock_filter.filter_stock(stock_info)
            return code, stock_row, result
        except Exception as e:
            return code, stock_row, {'passed': False, 'reasons': [f"异常:{type(e).__name__}"]}

    print(f"\n开始并发诊断 {total} 只...", flush=True)
    with ThreadPoolExecutor(max_workers=Config.STOCK_FILTER_MAX_WORKERS) as executor:
        futures = {executor.submit(_filter_one, row): i
                   for i, row in scan_df.iterrows()}
        completed = 0
        for future in as_completed(futures):
            code, stock_row, result = future.result()
            completed += 1
            reasons = result.get('reasons', [])

            if result.get('passed'):
                passed_filter.append((code, stock_row, result))
            else:
                # 第一个失败原因
                first_reason = reasons[0] if reasons else '未知'
                normalized = re.sub(r'\d+\.\d+', 'X', first_reason)
                normalized = re.sub(r'\d+', 'N', normalized)
                first_reason_counter[normalized] += 1
                # 所有失败原因
                for r in reasons:
                    n = re.sub(r'\d+\.\d+', 'X', r)
                    n = re.sub(r'\d+', 'N', n)
                    all_reasons_counter[n] += 1

                # 判断是否接近通过（失败原因≤2个，且不是硬性排除如ST/停牌/市值）
                hard_excludes = ['ST', '停牌', '非沪深主板', '上市不足']
                soft_reasons = [r for r in reasons
                                if not any(h in r for h in hard_excludes)]
                if len(soft_reasons) <= 2:
                    near_miss.append((code, stock_row, reasons))

                # 通过市值+ST+主板初筛
                if not any(h in first_reason for h in hard_excludes):
                    passed_market_cap.append((code, stock_row, reasons))

            if completed % 300 == 0:
                print(f"  进度: {completed}/{total}", flush=True)

    # 输出
    print("\n" + "=" * 80, flush=True)
    print("诊断结果", flush=True)
    print("=" * 80, flush=True)
    print(f"总股票数: {total}", flush=True)
    print(f"通过 filter_stock: {len(passed_filter)} 只", flush=True)
    print(f"通过市值+ST+主板初筛（排除硬性条件）: {len(passed_market_cap)} 只", flush=True)

    print(f"\n--- 第一失败原因分布 ---", flush=True)
    for reason, count in first_reason_counter.most_common(20):
        pct = count / total * 100
        print(f"  {count:>5} 次 ({pct:4.1f}%)  {reason}", flush=True)

    print(f"\n--- 所有失败原因累计分布（前20）---", flush=True)
    for reason, count in all_reasons_counter.most_common(20):
        print(f"  {count:>5} 次  {reason}", flush=True)

    # 展示接近通过的股票（只差1-2个软条件）
    print(f"\n--- 接近通过的股票（仅差1-2个软条件，共 {len(near_miss)} 只）---", flush=True)
    # 按涨幅降序，展示前30只
    near_miss.sort(key=lambda x: x[1].get('change_percent', 0), reverse=True)
    for code, row, reasons in near_miss[:30]:
        name = row.get('name', '')
        change = row.get('change_percent', 0)
        print(f"  {code} {name}  涨幅 {change:+.2f}%  失败: {reasons}", flush=True)

    # 展示通过 filter_stock 的股票
    if passed_filter:
        print(f"\n--- 通过 filter_stock 的 {len(passed_filter)} 只 ---", flush=True)
        for code, row, result in passed_filter:
            print(f"  {code} {row.get('name','')}  涨幅 {row.get('change_percent',0):+.2f}%", flush=True)

    print("=" * 80, flush=True)


if __name__ == '__main__':
    main()
