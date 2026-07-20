class Config:
    OUTPUT_DIR = "/Users/ivan/Documents/trae_work/01_trading/output"

    MARKET_START_TIME = "09:30"
    ANALYSIS_TIME = "10:00"
    PATCH_TIME_START = "09:45"

    MIN_CIRCULATION_MKT_CAP = 50

    EARLY_VOLUME_RATIO_THRESHOLD = 0.35

    BIAS_LOWER_BOUND = -2.0
    BIAS_UPPER_BOUND = 5.0

    PLATE_TOP_PERCENTILE = 0.10
    PLATE_MIN_STOCKS_ABOVE_5PCT = 3
    PLATE_MIN_STOCKS_LIMIT_UP = 1

    RELATIVE_STRENGTH_THRESHOLD = 2.0

    ZT_WINDOW_DAYS = 20
    ZT_THRESHOLD_MAIN = 9.5
    ZT_THRESHOLD_REGISTERED = 19.5

    BREAKOUT_WINDOW_DAYS = 10

    RESISTANCE_ZONE_PCT = 10
    RESISTANCE_LIMIT = 15
    PROFIT_RATE_LIMIT = 70

    MAX_PLATE_RANK = 20

    LONG_SHADOW_WINDOW = 3
    LONG_SHADOW_RATIO = 2.0

    NEW_STOCK_DAYS = 60

    MIN_VOLUME_FOR_ANALYSIS = 10000

    TU_SHARE_TOKEN = ""

    LOG_LEVEL = "INFO"

    # ---- 回测配置 ----
    # 回测目标日期，None 表示使用今日（实盘模式）
    # 设置为 'YYYY-MM-DD' 字符串则进入回测模式，所有数据按该日期获取：
    #   - 分时数据过滤到该日期 09:30-10:00
    #   - 日线/周线 end_date 设为该日期
    #   - 板块指数涨幅以该日期为最新交易日计算
    #   - 板块成分股接口仍实时获取（无历史成分股接口）
    TARGET_DATE = None

    # ---- 分时条件阈值 ----
    # 缩量回踩：回踩均量需低于上涨均量的多少比例
    PULLBACK_VOLUME_RATIO = 0.8
    # 补丁1（9:45-10:00）：期间最低价不得低于9:45均价的比例
    PATCH1_MIN_PRICE_RATIO = 0.98
    # 补丁1：期间低于均价线的分钟数占比上限
    PATCH1_MAX_BELOW_AVG_PCT = 0.2

    # ---- 仓位建议阈值（基于板块助攻强度）----
    POSITION_PLATE_LIMIT_UP_HEAVY = 2      # 板块涨停家数≥此值且突破前高 → 重仓
    POSITION_PLATE_ABOVE_5PCT_NORMAL = 5   # 板块涨幅≥5%家数≥此值 → 正常仓位
    POSITION_PLATE_ABOVE_5PCT_LIGHT = 3    # 板块涨幅≥5%家数≥此值 → 轻仓试错

    # ---- 结果输出 ----
    TOP_STOCK_MAX = 5                      # 最终输出个股数量上限

    # ---- 板块成分股并发获取 ----
    PLATE_CONS_MAX_WORKERS = 10            # 线程池并发数（同花顺概念涨幅获取）
    STOCK_FILTER_MAX_WORKERS = 8           # 个股筛选线程池并发数
    # 注：BaoStock 单 socket 连接通过 _bs_call_lock 串行化所有调用，
    # 多线程在此锁处会排队，不影响其他数据源（新浪/同花顺）的并发。

    # ---- 网络请求超时与预检 ----
    # 单次 akshare 请求的 socket 超时（秒），避免东财接口不可用时长时间等待
    REQUEST_SOCKET_TIMEOUT = 15
    # 板块成分股预检：先单独请求第一个板块，若失败则直接进入降级模式，跳过剩余板块请求
    PLATE_CONS_PREFLIGHT = True

    # ---- 请求节流（防 IP 封禁）----
    # 同一数据源内两次请求的最小间隔（秒），不同数据源可并行
    # 按数据源独立配置：东财风控严，新浪/同花顺中等，BaoStock 宽松
    REQUEST_INTERVAL_EASTMONEY = 2.0   # 东财 push2 风控严格
    REQUEST_INTERVAL_SINA = 1.0        # 新浪相对宽松
    REQUEST_INTERVAL_THS = 1.0         # 同花顺相对宽松
    REQUEST_INTERVAL_BAOSTOCK = 0.2    # BaoStock 已通过 _bs_call_lock 串行化，间隔可降到0.2秒
    REQUEST_INTERVAL_DEFAULT = 1.0     # 默认间隔

    # ---- 股票范围限定 ----
    # 仅扫描沪深主板股票（60/00开头），排除创业板(30)/科创板(68)/北交所(43/83/87/88/92/920)
    STOCK_PREFIX_MAIN = ['60', '00']       # 允许的股票代码前缀
    STOCK_PREFIX_EXCLUDE = ['30', '68', '43', '83', '87', '88', '92']  # 明确排除的前缀

    # ---- 概念板块筛选 ----
    # 个股所属概念板块涨幅需≥此阈值（独立筛选条件，与行业板块并列）
    CONCEPT_MIN_CHANGE = 3.0               # 概念板块最低涨幅(%)
    CONCEPT_TOP_PERCENTILE = 0.10          # 概念板块前10%阈值（同行业板块）
    MAX_CONCEPT_RANK = 20                  # 概念板块排名上限
    # 概念板块获取数量限制（同花顺降级模式下从371个概念中只取前N个计算涨幅，减少请求量）
    # 优先取市值较大或常见概念，N=0 表示获取全部
    # 注：概念涨幅仅用于仓位建议调整，30个已足够反映市场热点情绪
    CONCEPT_FETCH_LIMIT = 30               # 概念涨幅获取数量上限（降级模式）


# ---- akshare 列名重命名映射 ----
# akshare 不同接口返回中文字段名，统一重命名为英文以便代码引用

COLUMN_MAP_HIST = {
    '日期': 'date', '股票代码': 'code', '开盘': 'open', '收盘': 'close',
    '最高': 'high', '最低': 'low', '成交量': 'volume', '成交额': 'amount',
    '涨跌幅': 'change_percent', '涨跌额': 'change_amount', '换手率': 'turnover',
}

COLUMN_MAP_SPOT = {
    '代码': 'code', '名称': 'name', '最新价': 'current_price',
    '涨跌幅': 'change_percent', '涨跌额': 'change_amount',
    '流通市值': 'circulating_market_cap', '总市值': 'total_market_cap',
    '成交量': 'volume', '成交额': 'amount', '换手率': 'turnover',
}

COLUMN_MAP_PLATE = {
    '板块名称': 'name', '板块代码': 'code', '最新价': 'current_price',
    '涨跌幅': 'change_percent', '涨跌额': 'change_amount',
    '成交量': 'volume', '成交额': 'amount', '换手率': 'turnover',
    '上涨家数': 'rising_count', '下跌家数': 'falling_count',
}

COLUMN_MAP_MINUTE = {
    'day': 'time', '时间': 'time', '开盘': 'open', '收盘': 'close',
    '最高': 'high', '最低': 'low', '成交量': 'volume', '成交额': 'amount',
}

COLUMN_MAP_INDEX = {
    '代码': 'code', '名称': 'name', '最新价': 'current_price',
    '涨跌幅': 'change_percent', '涨跌额': 'change_amount',
}

# 概念板块数据列名映射（同花顺概念板块接口）
COLUMN_MAP_CONCEPT = {
    '板块名称': 'name', '代码': 'code', '涨跌幅': 'change_percent',
    '总市值': 'total_market_cap', '换手率': 'turnover',
    '上涨家数': 'rising_count', '下跌家数': 'falling_count',
    '领涨股票': 'leading_stock', '领涨股票-涨跌幅': 'leading_stock_change',
}


def rename_columns(df, column_map):
    """重命名 DataFrame 列，只重命名实际存在的列，忽略缺失的"""
    if df is None or df.empty:
        return df
    rename_dict = {k: v for k, v in column_map.items() if k in df.columns}
    return df.rename(columns=rename_dict)


def code_to_symbol(code):
    """纯数字股票代码转为 akshare 新浪接口所需的带前缀 symbol

    akshare 的新浪接口（stock_zh_a_minute / stock_zh_a_daily）要求 symbol 带交易所前缀：
        sh: 上海证券交易所（60/68/11/13/50/51/56 开头）
        sz: 深圳证券交易所（00/30/12/15/16/18 开头）
        bj: 北京证券交易所（43/83/87/88/92 开头）

    东财接口（stock_zh_a_hist / stock_zh_a_spot_em）使用纯数字代码。
    本函数用于在调用新浪接口前做格式转换。

    输入兼容：纯数字（'600519'）、已带前缀（'sh600519'）、带后缀（'600519.SH'）。
    """
    code = str(code).strip()

    # 已带前缀，直接返回
    if code.startswith(('sh', 'sz', 'bj')):
        return code

    # 处理后缀格式（600519.SH / 600519.SZ）
    if '.' in code:
        code = code.split('.')[0]

    # 去除非数字字符
    code = ''.join(c for c in code if c.isdigit())

    if not code:
        return code

    # 补齐6位
    code = code.zfill(6)

    # 根据代码前缀判断交易所
    if code.startswith(('60', '68', '11', '13', '50', '51', '56')):
        return 'sh' + code
    elif code.startswith(('00', '30', '12', '15', '16', '18')):
        return 'sz' + code
    elif code.startswith(('43', '83', '87', '88', '92')):
        return 'bj' + code
    else:
        # 默认归为上海（极少见）
        return 'sh' + code


def symbol_to_code(symbol):
    """带前缀 symbol 转为纯数字代码（code_to_symbol 的逆操作）

    'sh600519' -> '600519', 'sz000001' -> '000001', 'bj920000' -> '920000'
    纯数字输入直接返回。
    """
    s = str(symbol).strip()
    if s.startswith(('sh', 'sz', 'bj')):
        return s[2:]
    # 处理后缀格式
    if '.' in s:
        s = s.split('.')[0]
    return ''.join(c for c in s if c.isdigit())


def get_end_date():
    """获取数据获取的 end_date（YYYYMMDD 格式）

    回测模式（Config.TARGET_DATE 非空）返回 TARGET_DATE 转换的数字串；
    实盘模式返回今日。
    """
    if Config.TARGET_DATE:
        return Config.TARGET_DATE.replace('-', '')
    from datetime import datetime
    return datetime.now().strftime('%Y%m%d')


def get_target_date_str():
    """获取目标日期字符串（YYYY-MM-DD 格式）

    回测模式返回 TARGET_DATE；实盘模式返回今日。
    """
    if Config.TARGET_DATE:
        return Config.TARGET_DATE
    from datetime import datetime
    return datetime.now().strftime('%Y-%m-%d')
