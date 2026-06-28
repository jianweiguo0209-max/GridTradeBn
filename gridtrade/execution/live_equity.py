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

    def record_fill(self, price, side, size, ts_ms):
        if side not in ('buy', 'sell'):
            raise ValueError("side must be 'buy' or 'sell'")
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

    def replay(self, fills) -> 'LiveEquity':
        """fills: 可迭代的 (price, side, size, ts_ms)。供 reconciler 从持久化成交重建。"""
        for price, side, size, ts_ms in fills:
            self.record_fill(price, side, size, ts_ms)
        return self

    def snapshot(self, mark_price) -> dict:
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
        net_value = float(last['net_value']) - self.funding_paid / self.cap
        return {'net_value': net_value, 'pnl_ratio': net_value - 1.0,
                'net_position': float(last['hold_num']), 'avg_price': float(last['avg_price']),
                'realized_pnl': float(last['real_profit']), 'fee_paid': float(last['fee']),
                'funding_paid': self.funding_paid}
