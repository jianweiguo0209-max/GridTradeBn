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
