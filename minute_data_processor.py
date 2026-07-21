import akshare as ak
import pandas as pd
import numpy as np
import logging
from datetime import datetime, timedelta
from config import Config, rename_columns, COLUMN_MAP_MINUTE, COLUMN_MAP_HIST, code_to_symbol, get_end_date, get_target_date_str
from data_source import throttle, is_eastmoney_available

logger = logging.getLogger(__name__)


class MinuteDataProcessor:
    def __init__(self):
        self.minute_data_cache = {}

    def fetch_minute_data(self, code):
        """获取当日分时数据

        注意：akshare 的 stock_zh_a_minute 默认返回最近5个交易日的1分钟数据，
        必须按当日日期过滤，否则早盘量能、9:45-10:00等条件会混入历史数据。
        """
        code = str(code)
        if code in self.minute_data_cache:
            return self.minute_data_cache[code]

        try:
            # 新浪接口要求 symbol 带交易所前缀（sh/sz/bj），纯数字会返回空
            symbol = code_to_symbol(code)
            throttle('sina')
            df = ak.stock_zh_a_minute(symbol=symbol, period="1", adjust="qfq")

            if df is None or df.empty:
                logger.warning(f"获取股票 {code} 分时数据失败（akshare未返回数据）")
                return None

            # 重命名列（stock_zh_a_minute 返回 day 而非 time）
            df = rename_columns(df, COLUMN_MAP_MINUTE)

            # 关键修复：只保留当日分时数据
            # time 列格式形如 "2024-01-15 09:31:00"，提取日期部分匹配今日
            # 回测模式下使用 Config.TARGET_DATE
            target_date_str = get_target_date_str()
            df = df.copy()
            df['_date_part'] = df['time'].astype(str).str[:10]
            today_df = df[df['_date_part'] == target_date_str].drop(columns=['_date_part'])

            if today_df.empty:
                # 非交易时段或当日尚无分时数据，返回空（不缓存，允许下次重试）
                logger.info(f"股票 {code} 当日({target_date_str})无分时数据（可能非交易时段）")
                return None

            df = today_df.sort_values('time')
            df['volume'] = pd.to_numeric(df['volume'], errors='coerce')
            df['close'] = pd.to_numeric(df['close'], errors='coerce')

            # amount 列可能不存在，用 close*volume 估算
            # 新浪分时 volume 单位为"股"，amount 单位为"元"，故 amount = close * volume
            if 'amount' not in df.columns or df['amount'].isna().all():
                df['amount'] = df['close'] * df['volume']
            else:
                df['amount'] = pd.to_numeric(df['amount'], errors='coerce')

            df = df.dropna(subset=['time', 'close', 'volume'])

            self.minute_data_cache[code] = df
            return df
        except Exception as e:
            logger.error(f"获取股票 {code} 分时数据异常: {e}")
            return None

    def fetch_yesterday_volume(self, code, daily_df=None):
        """获取昨日全天成交量

        优先使用传入的 daily_df（来自 StockFilter 缓存），避免重复请求。

        关键修复：判断日线最后一行是否为今日，避免实盘 10:00 执行时
        数据源盘前未更新今日行导致昨日量取成前日量。

        判断逻辑：
          - 实盘模式（Config.TARGET_DATE=None）：target_date_str = 今日
            * 数据源未更新今日：iloc[-1]=昨日 → 返回 iloc[-1]
            * 数据源已更新今日：iloc[-1]=今日 → 返回 iloc[-2]
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
            # 回测模式用 TARGET_DATE，实盘用今日
            if Config.TARGET_DATE:
                end_date = Config.TARGET_DATE.replace('-', '')
                start_date = (datetime.strptime(Config.TARGET_DATE, '%Y-%m-%d') - timedelta(days=10)).strftime('%Y%m%d')
            else:
                end_date = datetime.now().strftime('%Y%m%d')
                start_date = (datetime.now() - timedelta(days=10)).strftime('%Y%m%d')

            # 优先东财接口（纯数字代码）；东财不可用直接走新浪
            df = None
            if is_eastmoney_available():
                try:
                    throttle('eastmoney')
                    df = ak.stock_zh_a_hist(
                        symbol=str(code),
                        period="daily",
                        start_date=start_date,
                        end_date=end_date,
                        adjust="qfq"
                    )
                except Exception:
                    pass

            # 东财失败或不可用，降级新浪接口（需带前缀）
            if df is None or df.empty:
                symbol = code_to_symbol(code)
                throttle('sina')
                df = ak.stock_zh_a_daily(
                    symbol=symbol,
                    start_date=start_date,
                    end_date=end_date,
                    adjust="qfq"
                )

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

    def calculate_avg_price_line(self, df):
        """计算分时均价线

        均价 = 累计成交额 / 累计成交量
        新浪分时数据 volume 单位为"股"，amount 单位为"元"，
        因此均价 = cum_amount / cum_volume（无需除100）。

        验证：茅台 2026-07-17 15:00
          volume=55700股, amount=69792100元
          均价 = 69792100 / 55700 = 1253 元 ✓（与收盘价一致）
        """
        if df is None or df.empty:
            return None

        try:
            df = df.copy()

            df['cum_amount'] = df['amount'].cumsum()
            df['cum_volume'] = df['volume'].cumsum()

            # 均价 = 累计成交额(元) / 累计成交量(股)
            # 注：之前版本误以为 volume 单位是"手"而除以100，导致均价缩小100倍，
            # 使"股价在均价线上"条件永远为真（失效）
            df['avg_price'] = np.where(
                df['cum_volume'] > 0,
                df['cum_amount'] / df['cum_volume'],
                df['close']
            )

            return df
        except Exception as e:
            logger.error(f"计算均价线异常: {e}")
            return None

    def filter_trading_minutes(self, df, start_time, end_time):
        """筛选指定时间段的分时数据"""
        if df is None or df.empty:
            return None

        try:
            # 将 time 列转为字符串以便比较
            df = df.copy()
            df['time_str'] = df['time'].astype(str)

            # 提取时间部分（HH:MM 或 HH:MM:SS）
            def extract_time(t):
                t = str(t)
                # 处理 "2024-01-01 09:30:00" 或 "09:30" 等格式
                if ' ' in t:
                    t = t.split(' ')[1]
                return t[:5]  # 取 HH:MM

            df['time_str'] = df['time_str'].apply(extract_time)

            mask = (df['time_str'] >= start_time) & (df['time_str'] <= end_time)
            result = df[mask].drop(columns=['time_str'])
            return result
        except Exception as e:
            logger.error(f"筛选时间段异常: {e}")
            return None

    def check_early_volume(self, df, yesterday_volume):
        """检查早盘量能爆发"""
        if df is None or df.empty:
            return False, "分时数据为空"

        if yesterday_volume <= 0:
            return False, "昨日成交量数据无效"

        try:
            early_data = self.filter_trading_minutes(df, Config.MARKET_START_TIME, Config.ANALYSIS_TIME)

            if early_data is None or early_data.empty:
                return False, "早盘数据不足"

            early_volume = early_data['volume'].sum()

            ratio = early_volume / yesterday_volume

            if ratio >= Config.EARLY_VOLUME_RATIO_THRESHOLD:
                return True, f"早盘量比 {ratio:.2f} (阈值 {Config.EARLY_VOLUME_RATIO_THRESHOLD})"
            return False, f"早盘量比 {ratio:.2f} (阈值 {Config.EARLY_VOLUME_RATIO_THRESHOLD})"
        except Exception as e:
            logger.error(f"检查早盘量能异常: {e}")
            return False, str(e)

    def check_price_above_avg(self, df, check_time=Config.ANALYSIS_TIME):
        """检查指定时间股价是否在均价线上"""
        if df is None or df.empty:
            return False, "分时数据为空"

        try:
            df_with_avg = self.calculate_avg_price_line(df)

            if df_with_avg is None:
                return False, "无法计算均价线"

            # 提取时间用于匹配
            df_with_avg = df_with_avg.copy()
            df_with_avg['time_str'] = df_with_avg['time'].astype(str).apply(
                lambda t: str(t).split(' ')[1][:5] if ' ' in str(t) else str(t)[:5]
            )

            target_row = df_with_avg[df_with_avg['time_str'] == check_time]

            if target_row.empty:
                times = df_with_avg['time_str'].tolist()
                nearest_time = min(times, key=lambda x: abs(pd.Timestamp(x) - pd.Timestamp(check_time)))
                target_row = df_with_avg[df_with_avg['time_str'] == nearest_time]
                logger.warning(f"未找到 {check_time} 数据，使用最近时间 {nearest_time}")

            if target_row.empty:
                return False, "未找到目标时间数据"

            current_price = float(target_row.iloc[0]['close'])
            avg_price = float(target_row.iloc[0]['avg_price'])

            if current_price >= avg_price:
                return True, f"股价 {current_price:.2f} >= 均价 {avg_price:.2f}"
            return False, f"股价 {current_price:.2f} < 均价 {avg_price:.2f}"
        except Exception as e:
            logger.error(f"检查股价与均价线关系异常: {e}")
            return False, str(e)

    def check_volume_on_pullback(self, df):
        """检查回踩时是否缩量"""
        if df is None or df.empty:
            return False, "分时数据为空"

        try:
            df_with_avg = self.calculate_avg_price_line(df)

            if df_with_avg is None:
                return False, "无法计算均价线"

            trading_data = self.filter_trading_minutes(df_with_avg, Config.MARKET_START_TIME, Config.ANALYSIS_TIME)

            if trading_data is None or len(trading_data) < 10:
                return False, "交易数据不足"

            pullback_periods = []
            rally_periods = []

            for i in range(1, len(trading_data)):
                prev_close = trading_data.iloc[i-1]['close']
                curr_close = trading_data.iloc[i]['close']
                curr_avg = trading_data.iloc[i]['avg_price']
                volume = trading_data.iloc[i]['volume']

                if curr_close < prev_close and curr_close <= curr_avg:
                    pullback_periods.append(volume)
                elif curr_close > prev_close:
                    rally_periods.append(volume)

            if len(pullback_periods) == 0 or len(rally_periods) == 0:
                return True, "无明显回踩或上涨阶段"

            avg_pullback_volume = np.mean(pullback_periods)
            avg_rally_volume = np.mean(rally_periods)

            if avg_pullback_volume < avg_rally_volume * Config.PULLBACK_VOLUME_RATIO:
                return True, f"缩量回踩: 回踩均量 {avg_pullback_volume:.0f} < 上涨均量 {avg_rally_volume:.0f} * {Config.PULLBACK_VOLUME_RATIO}"
            return False, f"放量回踩: 回踩均量 {avg_pullback_volume:.0f} >= 上涨均量 {avg_rally_volume:.0f} * {Config.PULLBACK_VOLUME_RATIO}"
        except Exception as e:
            logger.error(f"检查回踩量能异常: {e}")
            return False, str(e)

    def check_patch_1(self, df):
        """补丁1：检查9:45-10:00之间是否始终在均价线上"""
        if df is None or df.empty:
            return False, "分时数据为空"

        try:
            df_with_avg = self.calculate_avg_price_line(df)

            if df_with_avg is None:
                return False, "无法计算均价线"

            patch_data = self.filter_trading_minutes(df_with_avg, Config.PATCH_TIME_START, Config.ANALYSIS_TIME)

            if patch_data is None or patch_data.empty:
                return False, "9:45-10:00数据不足"

            nine_forty_five_row = patch_data.iloc[0]
            avg_price_at_945 = float(nine_forty_five_row['avg_price'])
            min_price_threshold = avg_price_at_945 * Config.PATCH1_MIN_PRICE_RATIO

            min_price_in_period = patch_data['close'].min()
            below_avg_count = len(patch_data[patch_data['close'] < patch_data['avg_price']])

            if min_price_in_period < min_price_threshold:
                return False, f"9:45-10:00最低价 {min_price_in_period:.2f} < 9:45均价的{Config.PATCH1_MIN_PRICE_RATIO} {min_price_threshold:.2f}"

            if below_avg_count > len(patch_data) * Config.PATCH1_MAX_BELOW_AVG_PCT:
                return False, f"9:45-10:00有{below_avg_count}/{len(patch_data)}分钟低于均价线"

            return True, f"9:45-10:00最低价 {min_price_in_period:.2f} >= 阈值 {min_price_threshold:.2f}"
        except Exception as e:
            logger.error(f"检查补丁1异常: {e}")
            return False, str(e)

    def check_all_minute_conditions(self, code, daily_df=None):
        """检查所有分时条件

        参数:
            code: 股票代码
            daily_df: 可选，已缓存的日线数据（来自 StockFilter），避免重复请求昨日成交量
        """
        result = {
            'passed': False,
            'reasons': [],
            'details': {}
        }

        minute_df = self.fetch_minute_data(code)

        if minute_df is None or minute_df.empty:
            result['reasons'].append("无法获取分时数据")
            return result

        # 复用日线数据获取昨日成交量，避免重复 API 调用
        yesterday_volume = self.fetch_yesterday_volume(code, daily_df)

        early_volume_ok, early_volume_msg = self.check_early_volume(minute_df, yesterday_volume)
        result['details']['early_volume'] = early_volume_msg

        if not early_volume_ok:
            result['reasons'].append(early_volume_msg)

        price_above_avg_ok, price_above_avg_msg = self.check_price_above_avg(minute_df)
        result['details']['price_above_avg'] = price_above_avg_msg

        if not price_above_avg_ok:
            result['reasons'].append(price_above_avg_msg)

        pullback_ok, pullback_msg = self.check_volume_on_pullback(minute_df)
        result['details']['pullback'] = pullback_msg

        if not pullback_ok:
            result['reasons'].append(pullback_msg)

        patch1_ok, patch1_msg = self.check_patch_1(minute_df)
        result['details']['patch1'] = patch1_msg

        if not patch1_ok:
            result['reasons'].append(patch1_msg)

        if early_volume_ok and price_above_avg_ok and pullback_ok and patch1_ok:
            result['passed'] = True

        return result
