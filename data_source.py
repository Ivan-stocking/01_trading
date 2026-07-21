"""数据源适配器：统一封装 AKShare 数据源

核心功能：
1. requests 全局 UA 修复：patch requests.Session 默认 headers，绕过东财反爬。
   原因：akshare 默认 requests UA 被东财反爬识别后会主动断连
   ('Connection aborted.', RemoteDisconnected)。
2. 按数据源分组节流：东财/新浪/同花顺各自独立间隔，
   不同数据源可并行请求，大幅提升并发性能。

设计说明：
- 节流器使用 3 把独立锁 + 3 个时间戳，不同源可并行（akshare 不同接口实际
  打不同域名，IP 封禁也是按域名计的）。
- 同一数据源内仍严格串行，确保不被封。
- 日线/周线/分钟数据均通过 AKShare 获取，东财不可用时降级新浪/同花顺。
"""
import time
import threading
import logging
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


# ============================================================================
# 按数据源分组节流器
# ============================================================================

# 3 个数据源各自独立的锁和上次请求时间
# 同源内严格串行（各自间隔），不同源可并行
_source_state = {
    'eastmoney': {'lock': threading.Lock(), 'last_time': 0.0, 'interval': lambda: Config.REQUEST_INTERVAL_EASTMONEY},
    'sina':      {'lock': threading.Lock(), 'last_time': 0.0, 'interval': lambda: Config.REQUEST_INTERVAL_SINA},
    'ths':       {'lock': threading.Lock(), 'last_time': 0.0, 'interval': lambda: Config.REQUEST_INTERVAL_THS},
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
    return 'default'


def throttle(source='default'):
    """请求节流：确保同一数据源两次请求间隔满足该数据源的配置间隔

    参数:
        source: 数据源标识，可选 'eastmoney'/'sina'/'ths'/'default'
                不同数据源之间可并行（不互相阻塞），同源内严格串行。
                各数据源间隔独立配置（Config.REQUEST_INTERVAL_*）。
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
