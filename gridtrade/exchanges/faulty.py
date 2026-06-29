"""FaultyAdapter：故障注入包装器（P6① 混沌测试）。透明包装任意 ExchangeAdapter，
按「方法名→故障列表」脚本消费故障，验证执行/对账/止损在异常下的端到端不变量。
鸭子类型（不继承 ABC），与 ResilientAdapter 同层（允许 import ccxt）。
"""
from dataclasses import dataclass


@dataclass
class Partial:
    """仅 create_market_order：内层下单量×ratio，模拟部分成交（HL 滑点/reduce 没吃满）。"""
    ratio: float


@dataclass
class RaiseAfter:
    """先调用内层（产生副作用）再抛 exc：模拟请求已达交易所但 ack 丢失的丢响应超时。"""
    exc: Exception


class FaultyAdapter:
    def __init__(self, inner, schedule=None):
        self._inner = inner
        self._schedule = {k: list(v) for k, v in (schedule or {}).items()}

    def _next_fault(self, name):
        q = self._schedule.get(name)
        if not q:
            return None
        return q.pop(0)

    def create_market_order(self, symbol, side, size, *, reduce_only=False, client_oid=None):
        fault = self._next_fault('create_market_order')
        if isinstance(fault, RaiseAfter):
            self._inner.create_market_order(symbol, side, size,
                                            reduce_only=reduce_only, client_oid=client_oid)
            raise fault.exc
        if isinstance(fault, Partial):
            size = size * fault.ratio
        elif isinstance(fault, Exception):
            raise fault
        return self._inner.create_market_order(symbol, side, size,
                                               reduce_only=reduce_only, client_oid=client_oid)

    def __getattr__(self, name):
        # 仅当属性未在本类正常解析时触发（_inner/_schedule/_next_fault/create_market_order 走正常解析）
        inner_attr = getattr(self._inner, name)
        if not callable(inner_attr):
            return inner_attr
        def wrapped(*args, **kwargs):
            fault = self._next_fault(name)
            if isinstance(fault, RaiseAfter):
                inner_attr(*args, **kwargs)
                raise fault.exc
            if isinstance(fault, Exception):
                raise fault
            return inner_attr(*args, **kwargs)
        return wrapped
