"""准入门链（Chain of Responsibility）—— 开网格提议的无冲突第一道闸。

触发器产出 GridProposal -> GateChain 顺序过闸 -> 放行的提议交 GridManager 开仓。
门只读状态、不下单、不写库。MarginGate/RiskBudgetGate 待保证金/风险口径决策后补。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterable, List, Optional


@dataclass
class GridProposal:
    exchange: str
    symbol: str
    grid_params: dict
    offset: int = 0
    tag: str = ''
    cap: Optional[float] = None
    source: str = ''


@dataclass
class GateResult:
    passed: bool
    gate: str
    reason: str = ''


class AdmissionGate(ABC):
    def begin_batch(self) -> None:
        """每轮 filter 开始前调用一次；有状态门（如 MarginGate）借此重置批次状态。默认空。"""
        return None

    @abstractmethod
    def check(self, proposal: GridProposal) -> GateResult:
        ...


class GateChain:
    def __init__(self, gates: Iterable[AdmissionGate]):
        self.gates: List[AdmissionGate] = list(gates)

    def evaluate(self, proposal: GridProposal) -> GateResult:
        for gate in self.gates:
            result = gate.check(proposal)
            if not result.passed:
                return result
        return GateResult(True, 'GateChain')

    def filter(self, proposals: Iterable[GridProposal]) -> List[GridProposal]:
        proposals = list(proposals)
        for gate in self.gates:
            gate.begin_batch()
        return [p for p in proposals if self.evaluate(p).passed]


class SymbolLockGate(AdmissionGate):
    """一期 SymbolExclusivePolicy：同 (exchange, symbol) 已有活跃网格则拒绝。"""

    def __init__(self, grids):
        self.grids = grids

    def check(self, proposal: GridProposal) -> GateResult:
        existing = self.grids.get_active_by_symbol(proposal.exchange,
                                                   proposal.symbol)
        if existing is not None:
            return GateResult(False, 'SymbolLockGate',
                              'active grid already exists for %s on %s'
                              % (proposal.symbol, proposal.exchange))
        return GateResult(True, 'SymbolLockGate')


class MaxConcurrentGate(AdmissionGate):
    """活跃网格数达到 max_concurrent 时拒绝新提议。"""

    def __init__(self, grids, max_concurrent: int):
        self.grids = grids
        self.max_concurrent = int(max_concurrent)

    def check(self, proposal: GridProposal) -> GateResult:
        active = len(self.grids.list_active())
        if active >= self.max_concurrent:
            return GateResult(False, 'MaxConcurrentGate',
                              'active grids %d >= limit %d'
                              % (active, self.max_concurrent))
        return GateResult(True, 'MaxConcurrentGate')


class RiskBudgetGate(AdmissionGate):
    """总风险敞口以 cap 衡量：∑(活跃网格 cap) + 本提议 cap ≤ total_budget。

    提议未显式给 cap 时按 default_cap（执行器默认建仓资金）计入，使敞口估算
    反映真实资金占用。口径决策：用户 2026-06-28 选「∑cap ≤ 总预算」。
    """

    def __init__(self, grids, total_budget, default_cap):
        self.grids = grids
        self.total_budget = float(total_budget)
        self.default_cap = float(default_cap)

    def check(self, proposal: GridProposal) -> GateResult:
        used = sum((g.cap or 0.0) for g in self.grids.list_active())
        incoming = (proposal.cap if proposal.cap is not None
                    else self.default_cap)
        if used + incoming > self.total_budget:
            return GateResult(False, 'RiskBudgetGate',
                              'cap sum %.4f + %.4f > budget %.4f'
                              % (used, incoming, self.total_budget))
        return GateResult(True, 'RiskBudgetGate')


class MarginGate(AdmissionGate):
    """可用保证金门：实时查交易所可用余额(cash) >= 本提议所需(cap)，同轮累计扣减。

    口径（用户敲定）：所需=proposal.cap（未给用 default_cap）；放行条件 cash - 已预留 >= 所需；
    fail-closed（余额读不到则全拒）。须置于门链末尾（短路链中过它即准入，预留不虚高）。
    """

    def __init__(self, adapter, default_cap):
        self.adapter = adapter
        self.default_cap = float(default_cap)
        self._available = None      # 本批可用余额快照；None=未快照
        self._reserved = 0.0        # 本批已放行提议的累计所需
        self._balance_ok = True

    def begin_batch(self) -> None:
        self._reserved = 0.0
        try:
            self._available = float(self.adapter.fetch_balance().cash)
            self._balance_ok = True
        except Exception:           # fail-closed：余额读不到 -> 本批全拒（绝不吞 BaseException）
            self._available = 0.0
            self._balance_ok = False

    def check(self, proposal: GridProposal) -> GateResult:
        if self._available is None:     # 未经 begin_batch 的独立 evaluate -> 惰性快照一次
            self.begin_batch()
        if not self._balance_ok:
            return GateResult(False, 'MarginGate', 'balance unavailable')
        required = (proposal.cap if proposal.cap is not None else self.default_cap)
        if self._available - self._reserved < required:
            return GateResult(False, 'MarginGate',
                              'free cash %.4f - reserved %.4f < required %.4f'
                              % (self._available, self._reserved, required))
        self._reserved += required
        return GateResult(True, 'MarginGate')
