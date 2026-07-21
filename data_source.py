"""数据源适配器：统一封装 AKShare + BaoStock 双数据源

核心功能：
1. requests 全局 UA 修复：patch requests.Session 默认 headers，绕过东财反爬。
   原因：akshare 默认 requests UA 被东财反爬识别后会主动断连
   ('Connection aborted.', RemoteDisconnected)。
2. 按数据源分组节流：东财/新浪/同花顺/BaoStock 各自独立 3 秒间隔，
   不同数据源可并行请求，大幅提升并发性能。
3. BaoStock 单例登录管理。
4. fetch_daily_via_baostock() / fetch_weekly_via_baostock()：日线/周线最终降级源。

设计说明：
- 节流器使用 4 把独立锁 + 4 个时间戳，不同源可并行（akshare 不同接口实际
  打不同域名，IP 封禁也是按域名计的）。
- 同一数据源内仍严格串行，确保不被封。
- BaoStock 仅用于日线/周线降级，分钟数据 BaoStock 无1分钟粒度，仍用新浪。
"""
import time
import threading
import logging
import pandas as pd
import requests
from config import Config

logger = logging.getLogger(__name__)


# ============================================================================
# requests 全局 UA 修复（绕过东财反爬）
# ============================================================================

# 仿真 Chrome 浏览器请求头，包含 Referer 防止被识别
_BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) '
                  'Chrome/120.0.0.0 Safari/537.36',
    'Accept': '*/*',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Referer': 'https://data.eastmoney.com/',
    'Connection': 'keep-alive',
}

# akshare 内部直接用 requests.get(url, params=...) 调用，不传 headers，
# 因此必须 patch requests.get / requests.post 本身注入浏览器 UA。
# 通过 monkey-patch 包装原函数，自动添加 headers（不覆盖调用方显式传入的）
_original_requests_get = requests.get
_original_requests_post = requests.post


def _patched_get(url, params=None, **kwargs):
    """注入浏览器 UA 的 requests.get 包装"""
    headers = dict(_BROWSER_HEADERS)
    # 合并调用方显式传入的 headers（覆盖默认值）
    if 'headers' in kwargs and kwargs['headers']:
        headers.update(kwargs['headers'])
    kwargs['headers'] = headers
    return _original_requests_get(url, params=params, **kwargs)


def _patched_post(url, data=None, **kwargs):
    """注入浏览器 UA 的 requests.post 包装"""
    headers = dict(_BROWSER_HEADERS)
    if 'headers' in kwargs and kwargs['headers']:
        headers.update(kwargs['headers'])
    kwargs['headers'] = headers
    return _original_requests_post(url, data=data, **kwargs)


requests.get = _patched_get
requests.post = _patched_post
logger.info("已 patch requests.get/post 全局 UA，绕过东财反爬")


# ============================================================================
# 东财 push2 API 可用性检测（启动时一次性）
# ============================================================================

# 全局标志：东财 push2 API 是否可用
# 原因：东财 push2.eastmoney.com / push2his.eastmoney.com 端点会因 IP 级风控
# 主动断连（'Connection aborted.', RemoteDisconnected）。
# data.eastmoney.com 网页仍可访问，但 push2 API 不行。
# 启动时检测一次，不可用则全局禁用东财接口，避免后续每只股票都等待超时。
_eastmoney_push2_available = None  # None=未检测, True/False=已检测结果


def check_eastmoney_available():
    """检测东财 push2 API 是否可用（启动时调用一次）

    通过请求一个轻量级接口判断，超时或断连则视为不可用。
    结果缓存到全局变量，后续 is_eastmoney_available() 直接返回。
    """
    global _eastmoney_push2_available
    if _eastmoney_push2_available is not None:
        return _eastmoney_push2_available

    try:
        # 用一个最轻量的接口测试（行业板块列表，pn=1&pz=1 只取1条）
        throttle('eastmoney')
        r = _original_requests_get(
            'https://17.push2.eastmoney.com/api/qt/clist/get',
            params={'pn': '1', 'pz': '1', 'fltt': '2', 'invt': '2',
                    'fid': 'f3', 'fs': 'm:90+t:2', 'fields': 'f12'},
            headers=_BROWSER_HEADERS,
            timeout=8
        )
        if r.status_code == 200 and r.json().get('data'):
            _eastmoney_push2_available = True
            logger.info("东财 push2 API 可用")
            return True
        else:
            _eastmoney_push2_available = False
            logger.warning(f"东财 push2 API 返回异常: status={r.status_code}")
            return False
    except Exception as e:
        _eastmoney_push2_available = False
        logger.warning(f"东财 push2 API 不可用（{type(e).__name__}），将全局禁用东财接口")
        return False


def is_eastmoney_available():
    """查询东财 push2 API 是否可用（未检测则触发检测）"""
    if _eastmoney_push2_available is None:
        return check_eastmoney_available()
    return _eastmoney_push2_available


def reset_eastmoney_check():
    """重置东财可用性检测结果（用于测试）"""
    global _eastmoney_push2_available
    _eastmoney_push2_available = None


def is_baostock_enabled():
    """检查 BaoStock 是否可用（已登录或网络可用）
    
    返回 False 的情况：
    1. BaoStock 模块未安装
    2. 之前登录失败（网络问题）
    返回 True 的情况：
    1. 已成功登录
    2. 模块已安装但尚未检测网络状态（首次调用）
    """
    global _bs_available
    # 如果已经检测过不可用，直接返回 False
    if _bs_available == False:
        return False
    # 如果已登录，返回 True
    if _bs_logged_in:
        return True
    # 模块未安装的情况在 ensure_baostock_login 中处理
    # 这里如果 _bs_available 是 None（未检测），返回 True 允许尝试登录
    return True


# ============================================================================
# 按数据源分组节流器
# ============================================================================

# 4 个数据源各自独立的锁和上次请求时间
# 同源内严格串行（各自间隔），不同源可并行
_source_state = {
    'eastmoney': {'lock': threading.Lock(), 'last_time': 0.0, 'interval': lambda: Config.REQUEST_INTERVAL_EASTMONEY},
    'sina':      {'lock': threading.Lock(), 'last_time': 0.0, 'interval': lambda: Config.REQUEST_INTERVAL_SINA},
    'ths':       {'lock': threading.Lock(), 'last_time': 0.0, 'interval': lambda: Config.REQUEST_INTERVAL_THS},
    'baostock':  {'lock': threading.Lock(), 'last_time': 0.0, 'interval': lambda: Config.REQUEST_INTERVAL_BAOSTOCK},
    'default':   {'lock': threading.Lock(), 'last_time': 0.0, 'interval': lambda: Config.REQUEST_INTERVAL_DEFAULT},
}


def _detect_source(url_or_func_name: str) -> str:
    """根据 URL 或函数名识别数据源"""
    s = str(url_or_func_name).lower()
    if 'eastmoney' in s or 'em' in s and ('stock_zh_a' in s or 'stock_board' in s):
        return 'eastmoney'
    if 'sina' in s or 'sinajs' in s:
        return 'sina'
    if 'ths' in s or '10jqka' in s or '同花顺' in s:
        return 'ths'
    if 'baostock' in s or 'bs.' in s:
        return 'baostock'
    return 'default'


def throttle(source='default'):
    """请求节流：确保同一数据源两次请求间隔满足该数据源的配置间隔

    参数:
        source: 数据源标识，可选 'eastmoney'/'sina'/'ths'/'baostock'/'default'
                不同数据源之间可并行（不互相阻塞），同源内严格串行。
                各数据源间隔独立配置（Config.REQUEST_INTERVAL_*）。

    示例：
        throttle('baostock')  # BaoStock 间隔 0.3 秒
        df = fetch_daily_via_baostock(...)
    """
    state = _source_state.get(source, _source_state['default'])
    interval = state['interval']()
    with state['lock']:
        now = time.time()
        elapsed = now - state['last_time']
        wait = interval - elapsed
        if wait > 0:
            time.sleep(wait)
        state['last_time'] = time.time()


# ============================================================================
# BaoStock 单例登录管理
# ============================================================================

_bs_login_lock = threading.Lock()
_bs_logged_in = False
_bs_available = None  # None=未检测, True/False=检测结果
# BaoStock 全局调用锁：单 socket 连接不支持并发，必须串行所有调用
# 否则会出现 utf-8 decode error / decompress error（数据流错乱）
_bs_call_lock = threading.Lock()


def ensure_baostock_login():
    """确保 BaoStock 已登录（单例模式）"""
    global _bs_logged_in, _bs_available
    with _bs_login_lock:
        if _bs_logged_in:
            return True
        # 如果之前检测过不可用，直接返回 False
        if _bs_available == False:
            return False
        try:
            import baostock as bs
            throttle('baostock')
            lg = bs.login()
            if lg.error_code == '0':
                _bs_logged_in = True
                _bs_available = True
                logger.info("BaoStock 登录成功")
                return True
            else:
                logger.error(f"BaoStock 登录失败: {lg.error_msg}")
                _bs_available = False
                return False
        except ImportError:
            _bs_available = False
            logger.warning("BaoStock 模块未安装，将跳过 BaoStock 数据源")
            return False
        except Exception as e:
            _bs_available = False
            logger.error(f"BaoStock 登录异常: {e}")
            return False


def baostock_logout():
    """BaoStock 登出（程序退出时调用）"""
    global _bs_logged_in
    if _bs_logged_in:
        try:
            import baostock as bs
            bs.logout()
            _bs_logged_in = False
            logger.info("BaoStock 已登出")
        except Exception:
            pass


def code_to_baostock_symbol(code):
    """6位纯数字代码转 BaoStock symbol 格式

    BaoStock 使用 'sh.600519' / 'sz.000001' / 'bj.430047' 格式。
    """
    code = str(code).strip().zfill(6)
    if code.startswith(('6', '9')):
        return f'sh.{code}'
    elif code.startswith(('0', '2', '3')):
        return f'sz.{code}'
    elif code.startswith(('4', '8')):
        return f'bj.{code}'
    return f'sh.{code}'


def fetch_daily_via_baostock(code, start_date, end_date):
    """通过 BaoStock 获取日线数据（作为 AKShare 降级源）

    参数:
        code: 6位纯数字代码（如 '600519'）
        start_date: YYYY-MM-DD 格式
        end_date: YYYY-MM-DD 格式

    返回:
        DataFrame，列与 AKShare 兼容：
            date, open, high, low, close, volume, amount, change_percent, turnover
        失败返回 None。

    线程安全：BaoStock 单 socket 连接不支持并发，通过 _bs_call_lock 串行化所有调用。
    """
    # 先检查全局标志，避免重复登录失败
    global _bs_available
    if _bs_available == False:
        return None
    if not ensure_baostock_login():
        return None

    # 全局锁串行化 BaoStock 调用（避免数据流错乱）
    with _bs_call_lock:
        try:
            import baostock as bs
            symbol = code_to_baostock_symbol(code)
            throttle('baostock')
            rs = bs.query_history_k_data_plus(
                symbol,
                "date,open,high,low,close,volume,amount,turn,pctChg",
                start_date=start_date,
                end_date=end_date,
                frequency="d",
                adjustflag="2"
            )

            if rs.error_code != '0':
                logger.warning(f"BaoStock 查询 {code} 失败: {rs.error_msg}")
                return None

            data_list = []
            while rs.next():
                data_list.append(rs.get_row_data())

            if not data_list:
                return None

            df = pd.DataFrame(data_list, columns=rs.fields)
            for col in ['open', 'high', 'low', 'close', 'volume', 'amount', 'turn', 'pctChg']:
                df[col] = pd.to_numeric(df[col], errors='coerce')
            df = df.rename(columns={'pctChg': 'change_percent', 'turn': 'turnover'})
            df = df.dropna(subset=['close'])
            df = df.sort_values('date').reset_index(drop=True)
            return df
        except Exception as e:
            logger.error(f"BaoStock 获取 {code} 日线数据异常: {e}")
            return None


def fetch_weekly_via_baostock(code, start_date, end_date):
    """通过 BaoStock 获取周线数据（作为 AKShare 降级源）

    线程安全：通过 _bs_call_lock 串行化所有 BaoStock 调用。
    """
    # 先检查全局标志，避免重复登录失败
    global _bs_available
    if _bs_available == False:
        return None
    if not ensure_baostock_login():
        return None

    with _bs_call_lock:
        try:
            import baostock as bs
            symbol = code_to_baostock_symbol(code)
            throttle('baostock')
            rs = bs.query_history_k_data_plus(
                symbol,
                "date,open,high,low,close,volume,amount,turn,pctChg",
                start_date=start_date,
                end_date=end_date,
                frequency="w",
                adjustflag="2"
            )

            if rs.error_code != '0':
                logger.warning(f"BaoStock 周线查询 {code} 失败: {rs.error_msg}")
                return None

            data_list = []
            while rs.next():
                data_list.append(rs.get_row_data())

            if not data_list:
                return None

            df = pd.DataFrame(data_list, columns=rs.fields)
            for col in ['open', 'high', 'low', 'close', 'volume', 'amount', 'turn', 'pctChg']:
                df[col] = pd.to_numeric(df[col], errors='coerce')
            df = df.rename(columns={'pctChg': 'change_percent', 'turn': 'turnover'})
            df = df.dropna(subset=['close'])
            df = df.sort_values('date').reset_index(drop=True)
            return df
        except Exception as e:
            logger.error(f"BaoStock 获取 {code} 周线数据异常: {e}")
            return None
