"""LiveEquity：实盘网格增量记账，通过复用 core.grid_engine.cal_equity_curve 与回测同源。
把累计成交流水重建为 trade_df + 一根当前 mark 价的合成 1m K线喂引擎，取末行 net_value；
资金费单独累计（funding_paid），从 net_value 扣除。不直接判止损（由监控层组合）。
"""
from typing import Optional

import pandas as pd

from gridtrade.core.grid_engine import cal_equity_curve


class LiveEquity:
    def __init__(self, cap, fee=0.0002, c_rate_taker=0.0005,
                 entry_price: Optional[float] = None):
        self.cap = float(cap)
        self.fee = float(fee)
        self.c_rate_taker = float(c_rate_taker)
        self.entry_price = None if entry_price is None else float(entry_price)
        self._fills = []      # trade_df 行：candle_begin_time/last_touch/touch/order_dir/order_num
        self._last_ts = None  # 最后成交时间（ms）
        self.funding_paid = 0.0
        self.real_fee_paid = 0.0

    def record_fill(self, price, side, size, ts_ms, fee=None):
        if side not in ('buy', 'sell'):
            raise ValueError("side must be 'buy' or 'sell'")
        # fee=None：无真实费时回退估算费率（与共用引擎口径一致），保持向后兼容
        real_fee = float(size) * float(price) * self.fee if fee is None else float(fee)
        self.real_fee_paid += real_fee
        order_dir = 1.0 if side == 'buy' else -1.0
        if self._fills:
            last_touch = self._fills[-1]['touch']
        elif self.entry_price is not None:
            last_touch = self.entry_price
        else:
            last_touch = float(price)
        self._fills.append({
            'candle_begin_time': pd.to_datetime(int(ts_ms), unit='ms'),
            'last_touch': float(last_touch), 'touch': float(price),
            'order_dir': order_dir, 'order_num': float(size),
        })
        self._last_ts = int(ts_ms)

    def add_funding(self, amount):
        self.funding_paid += float(amount)

    @property
    def net_position(self):
        """当前净仓 = Σ(order_dir×order_num),与引擎 hold_num(累计带符号量)同源。
        PositionLedger 的 claim 真相源——O(n) 直算,不走整条引擎重放。"""
        return float(sum(f['order_dir'] * f['order_num'] for f in self._fills))

    @property
    def last_fill_ts(self):
        """最后成交 ts(ms);无成交 None。账本↔DB 对齐判定顺序/乱序用。"""
        return self._last_ts

    def _avg_cost(self):
        """当前净仓的精确加权平均成本（逐笔回放：同向加权、减仓成本不变、过零重置为翻向价）。
        引擎 avg 是均匀 lot 阶梯近似（回测语义）；实盘非均匀 size 用真实成交直算——
        mainnet ADA 2026-07-08 实证近似路径产出 avg=0 → 幻影浮盈 +13.5%。"""
        pos = 0.0
        avg = 0.0
        for f in self._fills:
            signed = f['order_dir'] * f['order_num']
            px = f['touch']
            new = pos + signed
            if pos == 0.0 or pos * signed > 0:          # 开新/同向加仓 → 加权
                avg = px if pos == 0.0 else (avg * abs(pos) + px * abs(signed)) / abs(new)
            elif pos * new < 0:                          # 穿越翻向 → 成本=本笔价
                avg = px
            elif new == 0.0:                             # 恰好平净
                avg = 0.0
            # 部分减仓：成本不变
            pos = new
        return avg

    def replay(self, fills) -> 'LiveEquity':
        """fills: 可迭代的 (price, side, size, ts_ms) 或 (price, side, size, ts_ms, fee)。
        供 reconciler 从持久化成交重建。"""
        for price, side, size, ts_ms, *rest in fills:
            fee = rest[0] if rest else None
            self.record_fill(price, side, size, ts_ms, fee)
        return self

    def snapshot(self, mark_price) -> dict:
        """Mark-to-market snapshot: net_value/fee_paid via cal_equity_curve WITHOUT _apply_exit,
        so excludes close-out taker fee (applied by executor on actual exit).
        fee_paid 取真实累计费（real_fee_paid），net_value 按 (est_fee - real_fee)/cap 修正。"""
        if not self._fills:
            return {'net_value': 1.0, 'pnl_ratio': 0.0, 'net_position': 0.0,
                    'avg_price': 0.0, 'realized_pnl': 0.0, 'fee_paid': 0.0,
                    'funding_paid': self.funding_paid}
        trade_df = pd.DataFrame(self._fills)
        mark_ts = pd.to_datetime(self._last_ts + 60_000, unit='ms')  # 严格晚于所有成交
        mp = float(mark_price)
        candle_df = pd.DataFrame([{
            'candle_begin_time': mark_ts, 'open': mp, 'high': mp, 'low': mp,
            'close': mp, 'symbol': '_LIVE_',
        }])
        eq = cal_equity_curve(candle_df, trade_df.copy(), self.fee, self.cap,
                              self.c_rate_taker, funding_df=None)
        last = eq.iloc[-1]
        est_fee = float(last['fee'])                 # 引擎按费率估算的累计费
        # net_value 内已扣 est_fee；用真实费替换：+est_fee/cap -real_fee/cap -funding/cap
        net_value = (float(last['net_value'])
                     + (est_fee - self.real_fee_paid) / self.cap
                     - self.funding_paid / self.cap)
        return {'net_value': net_value, 'pnl_ratio': net_value - 1.0,
                'net_position': float(last['hold_num']), 'avg_price': self._avg_cost(),
                'realized_pnl': float(last['real_profit']), 'fee_paid': self.real_fee_paid,
                'funding_paid': self.funding_paid}
