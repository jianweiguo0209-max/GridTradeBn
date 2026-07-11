# tests/execution/test_close_share_split.py
"""组件一(spec 2026-07-11-symbol-desk):close_share 残余比例分摊 + EPS 统一。
cap=4 后多幸存格成常态:残余按反号 claim |比例| 分摊(正是对冲掉本格份额的各方);
无反号→全体按|claim|;全零→均分;守恒逐位(最后一名吃余数);EPS 防浮点尘埃转仓。
N=2 退化(单一反号幸存格得 100%)由现有 test_close_share.py 零改动锁定。"""
import pytest


@pytest.fixture(autouse=True)
def _wide_slots(monkeypatch):
    """本文件测 cap=N 通用逻辑,槽位策略与线上默认解耦(tier2_cap 现值变化不波及)。"""
    import gridtrade.config as cfg
    from gridtrade.core.tier_policy import TierPolicy
    monkeypatch.setattr(cfg, 'DEFAULT_TIER_POLICY', TierPolicy(tier2_cap=8))


from gridtrade.exchanges.base import Instrument
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.execution.grid_executor import GridExecutor

BTC = 'BTC/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}


def _setup(store, n):
    ex = FakeExchange(instruments=[Instrument(BTC, 0.1, 0.001, 0.001, 'live', 0)],
                      price=100.0)
    ex.set_price(BTC, 100.0)
    gx = GridExecutor(ex, store, cap=1000.0, leverage=5.0)
    gids = [gx.open('fake', BTC, dict(GP), tag='t%d' % i) for i in range(n)]
    return ex, gx, gids


def _force(ex, gx, want):     # {gid: claim};交易所净仓对齐 Σ
    for gid, c in want.items():
        cur = gx.live[gid].net_position
        d = c - cur
        if abs(d) > 1e-12:
            gx.live[gid].record_fill(100.0, 'buy' if d > 0 else 'sell', abs(d), 1000)
    ex._pos[BTC] = type(ex.fetch_positions(BTC))(BTC, float(sum(want.values())), 100.0)


def _ledger_rows(gx, gid):
    return [f for f in gx.fills.list_by_grid(gid) if f.trade_id.startswith('ledger:')]


def test_three_survivors_two_opposite_proportional(store):
    # 关 A(+12);幸存 B=-9, C=-3, D=+5;交易所净 +5 → reduce 5,残余 7 按 9:3 给 B/C,D 零
    ex, gx, (a, b, c, d) = _setup(store, 4)
    _force(ex, gx, {a: 12.0, b: -9.0, c: -3.0, d: 5.0})
    gx.close(a, BTC, '周期再平衡')
    assert abs(ex.fetch_positions(BTC).net_size - 0.0) < 1e-9      # 5 被 reduce 平掉
    assert abs(gx.live[b].net_position - (-9.0 + 7 * 9 / 12)) < 1e-9   # -3.75
    assert abs(gx.live[c].net_position - (-3.0 + 7 * 3 / 12)) < 1e-9   # -1.25
    assert abs(gx.live[d].net_position - 5.0) < 1e-12               # 同号幸存格不收
    assert abs(gx.live[a].net_position) < 1e-9
    # Σclaims == 交易所净仓(不变量恢复)
    total = sum(gx.live[g].net_position for g in (b, c, d))
    assert abs(total - ex.fetch_positions(BTC).net_size) < 1e-9


def test_all_same_sign_survivors_split_by_claim(store):
    # 幸存全同号(漂移态):按 |claim| 比例分摊,总账守恒、不让单格背全部
    ex, gx, (a, b, c) = _setup(store, 3)
    _force(ex, gx, {a: 6.0, b: 2.0, c: 4.0})
    ex._pos[BTC] = type(ex.fetch_positions(BTC))(BTC, 2.0, 100.0)   # 人工漂移:净仓仅 +2
    gx.close(a, BTC, '周期再平衡')
    # reduce 2 后残余 4 → B 得 4×2/6, C 得 4×4/6
    assert abs(gx.live[b].net_position - (2.0 + 4 * 2 / 6)) < 1e-9
    assert abs(gx.live[c].net_position - (4.0 + 4 * 4 / 6)) < 1e-9


def test_all_zero_survivors_equal_split(store):
    ex, gx, (a, b, c) = _setup(store, 3)
    _force(ex, gx, {a: 5.0, b: 0.0, c: 0.0})
    ex._pos[BTC] = type(ex.fetch_positions(BTC))(BTC, 0.0, 100.0)   # 净 0:无可 reduce
    gx.close(a, BTC, '周期再平衡')
    assert abs(gx.live[b].net_position - 2.5) < 1e-9
    assert abs(gx.live[c].net_position - 2.5) < 1e-9


def test_dust_claim_no_transfer(store):
    # 浮点尘埃(prod min_amount=0,ETHFI gt08 实证形态):多笔真实成交的带符号和
    # 残差 ~1e-16,被 EPS 挡住 → 零合成行(2026-07-11 巡查已批的一行修)
    ex, gx, (a, b) = _setup(store, 2)
    for _ in range(3):
        gx.live[a].record_fill(100.0, 'buy', 0.1, 1000)
    gx.live[a].record_fill(100.0, 'sell', 0.3, 2000)
    dust = gx.live[a].net_position
    assert 0 < abs(dust) < 1e-9                        # 真实浮点残差
    ex._pos[BTC] = type(ex.fetch_positions(BTC))(BTC, 0.0, 100.0)
    gx.close(a, BTC, '周期再平衡')
    assert not _ledger_rows(gx, a) and not _ledger_rows(gx, b)


def test_split_conservation_exact(store):
    # 守恒逐位:Σ 分摊量 == 残余(最后一名吃余数)
    ex, gx, (a, b, c, d) = _setup(store, 4)
    _force(ex, gx, {a: 7.0, b: -9.0, c: -3.0, d: -5.0})
    ex._pos[BTC] = type(ex.fetch_positions(BTC))(BTC, -10.0, 100.0)  # 反号净仓:无可 reduce
    gx.close(a, BTC, '周期再平衡')
    got = sum(f.size for g in (b, c, d) for f in _ledger_rows(gx, g))
    assert got == 7.0                                    # 逐位守恒
