"""触发引擎 —— 「触发 → 准入 → 执行」三段式的触发层（只提议，不下单）。

TriggerCondition 是可插拔策略：吃 TriggerContext，吐 GridProposal 列表。
TriggerEngine 汇集所有已注册触发器的提议，交给准入门链（gates.GateChain）过闸。
ScheduledSelectionTrigger 复刻 legacy 主流程的选币提议切片（主流程原样保留）。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterable, List, Optional

import numpy as np
import pandas as pd

from gridtrade.core.grid_params import GRID_ROW_FACTORS, calc_grid_params_v1, calc_grid_params_v2
from gridtrade.core.selection import (compute_offset, proceed_calc_symbol_factor,
                                      select_grid_coin)
from gridtrade.core.tick_fit import filter_tick_fit
from gridtrade.execution.gates import GridProposal


@dataclass
class TriggerContext:
    exchange: str
    run_time: pd.Timestamp
    symbol_candle_data: Optional[dict] = None
    # record-and-replay(2026-07-17 实盘对账):ScheduledSelectionTrigger 把排名结果写回,
    # scheduler 落 selection_snapshots → 离线复放精确对齐选币名次。触发器不落库(无 store),
    # 经 ctx 交给 scheduler(有 store)统一 fail-soft 写。
    selection_offset: Optional[int] = None
    selection_ranked: Optional[list] = None   # [{symbol, factors, rank_sum, rank}] 名次升序


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


def _default_select_fn(strategy_config, factors, weight_list, *,
                       tick_map_fn=None, min_ticks=0.0, log=print):
    period = strategy_config['period']
    choose_symbols = strategy_config['choose_symbols']

    def _fn(symbol_candle_data, run_time, offset):
        all_df = proceed_calc_symbol_factor(symbol_candle_data, run_time,
                                            period, offset)
        if all_df is None or all_df.empty:
            return all_df
        if tick_map_fn is not None and min_ticks > 0:
            # 排名前过滤 ⇒ 名次递补免费获得(与回测 pick_first_allowed 语义等价)
            all_df, _ = filter_tick_fit(all_df, tick_map_fn(), strategy_config,
                                        min_ticks, log=log)
            if all_df.empty:
                return all_df
        return select_grid_coin(all_df, factors, weight_list, choose_symbols,
                                run_time)

    return _fn


def build_eff1_select_fn(strategy_config, label_feed, *,
                         tick_map_fn=None, min_ticks=0.0, log=print):
    """eff1 选币(与回测 eff1_scan.make_picks 同口径):
    票池 = 布网列有限(**不走** rank_sum filter v1.0/因子 dropna,用户令 2026-07-25);
    p12_eff 降序、symbol 升序 tiebreak;缺标签不参选(inner merge 同款)。"""
    period = strategy_config['period']
    choose_symbols = strategy_config['choose_symbols']

    def _fn(symbol_candle_data, run_time, offset):
        all_df = proceed_calc_symbol_factor(symbol_candle_data, run_time, period,
                                            offset, needed=set(GRID_ROW_FACTORS),
                                            batch=True)
        if all_df is None or all_df.empty:
            return all_df
        all_df = all_df[np.isfinite(all_df['close']) & np.isfinite(all_df['Atr_5'])
                        & np.isfinite(all_df['middle_5'])]
        if tick_map_fn is not None and min_ticks > 0:
            all_df, _ = filter_tick_fit(all_df, tick_map_fn(), strategy_config,
                                        min_ticks, log=log)
        lab = label_feed.labels(run_time)
        if all_df.empty or not lab:
            log('[eff1] 本轮无候选(候选=%d 标签=%d)' % (len(all_df), len(lab)))
            return all_df.iloc[0:0]
        ldf = pd.DataFrame([dict(symbol=s, **v) for s, v in lab.items()])
        d = all_df.merge(ldf, on='symbol', how='inner')
        d = d.sort_values(['time', 'p12_eff', 'symbol'],
                          ascending=[True, False, True])
        d['rank'] = d.groupby('time', sort=False).cumcount() + 1.0
        return d[d['rank'] <= choose_symbols]

    return _fn


class ScheduledSelectionTrigger(TriggerCondition):
    """offset + 因子选币 → 网格提议（legacy 主流程原样保留）。

    产出 raw-float grid_params（来自已金标的 core.grid_params），tick 精度由适配器
    下单层负责，本触发器不格式化、不套用 legacy 的 round 碰撞护栏。
    """

    def __init__(self, strategy_config, factors, weight_list, *,
                 select_fn=None, source='ScheduledSelectionTrigger'):
        self.strategy_config = strategy_config
        self.factors = factors
        self.weight_list = weight_list
        self.source = source
        self.select_fn = select_fn or _default_select_fn(
            strategy_config, factors, weight_list)

    def propose(self, ctx: TriggerContext) -> List[GridProposal]:
        cfg = self.strategy_config
        period = cfg['period']
        offset = compute_offset(ctx.run_time, period)
        factor_data = self.select_fn(ctx.symbol_candle_data, ctx.run_time, offset)
        if factor_data is None or factor_data.empty:
            return []
        # point-in-time 新鲜度过滤（同 selection_replay）
        factor_data = factor_data[
            (factor_data['time'] + pd.to_timedelta(period)) >= ctx.run_time]
        if factor_data.empty:
            return []
        factor_data = factor_data.sort_values('rank')

        # record-and-replay(2026-07-17):把排名 picks(选中币+因子值+名次)写回 ctx,供
        # scheduler 落 selection_snapshots。fail-soft:序列化失败绝不阻断开格。
        try:
            # s 仪表(2026-07-21 用户定):S_shape_5(刺币值)只记录不排名——实盘 pnl×s
            # 分桶取证通道(A族战役唯一幸存资产,cal_factor 已算,此处仅落库)。
            _fcols = [c for c in self.factors if c in factor_data.columns]
            _fcols += [c for c in ('S_shape_5',)
                       if c in factor_data.columns and c not in _fcols]
            ctx.selection_offset = int(offset)
            ctx.selection_ranked = [
                {'symbol': r['symbol'],
                 'factors': {c: float(r[c]) for c in _fcols},
                 'rank_sum': float(r['rank_sum']) if 'rank_sum' in factor_data.columns else 0.0,
                 'rank': int(r['rank']) if 'rank' in factor_data.columns else 0}
                for _, r in factor_data.iterrows()]
        except Exception:
            ctx.selection_ranked = None

        grid_version = cfg.get('grid_version', 1)
        calc_fn = calc_grid_params_v2 if grid_version == 2 else calc_grid_params_v1
        price_limit = cfg['price_limit']
        stop_limit = cfg['stop_limit']
        v2_config = cfg.get('grid_v2_config', {})
        tag = '%s%d' % (cfg['strategy_tag'], offset)

        proposals: List[GridProposal] = []
        for _, row in factor_data.iterrows():
            params = calc_fn(row=row, price_limit=price_limit,
                             stop_limit=stop_limit, v2_config=v2_config)
            proposals.append(GridProposal(
                exchange=ctx.exchange, symbol=row['symbol'], grid_params=params,
                offset=offset, tag=tag, source=self.source))
        return proposals


# ── 三期预留扩展点 ─────────────────────────────────────────────────────────
# ThresholdTrigger（价格/指标阈值触发）与 ExternalSignalTrigger（外部信号/手动触发）
# 留到三期实现（需产品定义：阈值类别、外部信号格式）。扩展方式已就位、无需改本引擎：
#   1) 子类化 TriggerCondition，实现 propose(ctx) -> List[GridProposal]；
#   2) 在 TriggerEngine([...]) 注册（factory 处）即并入「触发→准入→执行」流水线；
#   3) 如需更多输入（现价/指标/外部 payload），给 TriggerContext 追加可选字段即可。
# 设计依据见 design.md §6① 触发引擎（Strategy + 可插拔 TriggerCondition）。
