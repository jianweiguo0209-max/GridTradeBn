"""LiveEquity：实盘网格增量记账,盈亏逐笔精确直算(_replay_exact/pnl_exact)。
2026-07-12 起引擎 cal_equity_curve 移出记账链路(spec honest-record-pnl):引擎是回测
均匀 lot/线价语义的模拟器,对部分成交/合成行/乱序输入失真(neutral hold_num、ADA avg=0、
VVV manual 三次同族事故);回测继续用引擎,记账一律直算。不直接判止损(由监控层组合)。
"""
from typing import Optional

import pandas as pd


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

    def _replay_exact(self):
        """单遍逐笔精确回放 → (avg, net, realized)。语义:同向加权、减仓 realize 且成本不变、
        过零先 realize 旧仓再以翻向价开新。realized 为毛额(费/资金费在 pnl_exact 层扣)。
        这是记账真相源(spec 2026-07-12-honest-record-pnl):引擎 cal_equity_curve 是回测
        均匀 lot/线价语义的模拟器,对部分成交/合成行/乱序输入失真(neutral hold_num、
        ADA avg=0、VVV manual 记录 +15/真实 −52 三次同族实证),不得用于记账。"""
        pos = 0.0
        avg = 0.0
        realized = 0.0
        for f in self._fills:
            signed = f['order_dir'] * f['order_num']
            px = f['touch']
            new = pos + signed
            if pos == 0.0 or pos * signed > 0:          # 开新/同向加仓 → 加权
                avg = px if pos == 0.0 else (avg * abs(pos) + px * abs(signed)) / abs(new)
            elif pos * new < 0:                          # 穿越翻向 → 平旧全额 realize,成本=本笔价
                realized += (px - avg) * pos if pos > 0 else (avg - px) * (-pos)
                avg = px
            elif new == 0.0:                             # 恰好平净 → realize 全部
                realized += (px - avg) * pos if pos > 0 else (avg - px) * (-pos)
                avg = 0.0
            else:                                        # 部分减仓 → realize 减仓量,成本不变
                q = abs(signed)
                realized += (px - avg) * q if pos > 0 else (avg - px) * q
            pos = new
        return avg, pos, realized

    def _avg_cost(self):
        """当前净仓的精确加权平均成本(薄壳,见 _replay_exact)。"""
        return self._replay_exact()[0]

    def pnl_exact(self, mark_price):
        """诚实盈亏(记账唯一口径):realized + (mark−avg)×net − 真实费 − 资金费。
        返回 {'realized','unreal','pnl','pnl_ratio','avg','net'}。"""
        avg, net, realized = self._replay_exact()
        unreal = (float(mark_price) - avg) * net if net != 0.0 else 0.0
        pnl = realized + unreal - self.real_fee_paid - self.funding_paid
        return {'realized': realized, 'unreal': unreal, 'pnl': pnl,
                'pnl_ratio': pnl / self.cap, 'avg': avg, 'net': net}

    def replay(self, fills) -> 'LiveEquity':
        """fills: 可迭代的 (price, side, size, ts_ms) 或 (price, side, size, ts_ms, fee)。
        供 reconciler 从持久化成交重建。"""
        for price, side, size, ts_ms, *rest in fills:
            fee = rest[0] if rest else None
            self.record_fill(price, side, size, ts_ms, fee)
        return self

    def snapshot(self, mark_price) -> dict:
        """Mark-to-market snapshot,全字段逐笔精确直算(spec 2026-07-12-honest-record-pnl):
        record/accounting/止损判定共用本口径。引擎 cal_equity_curve 已移出记账链路
        (回测均匀 lot/线价语义,对部分成交/合成行失真——三次同族事故实证,见 _replay_exact);
        不含平仓 taker 费(实际退出时由 executor 落真实费)。"""
        if not self._fills:
            return {'net_value': 1.0, 'pnl_ratio': 0.0, 'net_position': 0.0,
                    'avg_price': 0.0, 'realized_pnl': 0.0, 'fee_paid': 0.0,
                    'funding_paid': self.funding_paid}
        r = self.pnl_exact(mark_price)
        return {'net_value': 1.0 + r['pnl_ratio'], 'pnl_ratio': r['pnl_ratio'],
                'net_position': r['net'], 'avg_price': r['avg'],
                'realized_pnl': r['realized'], 'fee_paid': self.real_fee_paid,
                'funding_paid': self.funding_paid}
