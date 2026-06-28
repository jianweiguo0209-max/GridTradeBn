"""触发引擎 —— 「触发 → 准入 → 执行」三段式的触发层（只提议，不下单）。

TriggerCondition 是可插拔策略：吃 TriggerContext，吐 GridProposal 列表。
TriggerEngine 汇集所有已注册触发器的提议，交给准入门链（gates.GateChain）过闸。
ScheduledSelectionTrigger 复刻 legacy 主流程的选币提议切片（主流程原样保留）。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterable, List, Optional

import pandas as pd

from gridtrade.core.grid_params import calc_grid_params_v1, calc_grid_params_v2
from gridtrade.core.selection import (compute_offset, proceed_calc_symbol_factor,
                                      select_grid_coin)
from gridtrade.execution.gates import GridProposal


@dataclass
class TriggerContext:
    exchange: str
    run_time: pd.Timestamp
    symbol_candle_data: Optional[dict] = None


class TriggerCondition(ABC):
    @abstractmethod
    def propose(self, ctx: TriggerContext) -> List[GridProposal]:
        ...


class TriggerEngine:
    def __init__(self, triggers: Iterable[TriggerCondition]):
        self.triggers: List[TriggerCondition] = list(triggers)

    def collect(self, ctx: TriggerContext) -> List[GridProposal]:
        proposals: List[GridProposal] = []
        for trigger in self.triggers:
            proposals.extend(trigger.propose(ctx))
        return proposals
