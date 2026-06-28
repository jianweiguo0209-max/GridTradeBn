"""交易所调用健壮性：错误分类 + 退避重试 + 熔断（需求 1）。

绝不 sys.exit、绝不吞 BaseException。守护进程用 call_with_retry 包装所有 adapter
调用，耗尽/致命时由上层降级告警 + 续跑（不硬退出）。本模块在 exchanges 层，可 import
ccxt；execution/runtime 层只拿被包装的结果，保持 ccxt-free。
"""
import time
from dataclasses import dataclass

import ccxt


def classify_error(exc: Exception) -> str:
    # RateLimitExceeded/DDoSProtection 也是 NetworkError 子类，须先判限频。
    if isinstance(exc, (ccxt.RateLimitExceeded, ccxt.DDoSProtection)):
        return 'rate_limit'
    if isinstance(exc, ccxt.NetworkError):
        return 'retryable'
    if isinstance(exc, ccxt.ExchangeError):
        return 'fatal'
    return 'fatal'
