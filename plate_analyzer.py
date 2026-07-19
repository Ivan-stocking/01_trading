import akshare as ak
import pandas as pd
import numpy as np
import logging
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from config import Config, rename_columns, COLUMN_MAP_PLATE, COLUMN_MAP_SPOT, COLUMN_MAP_INDEX, COLUMN_MAP_CONCEPT, symbol_to_code, get_end_date
from data_source import throttle, is_eastmoney_available

logger = logging.getLogger(__name__)


class PlateAnalyzer:
    def __init__(self):
        # 设置 socket 超时，避免东财不可用时每个请求等待 2 分钟默认超时
        # 全局生效，影响所有 akshare 网络请求
        import socket
        socket.setdefaulttimeout(Config.REQUEST_SOCKET_TIMEOUT)

        self.plate_data = None
        self.plate_rankings = None
        self.plate_stats = {}
        self.all_a_stocks = None
        self.stock_plate_map = {}  # code → plate_name 映射
        self.degraded_mode = False  # 降级模式（东财不可用时为True）

        # 概念板块相关
        self.concept_rankings = None      # 概念板块涨幅排名 DataFrame
        self.concept_hot_list = []        # 热点概念名称列表（前N）
        self.concept_avg_change = 0.0     # 热点概念平均涨幅

    def fetch_plate_data(self):
        """获取行业板块数据

        优先东财接口（stock_board_industry_name_em，含涨幅），失败降级同花顺接口
        （stock_board_industry_name_ths，仅 name/code，需额外计算涨幅）。
        """
        try:
            logger.info("正在获取行业板块数据...")

            # 优先东财接口（含涨幅数据）；东财不可用则直接走同花顺降级
            if is_eastmoney_available():
                try:
                    throttle('eastmoney')
                    self.plate_data = ak.stock_board_industry_name_em()
                    if self.plate_data is not None and not self.plate_data.empty:
                        self.plate_data = rename_columns(self.plate_data, COLUMN_MAP_PLATE)
                        self.plate_data['change_percent'] = pd.to_numeric(
                            self.plate_data['change_percent'], errors='coerce')
                        logger.info(f"东财接口成功，获取 {len(self.plate_data)} 个板块")
                        return True
                except Exception as e:
                    logger.warning(f"东财板块接口失败: {e}")
            else:
                logger.info("东财不可用，直接使用同花顺板块接口")

            # 降级同花顺接口（仅 name/code，无涨幅，需后续计算）
            logger.warning("降级使用同花顺板块接口（无实时涨幅，将通过板块指数计算）")
            throttle('ths')
            ths_df = ak.stock_board_industry_name_ths()
            if ths_df is None or ths_df.empty:
                logger.error("同花顺板块接口也失败")
                return False

            # 同花顺返回 name, code 两列，构造与东财兼容的结构
            self.plate_data = ths_df.copy()
            self.plate_data['change_percent'] = 0.0  # 占位，后续 fetch_plate_change 计算填充
            logger.info(f"同花顺接口成功，获取 {len(self.plate_data)} 个板块（涨幅待计算）")
            return True
        except Exception as e:
            logger.error(f"获取板块数据异常: {e}")
            return False

    def fetch_all_a_stocks(self):
        """获取全市场A股实时数据

        优先东财接口（stock_zh_a_spot_em，含市值），失败降级新浪接口
        （stock_zh_a_spot，代码带 sh/sz/bj 前缀，无流通市值/换手率）。
        新浪接口的代码统一转为纯数字（6位）以便与板块成分股映射匹配。
        """
        try:
            logger.info("正在获取全市场A股实时数据...")

            # 优先东财接口；东财不可用直接走新浪
            use_sina_fallback = True
            if is_eastmoney_available():
                try:
                    throttle('eastmoney')
                    self.all_a_stocks = ak.stock_zh_a_spot_em()
                    if self.all_a_stocks is not None and not self.all_a_stocks.empty:
                        self.all_a_stocks = rename_columns(self.all_a_stocks, COLUMN_MAP_SPOT)
                        self.all_a_stocks['code'] = self.all_a_stocks['code'].astype(str).str.zfill(6)
                        logger.info(f"东财接口成功，获取 {len(self.all_a_stocks)} 只A股")
                        use_sina_fallback = False
                except Exception as e:
                    logger.warning(f"东财A股接口失败: {e}")
                    self.all_a_stocks = None
            else:
                logger.info("东财不可用，直接使用新浪A股接口")

            # 降级新浪接口
            if use_sina_fallback:
                logger.warning("降级使用新浪A股接口（无流通市值/换手率）")
                throttle('sina')
                self.all_a_stocks = ak.stock_zh_a_spot()
                if self.all_a_stocks is None or self.all_a_stocks.empty:
                    logger.error("新浪接口也失败")
                    return False
                self.all_a_stocks = rename_columns(self.all_a_stocks, COLUMN_MAP_SPOT)
                # 新浪代码带 sh/sz/bj 前缀，统一转为纯数字6位
                self.all_a_stocks['code'] = self.all_a_stocks['code'].apply(
                    lambda x: symbol_to_code(x).zfill(6))
                # 新浪无流通市值列，用 0 占位（check_market_cap 会跳过该筛选）
                if 'circulating_market_cap' not in self.all_a_stocks.columns:
                    self.all_a_stocks['circulating_market_cap'] = 0
                    logger.warning("新浪接口无流通市值数据，市值筛选将失效")
                logger.info(f"新浪接口成功，获取 {len(self.all_a_stocks)} 只A股")

            # 确保关键列是数值类型
            self.all_a_stocks['change_percent'] = pd.to_numeric(
                self.all_a_stocks['change_percent'], errors='coerce')
            self.all_a_stocks['current_price'] = pd.to_numeric(
                self.all_a_stocks['current_price'], errors='coerce')

            # 过滤掉无价格或代码异常的记录
            self.all_a_stocks = self.all_a_stocks[
                self.all_a_stocks['code'].str.match(r'^\d{6}$', na=False)
            ].reset_index(drop=True)

            logger.info(f"有效A股数据 {len(self.all_a_stocks)} 只")
            return True
        except Exception as e:
            logger.error(f"获取A股实时数据异常: {e}")
            return False

    def calculate_plate_rankings(self):
        """计算板块涨幅排名"""
        if self.plate_data is None or self.plate_data.empty:
            logger.error("板块数据为空，无法计算排名")
            return False

        try:
            plate_df = self.plate_data.copy()
            plate_df = plate_df.dropna(subset=['change_percent'])

            # 如果涨幅全为0（同花顺降级模式），需通过板块指数计算涨幅
            # 优化：东财不可用时跳过此计算（90个请求×2秒=180秒太慢，
            # 且降级模式下板块维度本来就无成分股映射，不会出结果）
            if (plate_df['change_percent'] == 0).all():
                if is_eastmoney_available():
                    # 东财可用但返回了空涨幅，尝试同花顺计算
                    logger.info("板块涨幅为占位值，启动同花顺板块指数计算...")
                    plate_df = self._calc_plate_change_via_ths(plate_df)
                else:
                    # 东财不可用，直接跳过板块涨幅计算（降级模式）
                    logger.info("东财不可用，跳过同花顺板块涨幅计算（降级模式无板块维度结果）")
                    self.plate_rankings = plate_df  # 涨幅全0，不参与板块维度筛选
                    return True

            plate_df = plate_df.sort_values('change_percent', ascending=False)
            plate_df['rank'] = range(1, len(plate_df) + 1)

            self.plate_rankings = plate_df

            top_n = int(len(plate_df) * Config.PLATE_TOP_PERCENTILE)
            logger.info(f"板块总数: {len(plate_df)}, 前10%阈值排名: {top_n}")

            return True
        except Exception as e:
            logger.error(f"计算板块排名异常: {e}")
            return False

    def _calc_plate_change_via_ths(self, plate_df):
        """通过同花顺板块指数接口计算板块涨幅（降级模式）

        遍历每个板块，获取近5日板块指数K线，用最后两日收盘价计算涨幅。
        并发请求由 PLATE_CONS_MAX_WORKERS 控制并发度。
        """
        logger.info(f"开始并发计算 {len(plate_df)} 个板块涨幅...")

        # 获取近5个交易日数据，用于计算最新日涨幅
        # 回测模式下用 TARGET_DATE 作为 end_date
        if Config.TARGET_DATE:
            end_date = Config.TARGET_DATE.replace('-', '')
            start_date = (datetime.strptime(Config.TARGET_DATE, '%Y-%m-%d') - timedelta(days=10)).strftime('%Y%m%d')
        else:
            end_date = datetime.now().strftime('%Y%m%d')
            start_date = (datetime.now() - timedelta(days=10)).strftime('%Y%m%d')

        def _fetch_one_plate_change(plate_name):
            """获取单个板块的涨幅（线程内执行）"""
            try:
                throttle('ths')
                df = ak.stock_board_industry_index_ths(
                    symbol=plate_name, start_date=start_date, end_date=end_date)
                if df is None or df.empty:
                    return plate_name, 0.0
                # 同花顺返回中文列名，取收盘价
                close_col = '收盘价' if '收盘价' in df.columns else df.columns[4]
                closes = pd.to_numeric(df[close_col], errors='coerce').dropna()
                if len(closes) < 2:
                    return plate_name, 0.0
                # 最新日涨幅 = (今日收盘 - 昨日收盘) / 昨日收盘 * 100
                change = (closes.iloc[-1] - closes.iloc[-2]) / closes.iloc[-2] * 100
                return plate_name, float(change)
            except Exception as e:
                logger.warning(f"获取板块 '{plate_name}' 指数失败: {e}")
                return plate_name, 0.0

        changes = {}
        with ThreadPoolExecutor(max_workers=Config.PLATE_CONS_MAX_WORKERS) as executor:
            futures = {executor.submit(_fetch_one_plate_change, name): name
                       for name in plate_df['name'].tolist()}
            for future in as_completed(futures):
                name, change = future.result()
                changes[name] = change

        plate_df = plate_df.copy()
        plate_df['change_percent'] = plate_df['name'].map(changes).fillna(0)
        logger.info(f"板块涨幅计算完成，非零板块数: {(plate_df['change_percent'] != 0).sum()}")
        return plate_df

    def fetch_plate_constituents(self):
        """获取排名靠前板块的成分股，建立 code→plate 映射

        stock_zh_a_spot_em 不含行业列，必须通过成分股接口逐板块获取。
        使用线程池并发请求，由 Config.PLATE_CONS_MAX_WORKERS 控制并发度。

        优化：
        1. 设置 socket 超时（Config.REQUEST_SOCKET_TIMEOUT），避免东财不可用时
           每个请求等待 2 分钟默认超时。
        2. 预检模式（Config.PLATE_CONS_PREFLIGHT）：先单独请求第一个板块，
           若失败（说明东财整体不可用）直接进入降级模式，跳过剩余板块请求，
           节省大量等待时间。

        如果东财成分股接口全部失败（映射为空），设置 degraded_mode=True，
        后续 main.py 将降级为遍历涨幅前N只股票（跳过板块归属筛选）。
        """
        if self.plate_rankings is None:
            logger.error("板块排名数据为空")
            return False

        # 东财不可用时直接进入降级模式（板块成分股接口是东财独有的，无替代源）
        if not is_eastmoney_available():
            logger.info("东财不可用，直接进入降级模式（无板块成分股映射）")
            self.degraded_mode = True
            return True

        try:
            self.stock_plate_map = {}
            top_plates = self.plate_rankings[
                self.plate_rankings['rank'] <= Config.MAX_PLATE_RANK
            ]

            plate_names = top_plates['name'].tolist()
            logger.info(f"开始获取 {len(plate_names)} 个板块的成分股"
                        f"（并发数 {Config.PLATE_CONS_MAX_WORKERS}）...")

            def _fetch_one(plate_name):
                """单个板块成分股获取（线程内执行）"""
                try:
                    throttle('eastmoney')
                    cons_df = ak.stock_board_industry_cons_em(symbol=plate_name)
                    if cons_df is None or cons_df.empty:
                        return plate_name, []
                    cons_df = rename_columns(cons_df, COLUMN_MAP_SPOT)
                    codes = []
                    for code in cons_df['code']:
                        # 规范化代码：只保留6位数字
                        code = str(code).strip()
                        if len(code) > 6:
                            code = code[-6:]
                        codes.append(code)
                    return plate_name, codes
                except Exception as e:
                    logger.warning(f"获取板块 '{plate_name}' 成分股失败: {e}")
                    return plate_name, []

            # 预检模式：先用第一个板块测试东财可用性
            if Config.PLATE_CONS_PREFLIGHT and plate_names:
                logger.info(f"预检：测试东财成分股接口（板块: {plate_names[0]}）...")
                _, preflight_codes = _fetch_one(plate_names[0])
                if not preflight_codes:
                    # 预检失败：东财接口整体不可用，直接进入降级模式
                    logger.warning("预检失败：东财成分股接口不可用，"
                                   "直接进入降级模式，跳过剩余板块请求")
                    self.degraded_mode = True
                    return True
                # 预检成功：保存第一个板块结果，并发获取剩余板块
                for code in preflight_codes:
                    self.stock_plate_map[code] = plate_names[0]
                logger.info(f"预检成功，继续并发获取剩余 {len(plate_names) - 1} 个板块...")
                plate_names_to_fetch = plate_names[1:]
            else:
                plate_names_to_fetch = plate_names

            # 并发获取剩余板块
            if plate_names_to_fetch:
                with ThreadPoolExecutor(max_workers=Config.PLATE_CONS_MAX_WORKERS) as executor:
                    futures = {executor.submit(_fetch_one, name): name
                               for name in plate_names_to_fetch}
                    for future in as_completed(futures):
                        plate_name, codes = future.result()
                        for code in codes:
                            self.stock_plate_map[code] = plate_name

            # 降级模式：映射为空说明东财成分股接口全部失败
            if not self.stock_plate_map:
                self.degraded_mode = True
                logger.warning("板块成分股映射为空（东财接口不可用），"
                               "降级为遍历涨幅前N只股票模式，跳过板块归属筛选")
            else:
                self.degraded_mode = False
                logger.info(f"共获取 {len(self.stock_plate_map)} 只成分股映射")
            return True
        except Exception as e:
            logger.error(f"获取板块成分股异常: {e}")
            self.degraded_mode = True
            return True  # 降级模式下不中断流程

    def analyze_plate_tier(self):
        """分析板块梯队结构（基于成分股映射）"""
        # 降级模式：无成分股映射，跳过梯队分析
        if self.degraded_mode:
            logger.warning("降级模式：跳过板块梯队分析（无成分股映射）")
            return True

        if self.all_a_stocks is None or self.all_a_stocks.empty:
            logger.error("A股数据为空，无法分析板块梯队")
            return False

        if not self.stock_plate_map:
            logger.error("板块成分股映射为空")
            return False

        if self.plate_rankings is None:
            logger.error("板块排名数据为空")
            return False

        try:
            stocks_df = self.all_a_stocks.copy()

            # 通过成分股映射来匹配板块
            stocks_df['plate'] = stocks_df['code'].astype(str).map(self.stock_plate_map)

            for _, plate_row in self.plate_rankings.iterrows():
                plate_name = plate_row['name']
                plate_stocks = stocks_df[stocks_df['plate'] == plate_name]

                above_5pct = len(plate_stocks[plate_stocks['change_percent'] >= 5])
                # 涨停家数：使用 Config 中可配置的涨停阈值（主板9.5%/注册制19.5%）
                # 注：板块成分股混合主板与注册制，此为近似统计（涨幅≥主板涨停线即计入）
                limit_up_10 = len(plate_stocks[plate_stocks['change_percent'] >= Config.ZT_THRESHOLD_MAIN])
                limit_up_20 = len(plate_stocks[plate_stocks['change_percent'] >= Config.ZT_THRESHOLD_REGISTERED])
                total_limit_up = limit_up_10 + limit_up_20

                avg_change = plate_stocks['change_percent'].mean() if not plate_stocks.empty else 0

                self.plate_stats[plate_name] = {
                    'rank': plate_row['rank'],
                    'above_5pct': above_5pct,
                    'limit_up': total_limit_up,
                    'avg_change': avg_change,
                    'stock_count': len(plate_stocks)
                }

            logger.info(f"完成 {len(self.plate_stats)} 个板块的梯队分析")
            return True
        except Exception as e:
            logger.error(f"分析板块梯队异常: {e}")
            return False

    def is_plate_qualified(self, plate_name):
        """判断板块是否符合条件"""
        stats = self.plate_stats.get(plate_name)

        if not stats:
            return False, "板块数据不存在"

        if stats['rank'] > Config.MAX_PLATE_RANK:
            return False, f"板块排名 {stats['rank']}，超过阈值 {Config.MAX_PLATE_RANK}"

        has_enough_tier = (stats['above_5pct'] >= Config.PLATE_MIN_STOCKS_ABOVE_5PCT or
                           stats['limit_up'] >= Config.PLATE_MIN_STOCKS_LIMIT_UP)

        if not has_enough_tier:
            return False, f"梯队不足: 涨幅≥5% {stats['above_5pct']}只, 涨停{stats['limit_up']}只"

        return True, "符合条件"

    def get_plate_avg_change(self, plate_name):
        """获取板块平均涨幅（基于成分股）"""
        stats = self.plate_stats.get(plate_name)
        return stats['avg_change'] if stats else 0

    def get_plate_change_percent(self, plate_name):
        """获取板块本身的涨幅（板块指数涨幅，来自 plate_rankings）

        用于结果展示：显示个股所属板块当日的涨跌幅。
        与 get_plate_avg_change 的区别：
          - get_plate_avg_change：板块内成分股的平均涨幅
          - get_plate_change_percent：板块指数本身的涨幅（更准确反映板块强度）
        """
        if not plate_name or self.plate_rankings is None:
            return 0.0
        try:
            row = self.plate_rankings[self.plate_rankings['name'] == plate_name]
            if not row.empty:
                return float(row.iloc[0]['change_percent'])
        except Exception:
            pass
        return 0.0

    def get_concept_change_percent(self, concept_name):
        """获取概念板块涨幅（来自 concept_rankings）

        用于结果展示：显示个股匹配概念当日的涨跌幅。
        """
        if not concept_name or self.concept_rankings is None:
            return 0.0
        try:
            row = self.concept_rankings[self.concept_rankings['name'] == concept_name]
            if not row.empty:
                return float(row.iloc[0]['change_percent'])
        except Exception:
            pass
        return 0.0

    def get_plate_info(self, plate_name):
        """获取板块完整信息"""
        return self.plate_stats.get(plate_name, {})

    def get_stock_plate(self, code):
        """获取股票所属板块"""
        return self.stock_plate_map.get(str(code), '')

    def get_all_a_index_change(self):
        """获取全A指数涨幅

        优先东财指数接口，失败时降级为全A股平均涨幅。
        """
        # 优先东财指数接口；东财不可用直接走全A平均涨幅
        if is_eastmoney_available():
            try:
                throttle('eastmoney')
                index_data = ak.stock_zh_index_spot_em()
                index_data = rename_columns(index_data, COLUMN_MAP_INDEX)
                all_a = index_data[index_data['name'].str.contains('全A', na=False)]
                if not all_a.empty:
                    return float(all_a.iloc[0]['change_percent'])
            except Exception as e:
                logger.warning(f"东财指数接口失败，将使用全A平均涨幅: {e}")
        else:
            logger.info("东财不可用，使用全A平均涨幅替代指数涨幅")

        # 降级：用全A股平均涨幅
        if self.all_a_stocks is not None and not self.all_a_stocks.empty:
            avg = float(self.all_a_stocks['change_percent'].mean())
            logger.info(f"使用全A股平均涨幅作为基准: {avg:.2f}%")
            return avg

        return 0

    def fetch_concept_rankings(self):
        """获取概念板块涨幅排名

        通过同花顺概念板块接口获取所有概念板块涨幅，排序后取前 MAX_CONCEPT_RANK 个作为热点概念。
        用于判断市场热点情绪：热点概念平均涨幅≥CONCEPT_MIN_CHANGE 时视为市场热点活跃。

        数据源：
          - stock_board_concept_name_ths: 获取概念名称列表（仅 name/code）
          - stock_board_concept_info_ths: 逐个获取概念涨幅（返回今开/昨收/涨幅等）
        东财接口（stock_board_concept_name_em/spot_em）不可用时使用同花顺降级。
        """
        try:
            # 优先东财概念板块接口（含涨幅）；东财不可用直接走同花顺
            concept_df = None
            if is_eastmoney_available():
                try:
                    throttle('eastmoney')
                    concept_df = ak.stock_board_concept_name_em()
                    if concept_df is not None and not concept_df.empty:
                        concept_df = rename_columns(concept_df, COLUMN_MAP_CONCEPT)
                        concept_df['change_percent'] = pd.to_numeric(
                            concept_df['change_percent'].astype(str).str.replace('%', ''),
                            errors='coerce')
                        logger.info(f"东财概念接口成功，获取 {len(concept_df)} 个概念")
                except Exception as e:
                    logger.warning(f"东财概念接口失败，降级同花顺: {e}")
                    concept_df = None
            else:
                logger.info("东财不可用，直接使用同花顺概念板块接口")

            # 降级同花顺：逐个获取概念涨幅
            if concept_df is None or concept_df.empty:
                logger.info("降级使用同花顺概念板块接口（逐个获取涨幅）...")
                throttle('ths')
                name_df = ak.stock_board_concept_name_ths()
                if name_df is None or name_df.empty:
                    logger.error("同花顺概念板块名称获取失败")
                    self.concept_rankings = None
                    self.concept_hot_list = []
                    self.concept_avg_change = 0.0
                    return True  # 不中断流程

                concept_names = name_df['name'].tolist()
                # 限制概念数量以减少请求量（Config.CONCEPT_FETCH_LIMIT）
                if Config.CONCEPT_FETCH_LIMIT > 0 and len(concept_names) > Config.CONCEPT_FETCH_LIMIT:
                    logger.info(f"概念总数 {len(concept_names)}，限制获取前 {Config.CONCEPT_FETCH_LIMIT} 个")
                    concept_names = concept_names[:Config.CONCEPT_FETCH_LIMIT]
                logger.info(f"开始并发获取 {len(concept_names)} 个概念涨幅"
                            f"（并发数 {Config.PLATE_CONS_MAX_WORKERS}）...")

                def _fetch_concept_change(concept_name):
                    """获取单个概念板块涨幅（线程内执行）"""
                    try:
                        throttle('ths')
                        info = ak.stock_board_concept_info_ths(symbol=concept_name)
                        if info is None or info.empty:
                            return concept_name, 0.0
                        # 找到"板块涨幅"行
                        change_row = info[info['项目'] == '板块涨幅']
                        if not change_row.empty:
                            change_str = str(change_row.iloc[0]['值']).replace('%', '').strip()
                            return concept_name, float(change_str)
                        # 降级：用今开/昨收计算
                        open_row = info[info['项目'] == '今开']
                        prev_row = info[info['项目'] == '昨收']
                        if not open_row.empty and not prev_row.empty:
                            open_val = float(open_row.iloc[0]['值'])
                            prev_val = float(prev_row.iloc[0]['值'])
                            if prev_val > 0:
                                return concept_name, (open_val - prev_val) / prev_val * 100
                        return concept_name, 0.0
                    except Exception as e:
                        return concept_name, 0.0

                changes = {}
                with ThreadPoolExecutor(max_workers=Config.PLATE_CONS_MAX_WORKERS) as executor:
                    futures = {executor.submit(_fetch_concept_change, name): name
                               for name in concept_names}
                    for future in as_completed(futures):
                        name, change = future.result()
                        changes[name] = change

                concept_df = pd.DataFrame([
                    {'name': name, 'change_percent': change}
                    for name, change in changes.items()
                ])
                logger.info(f"同花顺概念涨幅获取完成，有效概念 {len(concept_df)} 个")

            # 排序并取前 MAX_CONCEPT_RANK 作为热点概念
            concept_df = concept_df.dropna(subset=['change_percent'])
            concept_df = concept_df.sort_values('change_percent', ascending=False)
            concept_df['rank'] = range(1, len(concept_df) + 1)

            self.concept_rankings = concept_df
            top_n = min(Config.MAX_CONCEPT_RANK, len(concept_df))
            self.concept_hot_list = concept_df.head(top_n)['name'].tolist()
            self.concept_avg_change = float(concept_df.head(top_n)['change_percent'].mean())

            logger.info(f"热点概念前{top_n}个，平均涨幅: {self.concept_avg_change:.2f}%"
                        f"（阈值 {Config.CONCEPT_MIN_CHANGE}%）")
            if self.concept_avg_change >= Config.CONCEPT_MIN_CHANGE:
                logger.info("市场概念热点活跃")
            else:
                logger.info("市场概念热点不活跃")

            return True
        except Exception as e:
            logger.error(f"获取概念板块涨幅异常: {e}")
            self.concept_rankings = None
            self.concept_hot_list = []
            self.concept_avg_change = 0.0
            return True  # 不中断流程

    def is_concept_active(self):
        """判断市场概念热点是否活跃（用于仓位建议调整）"""
        return self.concept_avg_change >= Config.CONCEPT_MIN_CHANGE

    def run(self):
        """执行完整的板块分析流程"""
        logger.info("=" * 50)
        logger.info("开始板块分析流程")
        logger.info("=" * 50)

        steps = [
            ("获取板块数据", self.fetch_plate_data),
            ("获取A股实时数据", self.fetch_all_a_stocks),
            ("计算板块排名", self.calculate_plate_rankings),
            ("获取板块成分股", self.fetch_plate_constituents),
            ("分析板块梯队", self.analyze_plate_tier),
            ("分析概念板块涨幅", self.fetch_concept_rankings)
        ]

        for step_name, step_func in steps:
            logger.info(f"执行步骤: {step_name}")
            if not step_func():
                logger.error(f"步骤 '{step_name}' 失败")
                return False

        logger.info("板块分析流程完成")
        return True
