"""MarginGate 交易所 IM 口径（spec 2026-07-18-margin-gate-exchange-im）。

新口径：required = k×(整梯名义/L + worst浮亏 + fee)（margin_policy），available ≥ required；
适配器缺能力 / tiers 空 / 取数抛错 / executor 缺失 → fail-closed 回退旧 cap 口径并留痕。
旧口径回归见 test_gates.py（其 _BalAdapter 无 tiers/price 能力 → 天然走回退路径）。

MET 主网回归（2026-07-18 首开实证）：equity=cash=$510.95、cap=$751.39(frac1.47 N=2)、
旧口径 cash<cap 恒拒 → 新口径 required≈$293 → 放行。
"""
import pytest

from gridtrade.exchanges.base import Balance
from gridtrade.execution.gates import GridProposal, MarginGate

# —— 干净数几何（同 test_margin_policy 手算基准）——
GP = {'low_price': 100.0, 'high_price': 400.0, 'grid_count': 2,
      'stop_low_price': 50.0, 'stop_high_price': 500.0}
TIERS = [{'maxLeverage': 10, 'maxNotional': 1000.0},
         {'maxLeverage': 5, 'maxNotional': 5000.0}]
REQ_CLEAN = 1.25 * (70.0 + 400.0 + 700.0 * 0.0005)   # k=1.25, 默认 fee_rate=0.0005 → 588.19

# —— MET 主网实数（2026-07-18 容器实测）——
GP_MET = {'low_price': 0.109791, 'high_price': 0.147409, 'grid_count': 10,
          'stop_low_price': 0.108693, 'stop_high_price': 0.148883}
TIERS_MET = [{'maxLeverage': 50, 'maxNotional': 5000.0},
             {'maxLeverage': 20, 'maxNotional': 10000.0},
             {'maxLeverage': 10, 'maxNotional': 20000.0},
             {'maxLeverage': 5, 'maxNotional': 50000.0},
             {'maxLeverage': 4, 'maxNotional': 250000.0},
             {'maxLeverage': 3, 'maxNotional': 500000.0},
             {'maxLeverage': 2, 'maxNotional': 7500000.0},
             {'maxLeverage': 1, 'maxNotional': 12500000.0}]
CAP_MET, CASH_MET, ENTRY_MET = 751.3941176470588, 510.948, 0.1285


def _p(gp=None, **kw):
    base = dict(exchange='binance', symbol='MET/USDT:USDT', grid_params=gp or GP)
    base.update(kw)
    return GridProposal(**base)


class _Exec:
    """gate 所需最小执行器桩：gearing / min_amount / _resolve_cap。"""
    def __init__(self, cap, gearing, min_amount=0.0):
        self.gearing = gearing
        self.min_amount = min_amount
        self._cap = cap

    def _resolve_cap(self):
        return self._cap


class _IMAdapter:
    """余额 + 杠杆档位 + 现价桩；可注抛错模拟取数失败。"""
    def __init__(self, cash, tiers, price, tiers_raises=False, price_raises=False):
        self._cash = cash
        self._tiers = tiers
        self._price = price
        self._tr = tiers_raises
        self._pr = price_raises

    def fetch_balance(self):
        return Balance(equity=self._cash, cash=self._cash)

    def fetch_leverage_tiers(self, symbol):
        if self._tr:
            raise RuntimeError('tiers down')
        return self._tiers

    def fetch_price(self, symbol):
        if self._pr:
            raise RuntimeError('price down')
        return self._price


def test_im_basis_rejects_where_cap_basis_would_pass():
    # cash=100 ≥ cap=70（旧口径过）但 < required≈588（新口径拒）→ 证明新口径生效
    gate = MarginGate(_IMAdapter(100.0, TIERS, 150.0), default_cap=70.0,
                      executor=_Exec(70.0, 10.0))
    r = gate.check(_p())
    assert r.passed is False and r.gate == 'MarginGate'
    assert 'IM' in r.reason


def test_im_basis_passes_where_cap_basis_would_reject_met_mainnet_regression():
    # MET 2026-07-18 实证：cash 510.95 < cap 751.39（旧口径拒死）→ 新口径 required≈$293 放行
    gate = MarginGate(_IMAdapter(CASH_MET, TIERS_MET, ENTRY_MET), default_cap=CAP_MET,
                      executor=_Exec(CAP_MET, 3.4))
    assert gate.check(_p(GP_MET)).passed is True


def test_im_required_value_matches_policy():
    gate = MarginGate(_IMAdapter(100.0, TIERS, 150.0), default_cap=70.0,
                      executor=_Exec(70.0, 10.0))
    r = gate.check(_p())
    assert ('%.2f' % REQ_CLEAN) in r.reason      # 拒因里报出新口径 required


def test_cumulative_reservation_uses_im_required():
    # cash=700：第一笔过（预留 588.19），第二笔 700−588.19 < 588.19 → 拒
    gate = MarginGate(_IMAdapter(700.0, TIERS, 150.0), default_cap=70.0,
                      executor=_Exec(70.0, 10.0))
    gate.begin_batch()
    assert gate.check(_p()).passed is True
    assert gate.check(_p()).passed is False


def test_fallback_to_cap_basis_when_tiers_raise():
    logs = []
    gate = MarginGate(_IMAdapter(510.0, TIERS, 150.0, tiers_raises=True),
                      default_cap=751.0, executor=_Exec(751.0, 3.4), log=logs.append)
    r = gate.check(_p(GP_MET))
    assert r.passed is False                     # 回退 cap 口径：510 < 751 → 拒（fail-closed）
    assert any('fallback' in m for m in logs), logs


def test_fallback_to_cap_basis_when_price_raises():
    logs = []
    gate = MarginGate(_IMAdapter(510.0, TIERS, 150.0, price_raises=True),
                      default_cap=751.0, executor=_Exec(751.0, 3.4), log=logs.append)
    assert gate.check(_p(GP_MET)).passed is False
    assert any('fallback' in m for m in logs), logs


def test_fallback_when_empty_tiers():
    logs = []
    gate = MarginGate(_IMAdapter(510.0, [], 150.0), default_cap=751.0,
                      executor=_Exec(751.0, 3.4), log=logs.append)
    assert gate.check(_p(GP_MET)).passed is False
    assert any('fallback' in m for m in logs), logs


def test_fallback_when_no_executor():
    # executor 缺失 → 无 gearing/min_amount，无法算 IM → 回退 cap 口径
    logs = []
    gate = MarginGate(_IMAdapter(510.0, TIERS, 150.0), default_cap=751.0,
                      log=logs.append)
    assert gate.check(_p(GP_MET)).passed is False
    assert any('fallback' in m for m in logs), logs


def test_pass_leaves_breakdown_log():
    # 放行也留痕（每小时最多 1-2 条,观测新口径生效与数值）
    logs = []
    gate = MarginGate(_IMAdapter(CASH_MET, TIERS_MET, ENTRY_MET), default_cap=CAP_MET,
                      executor=_Exec(CAP_MET, 3.4), log=logs.append)
    assert gate.check(_p(GP_MET)).passed is True
    assert any('IM' in m for m in logs), logs
