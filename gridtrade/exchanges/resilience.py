"""交易所调用健壮性：错误分类 + 退避重试 + 熔断（需求 1）。

绝不 sys.exit、绝不吞 BaseException。守护进程用 call_with_retry 包装所有 adapter
调用，耗尽/致命时由上层降级告警 + 续跑（不硬退出）。本模块在 exchanges 层，可 import
ccxt；execution/runtime 层只拿被包装的结果，保持 ccxt-free。
"""
import random as _random
import threading
import time
from dataclasses import dataclass

import ccxt


@dataclass
class RetryPolicy:
    max_attempts: int = 4
    base_delay: float = 0.5
    rate_limit_base_delay: float = 2.0
    max_delay: float = 8.0


class CircuitOpenError(Exception):
    pass


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
    """连续失败达阈值则 open；冷却到点 half-open 放行一次试探；成功 close、失败重 open。

    线程安全：monitor per-grid 并行后多线程共用一路电路。半开只授一个探针线程
    （其余线程 allow()=False → CircuitOpenError）；探针线程自身可重入——429 中性
    重试期间 call_with_retry 会对同一逻辑调用再次 allow()，不能把自己挡死。
    """

    def __init__(self, failure_threshold=5, cooldown=30.0, clock=time.monotonic):
        self.failure_threshold = int(failure_threshold)
        self.cooldown = float(cooldown)
        self.clock = clock
        self._failures = 0
        self._opened_at = None
        self._half_open = False
        self._probe_tid = None      # 半开探针的持有线程 id
        self._lock = threading.Lock()

    def allow(self) -> bool:
        with self._lock:
            if self._opened_at is None:
                return True
            if self._probe_tid is not None:
                return self._probe_tid == threading.get_ident()
            if self.clock() - self._opened_at >= self.cooldown:
                self._half_open = True
                self._probe_tid = threading.get_ident()
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None
            self._half_open = False
            self._probe_tid = None

    def record_failure(self) -> None:
        with self._lock:
            if self._half_open:
                self._opened_at = self.clock()   # 试探失败 -> 重新 open
                self._half_open = False
                self._probe_tid = None
                return
            self._failures += 1
            if self._failures >= self.failure_threshold:
                self._opened_at = self.clock()


def call_with_retry(fn, policy, *, classify=classify_error, sleep=time.sleep,
                    rng=None, breaker=None):
    """调用 fn()，按 policy 退避重试。绝不 sys.exit、绝不吞 BaseException。

    熔断 open -> 抛 CircuitOpenError（不调用 fn）；致命 -> 立即抛；可重试/限频 ->
    退避重试，耗尽抛最后异常。成功/失败都喂给 breaker。
    """
    if rng is None:
        rng = _random.Random()
    attempt = 0
    while True:
        if breaker is not None and not breaker.allow():
            raise CircuitOpenError('circuit open')
        try:
            result = fn()
        except Exception as exc:        # 只捕 Exception；BaseException(如 KeyboardInterrupt)自然上抛
            kind = classify(exc)
            if kind == 'fatal':
                # 致命=交易所有响应、只是该请求永久失败（BadSymbol/InvalidOrder/Auth…）：
                # 证明交易所可达，不计入熔断（否则批量遍历坏币会拉垮全局电路）。
                if breaker is not None:
                    breaker.record_success()
                raise
            attempt += 1
            exhausted = attempt >= policy.max_attempts
            # 429/DDoS 单次尝试**中性**：交易所健康、只是让你慢点，由退避重试吸收，
            # 不该秒开全局电路（曾致 MarginGate 查余额被熔断 fail-closed 连锁）。
            # 但退避耗尽仍失败=持续不可用 → 计 1 次熔断（连续多币耗尽会开电路 →
            # 门链 fail-closed 挡开仓，避免在残缺市场数据上选币）。真网络故障照旧每次计。
            if breaker is not None and (kind != 'rate_limit' or exhausted):
                breaker.record_failure()
            if exhausted:
                raise
            base = (policy.rate_limit_base_delay if kind == 'rate_limit'
                    else policy.base_delay)
            raw = min(policy.max_delay, base * (2 ** (attempt - 1)))
            sleep(raw * (0.5 + rng.random() * 0.5))
            continue
        else:
            if breaker is not None:
                breaker.record_success()
            return result
