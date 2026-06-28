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


class CircuitBreaker:
    """连续失败达阈值则 open；冷却到点 half-open 放行一次试探；成功 close、失败重 open。"""

    def __init__(self, failure_threshold=5, cooldown=30.0, clock=time.monotonic):
        self.failure_threshold = int(failure_threshold)
        self.cooldown = float(cooldown)
        self.clock = clock
        self._failures = 0
        self._opened_at = None
        self._half_open = False

    def allow(self) -> bool:
        if self._opened_at is None:
            return True
        if self.clock() - self._opened_at >= self.cooldown:
            self._half_open = True
            return True
        return False

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._half_open = False

    def record_failure(self) -> None:
        if self._half_open:
            self._opened_at = self.clock()   # 试探失败 -> 重新 open
            self._half_open = False
            return
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._opened_at = self.clock()
