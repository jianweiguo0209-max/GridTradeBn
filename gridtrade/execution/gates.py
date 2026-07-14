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
    def __init__(self, gates: Iterable[AdmissionGate], *, log=None):
        self.gates: List[AdmissionGate] = list(gates)
        self.log = log      # 可选：被拒提案留痕（None=静默）

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
        kept: List[GridProposal] = []
        for p in proposals:
            res = self.evaluate(p)
            if res.passed:
                kept.append(p)
            elif self.log is not None:   # 可观测性：该开未开必须留痕（gate+reason+symbol）
                self.log('[gate] rejected %s tag=%s by %s: %s'
                         % (p.symbol, p.tag, res.gate, res.reason))
        return kept


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


class FuseCoverageGate(AdmissionGate):
    """保险丝覆盖率门（spec 2026-07-15）：保险丝数量 worst=order_num×grid_count 受币安
    MARKET_LOT_SIZE.maxQty 限制——超限被 -4005 拒（ed4616e 起适配器封顶到 maxQty，代价是
    超出部分无原生硬保护，只剩软止损 5s 轮 + 爆仓线）。

    本门在开仓前把 cap 降到"丝能护全额"的水平（保住币、只缩仓）；降到 CAP_MIN 之下才拒
    （安全失败，不建死网格）。**降后"每笔名义额够不够"不在此重复实现**——交给链上紧随其后
    的 MinNotionalGate 用新 cap 自然拒（DRY）。故链序必须是
    FuseCoverage → RiskBudget → MinNotional → Margin：cap 在被任何"吃 cap"的门消费前定稿。

    主网当前恒不触发（票池最小市价名义上限 $30,570 > 满仓名义额；临界权益 ≈$36,684），
    权益长大后自动接管；demo 的 maxQty 比主网小 3-1200 倍故会真实触发。
    min_coverage<=0 = 停用（紧急回退）。begin_batch 刷按币 maxQty 映射；取数失败 fail-open
    （与 MinNotionalGate 同构——绝不因限额表读不到而拒单）。"""

    def __init__(self, executor, min_coverage, *, adapter=None, log=None):
        self.executor = executor
        self.min_coverage = float(min_coverage)
        self.adapter = adapter          # 按币 maxQty 来源（Instrument.market_max_qty）
        self._max_qty = None            # None=未加载；{}=无数据（fail-open 不干预）
        self.log = log

    def begin_batch(self) -> None:
        if self.adapter is None:
            self._max_qty = {}
            return
        try:
            self._max_qty = {i.symbol: float(getattr(i, 'market_max_qty', 0.0) or 0.0)
                             for i in self.adapter.list_instruments()}
        except Exception as exc:        # fail-open：限额表读不到只退化，不拒单
            self._max_qty = {}
            if self.log is not None:
                self.log('[gate] FuseCoverageGate: list_instruments failed %r' % (exc,))

    def check(self, proposal: GridProposal) -> GateResult:
        if self._max_qty is None:       # 未经 begin_batch 的独立 evaluate → 惰性加载一次
            self.begin_batch()
        if self.min_coverage <= 0:      # 停用（紧急回退）
            return GateResult(True, 'FuseCoverageGate')
        from gridtrade.execution.fuse_policy import fuse_capped_cap
        mx = (self._max_qty or {}).get(proposal.symbol, 0.0)
        cap = (proposal.cap if proposal.cap is not None
               else self.executor._resolve_cap())
        cap2, cov = fuse_capped_cap(cap, self.executor.gearing, proposal.grid_params, mx,
                                    min_amount=self.executor.min_amount,
                                    min_coverage=self.min_coverage)
        if cov is None or cap2 >= cap:  # 未知/建不了网/足额 → 放行不干预
            return GateResult(True, 'FuseCoverageGate')
        if cap2 < self.executor.cap_min:
            return GateResult(False, 'FuseCoverageGate',
                              'fuse coverage %.0f%% → cap %.2f->%.2f < CAP_MIN %.2f'
                              % (100.0 * cov, cap, cap2, self.executor.cap_min))
        proposal.cap = cap2             # 定稿 cap：后续门与 executor.open 都 honor
        if self.log is not None:
            self.log('[gate] FuseCoverageGate: %s 丝覆盖 %.0f%% → cap %.2f->%.2f（降档护全额）'
                     % (proposal.symbol, 100.0 * cov, cap, cap2))
        return GateResult(True, 'FuseCoverageGate')


class MinNotionalGate(AdmissionGate):
    """最小名义额门：预检每笔挂单名义额 ≥ 下限。下限 = max(全局 env MIN_ORDER_NOTIONAL,
    该币 Instrument.min_cost)——币安各币 MIN_NOTIONAL 不同（多数 5、BTC 50、ETH 20 USDT，
    2026-07-14 fapi 实测），单一全局值必漏（spec 2026-07-14 §5.3）。

    动机（mainnet 2026-07-05 实证）：单笔 < 交易所下限 → 开仓首单即被拒 → 留零挂单死
    OPENING。门链预检直接拒提案：不建死网格、拒因可观测。

    口径与 executor.open 同源：grid_order_info + executor._resolve_cap()；
    最低档名义额 = 每笔数量 × low_price。adapter=None 且 env<=0 = 停用（向后兼容）。
    begin_batch 刷新按币映射；取数失败 fail-open 退回全局下限。"""

    def __init__(self, executor, min_notional, *, adapter=None, log=None):
        self.executor = executor
        self.min_notional = float(min_notional)
        self.adapter = adapter          # 可选：按币 min_cost 来源（Instrument.min_cost）
        self._min_cost = None           # None=未加载；{}=无数据（fail-open 只用全局下限）
        self.log = log

    def begin_batch(self) -> None:
        if self.adapter is None:
            self._min_cost = {}
            return
        try:
            self._min_cost = {i.symbol: float(getattr(i, 'min_cost', 0.0) or 0.0)
                              for i in self.adapter.list_instruments()}
        except Exception as exc:        # fail-open：精度表读不到只退化，不拒单
            self._min_cost = {}
            if self.log is not None:
                self.log('[gate] MinNotionalGate: list_instruments failed %r' % (exc,))

    def check(self, proposal: GridProposal) -> GateResult:
        if self._min_cost is None:      # 未经 begin_batch 的独立 evaluate → 惰性加载一次
            self.begin_batch()
        floor = max(self.min_notional,
                    (self._min_cost or {}).get(proposal.symbol, 0.0))
        if floor <= 0:
            return GateResult(True, 'MinNotionalGate')
        from gridtrade.core.grid_engine import grid_order_info
        gp = proposal.grid_params
        cap = (proposal.cap if proposal.cap is not None
               else self.executor._resolve_cap())
        gi = grid_order_info(cap, self.executor.gearing, gp['low_price'],
                             gp['high_price'], int(gp['grid_count']),
                             gp['stop_low_price'], gp['stop_high_price'],
                             min_amount=self.executor.min_amount,
                             max_rate=1.0)
        if gi is None:
            return GateResult(False, 'MinNotionalGate',
                              'cap %.2f 无法建网（每笔数量<=0）' % cap)
        worst = float(gi['每笔数量']) * float(gp['low_price'])   # 最低档名义额
        if worst < floor:
            return GateResult(False, 'MinNotionalGate',
                              'per-order notional %.2f < min %.2f '
                              '(cap=%.2f grids=%d)' % (worst, floor,
                                                       cap, int(gp['grid_count'])))
        return GateResult(True, 'MinNotionalGate')


class MarginGate(AdmissionGate):
    """可用保证金门：实时查交易所可用余额(cash) >= 本提议所需(cap)，同轮累计扣减。

    口径（用户敲定）：所需=proposal.cap；未给时优先 executor._resolve_cap()（与真实开仓同源的
    动态 cap，随权益自动跟随——default_cap 是静态值会与动态 cap 脱节、预留虚低），executor
    未传回退 default_cap（向后兼容）。放行条件 cash - 已预留 >= 所需；
    fail-closed（余额读不到则全拒）。须置于门链末尾（短路链中过它即准入，预留不虚高）。
    """

    def __init__(self, adapter, default_cap, *, executor=None, log=None):
        self.adapter = adapter
        self.default_cap = float(default_cap)
        self.executor = executor    # 可选：动态 cap 来源（_resolve_cap 内部已有失败回退）
        self._available = None      # 本批可用余额快照；None=未快照
        self._reserved = 0.0        # 本批已放行提议的累计所需
        self._balance_ok = True
        self.log = log              # 可选：fail-closed 时把被吞的余额异常打出来

    def begin_batch(self) -> None:
        self._reserved = 0.0
        try:
            self._available = float(self.adapter.fetch_balance().cash)
            self._balance_ok = True
        except Exception as exc:    # fail-closed：余额读不到 -> 本批全拒（绝不吞 BaseException）
            self._available = 0.0
            self._balance_ok = False
            if self.log is not None:    # 不再静默：留真因，否则整批被拒却零线索
                self.log('[gate] MarginGate fail-closed: balance fetch failed: %r'
                         % (exc,))

    def check(self, proposal: GridProposal) -> GateResult:
        if self._available is None:     # 未经 begin_batch 的独立 evaluate -> 惰性快照一次
            self.begin_batch()
        if not self._balance_ok:
            return GateResult(False, 'MarginGate', 'balance unavailable')
        if proposal.cap is not None:
            required = proposal.cap
        elif self.executor is not None:
            required = float(self.executor._resolve_cap())   # 与真实开仓同源的动态 cap
        else:
            required = self.default_cap
        if self._available - self._reserved < required:
            return GateResult(False, 'MarginGate',
                              'free cash %.4f - reserved %.4f < required %.4f'
                              % (self._available, self._reserved, required))
        self._reserved += required
        return GateResult(True, 'MarginGate')
