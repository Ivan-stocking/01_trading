import akshare as ak
import pandas as pd
import numpy as np
import logging
from datetime import datetime, timedelta
from config import Config, rename_columns, COLUMN_MAP_HIST, code_to_symbol, get_end_date, get_target_date_str
from data_source import throttle, fetch_daily_via_baostock, fetch_weekly_via_baostock, baostock_logout, is_eastmoney_available

logger = logging.getLogger(__name__)


def is_baostock_available():
    """检查 BaoStock 是否可用（模块导入 + 网络连接）"""
    from data_source import is_baostock_enabled
    return is_baostock_enabled()


class StockFilter:
    def __init__(self, plate_analyzer):
        self.plate_analyzer = plate_analyzer
        self.all_a_index_change = 0
        self.stock_data_cache = {}      # 日线缓存
        self.weekly_data_cache = {}     # 周线缓存

    def set_all_a_index_change(self, change):
        """设置全A指数涨幅"""
        self.all_a_index_change = change

    def get_cached_daily_data(self, code):
        """获取已缓存的日线数据（供外部模块复用，避免重复请求）"""
        return self.stock_data_cache.get(str(code))

    def is_registered_stock(self, code):
        """判断是否为注册制股票（创业板/科创板）"""
        code = str(code)
        if code.startswith('30'):
            return True
        if code.startswith('68'):
            return True
        return False

    def is_main_board_stock(self, code):
        """判断是否为沪深主板股票（60/00开头）

        排除创业板(30)/科创板(68)/北交所(43/83/87/88/92/920)。
        由 Config.STOCK_PREFIX_MAIN 控制允许的前缀。
        """
        code = str(code).strip().zfill(6)
        return any(code.startswith(prefix) for prefix in Config.STOCK_PREFIX_MAIN)

    def get_zt_threshold(self, code):
        """获取涨停阈值"""
        if self.is_registered_stock(code):
            return Config.ZT_THRESHOLD_REGISTERED
        return Config.ZT_THRESHOLD_MAIN

    def fetch_daily_data(self, code, days=60):
        """获取日线数据（带缓存）

        智能降级链（根据东财可用性动态选择）：
          - 东财可用：东财 → 新浪 → BaoStock
          - 东财不可用：BaoStock（主源，最稳定） → 新浪

        原因：东财 push2 API 受 IP 级风控，启动时检测一次，不可用则
        全局禁用东财接口，避免每只股票都等待超时浪费 8-15 秒。

        回测模式（Config.TARGET_DATE 非空）下 end_date 设为 TARGET_DATE。
        BaoStock 按日期回测时数据最完整、最稳定，且无 IP 风控。

        节流：按数据源分组（eastmoney/sina/baostock），同源内串行 2 秒，
        不同源可并行。
        """
        code = str(code)
        # 如果已缓存且条数足够，直接返回
        if code in self.stock_data_cache:
            cached = self.stock_data_cache[code]
            if cached is not None and len(cached) >= days:
                return cached

        end_date = get_end_date()
        if Config.TARGET_DATE:
            start_date = (datetime.strptime(Config.TARGET_DATE, '%Y-%m-%d') - timedelta(days=days * 2)).strftime('%Y%m%d')
            bs_start_date = (datetime.strptime(Config.TARGET_DATE, '%Y-%m-%d') - timedelta(days=days * 2)).strftime('%Y-%m-%d')
            bs_end_date = Config.TARGET_DATE
        else:
            start_date = (datetime.now() - timedelta(days=days * 2)).strftime('%Y%m%d')
            bs_start_date = (datetime.now() - timedelta(days=days * 2)).strftime('%Y-%m-%d')
            bs_end_date = datetime.now().strftime('%Y-%m-%d')

        df = None

        # 东财可用时优先东财，否则直接跳过东财（避免每只股票等 8-15 秒超时）
        if is_eastmoney_available():
            try:
                throttle('eastmoney')
                df = ak.stock_zh_a_hist(
                    symbol=code, period="daily",
                    start_date=start_date, end_date=end_date, adjust="qfq"
                )
            except Exception:
                pass

        # 东财失败或东财不可用：优先 BaoStock（最稳定，支持按日期回测）
        # 优化：当东财不可用时，跳过 BaoStock（网络环境可能有问题），直接走新浪
        if df is None or df.empty:
            # 只有东财可用时才尝试 BaoStock（说明网络环境正常）
            if is_eastmoney_available() and is_baostock_available():
                try:
                    df = fetch_daily_via_baostock(code, bs_start_date, bs_end_date)
                    if df is not None and not df.empty:
                        logger.debug(f"股票 {code} 日线使用 BaoStock")
                except Exception as e:
                    logger.warning(f"股票 {code} BaoStock 日线失败: {e}")
                    df = None

        # BaoStock 也失败或不可用：最后降级新浪
        if df is None or df.empty:
            try:
                throttle('sina')
                symbol = code_to_symbol(code)
                df = ak.stock_zh_a_daily(
                    symbol=symbol, start_date=start_date, end_date=end_date, adjust="qfq"
                )
            except Exception as e:
                logger.error(f"股票 {code} 日线数据所有源均失败: {e}")
                return None

        if df is None or df.empty:
            return None

        try:
            df = rename_columns(df, COLUMN_MAP_HIST)
            df = df.sort_values('date')

            # 新浪接口无 change_percent 列，需自行计算（BaoStock 已有 change_percent）
            if 'change_percent' not in df.columns:
                df['change_percent'] = df['close'].pct_change() * 100
            if 'change_amount' not in df.columns:
                df['change_amount'] = df['close'].diff()

            df['ma5'] = df['close'].rolling(window=5).mean()
            df['ma10'] = df['close'].rolling(window=10).mean()
            df['ma20'] = df['close'].rolling(window=20).mean()

            self.stock_data_cache[code] = df
            return df
        except Exception as e:
            logger.error(f"处理股票 {code} 日线数据异常: {e}")
            return None

    def fetch_weekly_data(self, code, weeks=25):
        """获取周线数据（带缓存）

        三级降级链：
          1. AKShare 东财周线接口
          2. 从日线数据重采样为周线（W-FRI）
          3. BaoStock 周线接口（frequency='w'）

        回测模式下 end_date 设为 TARGET_DATE。
        weeks 默认 25 周（确保足够计算 MA20 均线）。

        节流：每个网络请求前调用 throttle()。
        """
        code = str(code)
        if code in self.weekly_data_cache:
            return self.weekly_data_cache[code]

        end_date = get_end_date()
        if Config.TARGET_DATE:
            start_date = (datetime.strptime(Config.TARGET_DATE, '%Y-%m-%d') - timedelta(weeks=weeks + 4)).strftime('%Y%m%d')
            bs_start_date = (datetime.strptime(Config.TARGET_DATE, '%Y-%m-%d') - timedelta(weeks=weeks + 4)).strftime('%Y-%m-%d')
            bs_end_date = Config.TARGET_DATE
        else:
            start_date = (datetime.now() - timedelta(weeks=weeks + 4)).strftime('%Y%m%d')
            bs_start_date = (datetime.now() - timedelta(weeks=weeks + 4)).strftime('%Y-%m-%d')
            bs_end_date = datetime.now().strftime('%Y-%m-%d')

        # 第1级：东财周线接口（东财不可用时直接跳过）
        df = None
        if is_eastmoney_available():
            try:
                throttle('eastmoney')
                df = ak.stock_zh_a_hist(
                    symbol=code, period="weekly",
                    start_date=start_date, end_date=end_date, adjust="qfq"
                )
            except Exception:
                pass

        # 第2级：东财失败或不可用，优先 BaoStock 周线
        # 优化：当东财不可用时，跳过 BaoStock，直接从日线重采样
        if df is None or df.empty:
            if is_eastmoney_available() and is_baostock_available():
                try:
                    df = fetch_weekly_via_baostock(code, bs_start_date, bs_end_date)
                    if df is not None and not df.empty:
                        logger.debug(f"股票 {code} 周线使用 BaoStock")
                except Exception as e:
                    logger.warning(f"股票 {code} BaoStock 周线失败: {e}")
                    df = None

        # 第3级：BaoStock 也失败或不可用，从日线数据重采样为周线
        if df is None or df.empty:
            try:
                daily_df = self.fetch_daily_data(code, days=weeks * 7 + 30)
                if daily_df is not None and not daily_df.empty:
                    df = self._resample_to_weekly(daily_df)
            except Exception as e:
                logger.error(f"股票 {code} 周线数据所有源均失败: {e}")
                return None

        if df is None or df.empty:
            return None

        try:
            df = rename_columns(df, COLUMN_MAP_HIST)
            df = df.sort_values('date')
            df['ma5'] = df['close'].rolling(window=5).mean()
            df['ma10'] = df['close'].rolling(window=10).mean()
            df['ma20'] = df['close'].rolling(window=20).mean()

            self.weekly_data_cache[code] = df
            return df
        except Exception as e:
            logger.error(f"处理股票 {code} 周线数据异常: {e}")
            return None

    @staticmethod
    def _resample_to_weekly(daily_df):
        """日线数据重采样为周线数据（W-FRI 周五收盘）"""
        try:
            df = daily_df.copy()
            df['date'] = pd.to_datetime(df['date'])
            df = df.set_index('date')

            weekly = pd.DataFrame()
            weekly['open'] = df['open'].resample('W-FRI').first()
            weekly['high'] = df['high'].resample('W-FRI').max()
            weekly['low'] = df['low'].resample('W-FRI').min()
            weekly['close'] = df['close'].resample('W-FRI').last()
            weekly['volume'] = df['volume'].resample('W-FRI').sum()
            if 'amount' in df.columns:
                weekly['amount'] = df['amount'].resample('W-FRI').sum()

            weekly = weekly.dropna(subset=['close']).reset_index()
            weekly['date'] = weekly['date'].dt.strftime('%Y-%m-%d')
            return weekly
        except Exception:
            return None

    def check_market_cap(self, stock_info):
        """检查流通市值

        akshare 的 stock_zh_a_spot_em 返回的 `流通市值` 单位为**元**（数值很大），
        需转换为亿元后再与 Config.MIN_CIRCULATION_MKT_CAP（单位：亿）比较。
        兼容三种输入：纯数值（元）、带"亿"字符串、纯数值字符串（元）。
        """
        try:
            mkt_cap = stock_info.get('circulating_market_cap', 0)

            # 字符串处理
            if isinstance(mkt_cap, str):
                mkt_cap = mkt_cap.replace(',', '').strip()
                if '亿' in mkt_cap:
                    # 已是亿元单位
                    mkt_cap = float(mkt_cap.replace('亿', ''))
                else:
                    # 视为元单位
                    mkt_cap = float(mkt_cap) / 1e8
            else:
                # 数值型：akshare 默认返回元为单位
                mkt_cap = float(mkt_cap) / 1e8

            return mkt_cap >= Config.MIN_CIRCULATION_MKT_CAP
        except Exception as e:
            logger.error(f"检查流通市值异常: {e}")
            return False

    def _estimate_market_cap_from_daily(self, code):
        """从日线数据反推流通市值（东财不可用时的降级方案）

        两种计算方式（按可靠性优先）：
        1. 流通股本 × 收盘价（最准确）
           新浪 stock_zh_a_daily 返回 outstanding_share（流通股本，单位：股）
           流通市值 = outstanding_share × close
           验证：贵州茅台 outstanding_share=12.5亿股, close=1253 → 15662亿（实际约1.5-1.6万亿）

        2. 成交额 / 换手率（备用）
           需注意换手率单位差异：
           - BaoStock turn: 百分比形式（0.4673 = 0.4673%），需除以100
           - 新浪 turnover: 小数形式（0.004673 = 0.4673%），不需除以100
           通过判断 turnover > 0.1 区分（正常股票单日换手率不会超过10%即0.1）

        参数:
            code: 6位纯数字代码

        返回:
            流通市值（亿元），失败返回 0。
        """
        try:
            df = self.fetch_daily_data(code, days=5)
            if df is None or df.empty:
                return 0

            latest = df.iloc[-1]
            close = float(latest.get('close', 0))

            # 方式1：流通股本 × 收盘价（优先，最准确）
            if 'outstanding_share' in df.columns:
                outstanding = float(latest.get('outstanding_share', 0))
                if outstanding > 0 and close > 0:
                    circ_mv_yuan = outstanding * close
                    return circ_mv_yuan / 1e8

            # 方式2：成交额 / 换手率（备用）
            if 'turnover' in df.columns:
                amount = float(latest.get('amount', 0))
                turn = float(latest.get('turnover', 0))
                if amount > 0 and turn > 0:
                    # 新浪接口 amount 单位可能是"元"或"万元"，需要判断
                    # 正常成交额不会小于 1000（单位：元），如果小于 1000 可能是"万元"
                    if amount < 1000 and close > 0:
                        amount = amount * 10000  # 转为元
                    
                    # 判断换手率单位
                    if turn > 0.1:
                        circ_mv_yuan = amount / (turn / 100)
                    else:
                        circ_mv_yuan = amount / turn
                    return circ_mv_yuan / 1e8

            # 方式3：新浪接口可能有不同的列名，尝试其他可能的列
            if 'volume' in df.columns and close > 0:
                volume = float(latest.get('volume', 0))
                if volume > 0:
                    # 新浪 volume 单位通常是"股"
                    # 简单估算：假设平均价 = 收盘价，则流通市值 ≈ 成交额/换手率
                    # 但如果没有换手率，我们无法准确计算
                    # 这里只做一个粗略的估算，不用于筛选
                    pass

            logger.debug(f"股票 {code} 无法反推流通市值，列名: {df.columns.tolist()}")
            return 0
        except Exception as e:
            logger.debug(f"反推流通市值异常 {code}: {e}")
            return 0

    def check_daily_trend(self, df):
        """检查日线多头排列"""
        if df is None or len(df) < 20:
            return False, "日线数据不足"

        try:
            latest = df.iloc[-1]

            if pd.isna(latest['ma5']) or pd.isna(latest['ma10']) or pd.isna(latest['ma20']):
                return False, "均线数据缺失"

            if not (latest['ma5'] > latest['ma10'] > latest['ma20']):
                return False, "均线未呈多头排列"

            ma5_slope = latest['ma5'] - df.iloc[-5]['ma5']
            ma10_slope = latest['ma10'] - df.iloc[-10]['ma10']
            ma20_slope = latest['ma20'] - df.iloc[-20]['ma20']

            if not (ma5_slope > 0 and ma10_slope > 0 and ma20_slope > 0):
                return False, "均线未向上倾斜"

            return True, "日线多头"
        except Exception as e:
            logger.error(f"检查日线趋势异常: {e}")
            return False, str(e)

    def check_weekly_trend(self, df):
        """检查周线多头排列"""
        if df is None or len(df) < 20:
            return False, "周线数据不足"

        try:
            latest = df.iloc[-1]

            if pd.isna(latest['ma5']) or pd.isna(latest['ma10']) or pd.isna(latest['ma20']):
                return False, "周线均线数据缺失"

            if latest['ma5'] > latest['ma10'] > latest['ma20']:
                return True, "周线多头"
            return False, "周线未呈多头排列"
        except Exception as e:
            logger.error(f"检查周线趋势异常: {e}")
            return False, str(e)

    def check_bias(self, df, current_price):
        """检查乖离率"""
        if df is None or len(df) < 5:
            return False, "数据不足", 0

        try:
            ma5 = df.iloc[-1]['ma5']

            if pd.isna(ma5) or ma5 == 0:
                return False, "MA5数据无效", 0

            bias = (current_price - ma5) / ma5 * 100

            if Config.BIAS_LOWER_BOUND <= bias <= Config.BIAS_UPPER_BOUND:
                return True, f"乖离率 {bias:.2f}%", bias
            return False, f"乖离率 {bias:.2f}% 超出范围", bias
        except Exception as e:
            logger.error(f"检查乖离率异常: {e}")
            return False, str(e), 0

    def check_zt_gene(self, df, code):
        """检查涨停基因"""
        if df is None or len(df) < Config.ZT_WINDOW_DAYS:
            return False, "数据不足", []

        try:
            zt_threshold = self.get_zt_threshold(code)

            recent = df.tail(Config.ZT_WINDOW_DAYS)
            zt_dates = []

            for _, row in recent.iterrows():
                pct_change = row.get('change_percent', 0)
                if pd.notna(pct_change) and pct_change >= zt_threshold:
                    zt_dates.append({
                        'date': str(row['date'])[:10],
                        'change': float(pct_change)
                    })

            if len(zt_dates) >= 1:
                return True, f"近{Config.ZT_WINDOW_DAYS}日涨停{len(zt_dates)}次", zt_dates
            return False, f"近{Config.ZT_WINDOW_DAYS}日无涨停", zt_dates
        except Exception as e:
            logger.error(f"检查涨停基因异常: {e}")
            return False, str(e), []

    def check_breakout(self, df, current_price):
        """检查是否突破近10日新高"""
        if df is None or len(df) < Config.BREAKOUT_WINDOW_DAYS:
            return False, "数据不足"

        try:
            recent_high = df.tail(Config.BREAKOUT_WINDOW_DAYS)['high'].max()

            if current_price >= recent_high:
                return True, f"突破近{Config.BREAKOUT_WINDOW_DAYS}日新高 {recent_high:.2f}"
            return False, f"未突破近{Config.BREAKOUT_WINDOW_DAYS}日新高 {recent_high:.2f}"
        except Exception as e:
            logger.error(f"检查突破异常: {e}")
            return False, str(e)

    def check_relative_strength(self, stock_change):
        """检查相对全市场强度"""
        excess_return = stock_change - self.all_a_index_change

        if excess_return >= Config.RELATIVE_STRENGTH_THRESHOLD:
            return True, f"超额收益 {excess_return:.2f}%"
        return False, f"超额收益 {excess_return:.2f}%"

    def check_long_shadow(self, df):
        """检查长上影线"""
        if df is None or len(df) < Config.LONG_SHADOW_WINDOW:
            return True, "数据不足"

        try:
            recent = df.tail(Config.LONG_SHADOW_WINDOW)

            for i in range(len(recent) - 1):
                row = recent.iloc[i]
                high = row['high']
                low = row['low']
                close = row['close']
                open_ = row['open']

                body = abs(close - open_)
                upper_shadow = high - max(open_, close)

                if body == 0:
                    body = 0.0001

                if upper_shadow / body > Config.LONG_SHADOW_RATIO:
                    next_row = recent.iloc[i + 1] if i + 1 < len(recent) else None

                    if next_row is not None and next_row['close'] < high:
                        return False, f"近{Config.LONG_SHADOW_WINDOW}日存在长上影线且未突破"

            return True, "无长上影线问题"
        except Exception as e:
            logger.error(f"检查长上影线异常: {e}")
            return True, str(e)

    def check_is_st(self, name):
        """检查是否为ST股"""
        if name is None:
            return False

        name = str(name)
        if 'ST' in name or 'st' in name:
            return True
        return False

    def check_new_stock(self, df):
        """检查是否为新股"""
        if df is None or df.empty:
            return True

        try:
            trading_days = len(df)

            if trading_days < Config.NEW_STOCK_DAYS:
                return True
            return False
        except Exception as e:
            logger.error(f"检查新股异常: {e}")
            return True

    def check_stock_status(self, stock_info):
        """检查股票状态（是否停牌）"""
        try:
            price = stock_info.get('current_price', 0)

            if isinstance(price, str):
                price = float(price.replace(',', ''))
            else:
                price = float(price)

            if price == 0 or pd.isna(price):
                return False, "停牌"
            return True, "正常交易"
        except Exception as e:
            logger.error(f"检查股票状态异常: {e}")
            return False, str(e)

    def check_plate_position(self, stock_change, plate_name):
        """检查个股在板块中的地位（涨幅是否大于板块均值）"""
        plate_avg = self.plate_analyzer.get_plate_avg_change(plate_name)

        if stock_change > plate_avg:
            return True, f"板块均值 {plate_avg:.2f}%, 个股涨幅 {stock_change:.2f}%"
        return False, f"板块均值 {plate_avg:.2f}%, 个股涨幅 {stock_change:.2f}%"

    def calculate_ranking_score(self, stock_change, plate_rank, excess_return):
        """计算综合排名分数"""
        plate_score = max(0, 100 - plate_rank * 3)
        excess_score = excess_return * 10
        change_score = stock_change * 5

        return plate_score + excess_score + change_score

    def filter_stock(self, stock_info):
        """筛选单只股票"""
        result = {
            'code': str(stock_info.get('code', '')),
            'name': stock_info.get('name', ''),
            'plate': stock_info.get('industry', stock_info.get('sector', '未知')),
            'current_price': stock_info.get('current_price', 0),
            'change_percent': stock_info.get('change_percent', 0),
            'circulating_market_cap': stock_info.get('circulating_market_cap', 0),
            'passed': False,
            'reasons': [],
            'details': {}
        }

        try:
            result['change_percent'] = float(result['change_percent'])
            result['current_price'] = float(result['current_price'])
        except (ValueError, TypeError):
            result['reasons'].append("价格数据异常")
            return result

        # 主板过滤：仅保留沪深主板（60/00开头），排除创业板/科创板/北交所
        if not self.is_main_board_stock(result['code']):
            result['reasons'].append("非沪深主板股票")
            return result

        if self.check_is_st(result['name']):
            result['reasons'].append("ST股")
            return result

        status_ok, status_msg = self.check_stock_status(stock_info)
        if not status_ok:
            result['reasons'].append(status_msg)
            return result

        # 流通市值筛选
        # 优先用东财实时市值；东财不可用（新浪降级模式）时用日线数据反推
        mkt_cap = stock_info.get('circulating_market_cap', 0)
        if mkt_cap and float(mkt_cap) != 0:
            # 东财实时市值
            if not self.check_market_cap(stock_info):
                result['reasons'].append(f"流通市值不足 {Config.MIN_CIRCULATION_MKT_CAP} 亿")
                return result
        else:
            # 新浪降级模式：从日线数据反推流通市值
            estimated_mv = self._estimate_market_cap_from_daily(result['code'])
            # 只有反推成功且结果合理时才进行筛选
            # 反推失败（estimated_mv=0）或结果异常小时，跳过市值筛选
            # 在新浪降级模式下，反推市值的可靠性较低，放宽筛选条件
            if estimated_mv > 0:
                if estimated_mv >= Config.MIN_CIRCULATION_MKT_CAP:
                    # 反推市值足够大，保留
                    result['details']['estimated_mv'] = round(estimated_mv, 2)
                elif estimated_mv >= 50:
                    # 反推市值在50-150亿之间，保留但标记为低可靠性
                    result['details']['estimated_mv'] = round(estimated_mv, 2)
                    logger.debug(f"股票 {result['code']} 反推市值 {estimated_mv:.1f}亿，低于阈值但保留")
                else:
                    # 反推市值太小或不可靠，跳过市值筛选（不淘汰）
                    logger.debug(f"股票 {result['code']} 反推市值 {estimated_mv:.1f}亿，不可靠，跳过市值筛选")
            else:
                # 反推失败，跳过市值筛选
                logger.debug(f"股票 {result['code']} 反推市值失败，跳过市值筛选")

        # 板块筛选（降级模式下 plate 为空，跳过板块筛选）
        if result['plate']:
            plate_ok, plate_msg = self.plate_analyzer.is_plate_qualified(result['plate'])
            if not plate_ok:
                result['reasons'].append(plate_msg)
                return result

            plate_position_ok, plate_position_msg = self.check_plate_position(
                result['change_percent'], result['plate'])
            if not plate_position_ok:
                result['reasons'].append(plate_position_msg)
                return result
        else:
            result['details']['plate_rank'] = 1  # 降级模式占位

        # 获取日线数据（BaoStock 单连接，串行获取更稳定）
        daily_df = self.fetch_daily_data(result['code'])

        if self.check_new_stock(daily_df):
            result['reasons'].append("上市不足60日")
            return result

        daily_trend_ok, daily_trend_msg = self.check_daily_trend(daily_df)
        if not daily_trend_ok:
            result['reasons'].append(daily_trend_msg)
            return result
        result['details']['daily_trend'] = daily_trend_msg

        # 获取周线数据（BaoStock 已通过 _bs_call_lock 串行化，多线程安全）
        weekly_df = self.fetch_weekly_data(result['code'])
        weekly_trend_ok, weekly_trend_msg = self.check_weekly_trend(weekly_df)
        if not weekly_trend_ok:
            result['reasons'].append(weekly_trend_msg)
            return result
        result['details']['weekly_trend'] = weekly_trend_msg

        bias_ok, bias_msg, bias_value = self.check_bias(daily_df, result['current_price'])
        result['details']['bias'] = bias_value

        # 乖离率为必须条件：超出范围直接淘汰（不再与突破做 OR）
        # 原因：乖离率过大意味着短期涨幅过大、回调风险高，即使突破新高也不应介入
        if not bias_ok:
            result['reasons'].append(bias_msg)
            return result

        # 突破作为加分项记录，但不作为筛选门槛
        breakout_ok, breakout_msg = self.check_breakout(daily_df, result['current_price'])
        result['details']['breakout'] = breakout_ok

        zt_ok, zt_msg, zt_dates = self.check_zt_gene(daily_df, result['code'])
        if not zt_ok:
            result['reasons'].append(zt_msg)
            return result
        result['details']['zt_gene'] = zt_msg
        result['details']['zt_dates'] = zt_dates

        rs_ok, rs_msg = self.check_relative_strength(result['change_percent'])
        if not rs_ok:
            result['reasons'].append(rs_msg)
            return result
        result['details']['relative_strength'] = rs_msg
        result['details']['excess_return'] = result['change_percent'] - self.all_a_index_change

        shadow_ok, shadow_msg = self.check_long_shadow(daily_df)
        if not shadow_ok:
            result['reasons'].append(shadow_msg)
            return result

        plate_info = self.plate_analyzer.get_plate_info(result['plate'])
        result['details']['plate_rank'] = plate_info.get('rank', 999)
        result['details']['plate_above_5pct'] = plate_info.get('above_5pct', 0)
        result['details']['plate_limit_up'] = plate_info.get('limit_up', 0)

        result['passed'] = True

        ranking_score = self.calculate_ranking_score(
            result['change_percent'],
            result['details']['plate_rank'],
            result['details']['excess_return']
        )
        result['details']['ranking_score'] = ranking_score

        return result

    def generate_comment(self, stock_result):
        """生成备注（融入概念热度）"""
        details = stock_result['details']
        concept_active = self.plate_analyzer.is_concept_active()

        # 基础仓位建议（基于行业板块助攻）
        if details.get('breakout') and details.get('plate_limit_up', 0) >= Config.POSITION_PLATE_LIMIT_UP_HEAVY:
            base_position = "重仓"
        elif details.get('plate_above_5pct', 0) >= Config.POSITION_PLATE_ABOVE_5PCT_NORMAL:
            base_position = "正常仓位"
        elif details.get('plate_above_5pct', 0) >= Config.POSITION_PLATE_ABOVE_5PCT_LIGHT:
            base_position = "轻仓试错"
        else:
            base_position = "轻仓试错"

        # 概念热度调整
        if concept_active:
            concept_msg = f"概念热点活跃(均值{self.plate_analyzer.concept_avg_change:.1f}%)"
            # 概念活跃时升级仓位建议
            if base_position == "轻仓试错":
                base_position = "正常仓位"
        else:
            concept_msg = f"概念热点冷清(均值{self.plate_analyzer.concept_avg_change:.1f}%)"
            # 概念冷清时降级仓位建议
            if base_position == "重仓":
                base_position = "正常仓位"

        return f"{base_position}（{concept_msg}）"
