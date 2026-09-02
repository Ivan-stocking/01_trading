"""数据源适配器：统一封装 AKShare 数据源

核心功能：
1. requests 全局 UA 修复：patch requests.Session 默认 headers，
   伪装浏览器请求，降低被反爬识别的概率。
2. 按数据源分组节流：新浪/同花顺各自独立间隔，
   不同数据源可并行请求，大幅提升并发性能。

设计说明：
- 节流器使用独立锁 + 时间戳，不同源可并行。
- 同一数据源内仍严格串行，确保不被封。
- 日线/周线/分钟数据均通过 AKShare 获取（新浪/同花顺），
  东财接口不稳定，已全部移除。
"""
import time
import threading
import logging
import requests
from config import Config

logger = logging.getLogger(__name__)


# ============================================================================
# requests 全局 UA 修复
# ============================================================================

# 仿真 Chrome 浏览器请求头
_BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) '
                  'Chrome/120.0.0.0 Safari/537.36',
    'Accept': '*/*',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
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
logger.info("已 patch requests.get/post 全局 UA")


# ============================================================================
# 按数据源分组节流器
# ============================================================================

# 各数据源各自独立的锁和上次请求时间
# 同源内严格串行（各自间隔），不同源可并行
_source_state = {
    'sina':      {'lock': threading.Lock(), 'last_time': 0.0, 'interval': lambda: Config.REQUEST_INTERVAL_SINA},
    'ths':       {'lock': threading.Lock(), 'last_time': 0.0, 'interval': lambda: Config.REQUEST_INTERVAL_THS},
    'default':   {'lock': threading.Lock(), 'last_time': 0.0, 'interval': lambda: Config.REQUEST_INTERVAL_DEFAULT},
}


def _detect_source(url_or_func_name: str) -> str:
    """根据 URL 或函数名识别数据源"""
    s = str(url_or_func_name).lower()
    if 'sina' in s or 'sinajs' in s:
        return 'sina'
    if 'ths' in s or '10jqka' in s or '同花顺' in s:
        return 'ths'
    return 'default'


def throttle(source='default'):
    """请求节流：确保同一数据源两次请求间隔满足该数据源的配置间隔

    参数:
        source: 数据源标识，可选 'sina'/'ths'/'default'
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
