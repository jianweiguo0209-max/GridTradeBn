import numpy as np
import pandas as pd

from gridtrade.core.grid_engine import cal_equity_curve
from gridtrade.execution.live_equity import LiveEquity


CAP = 1000.0
FEE = 0.0002
CRATE = 0.0005


def _le(entry=100.0):
    from gridtrade.execution.live_equity import LiveEquity
    return LiveEquity(CAP, fee=FEE, c_rate_taker=CRATE, entry_price=entry)


# 一组确定性成交（分钟, 价, 方向）；价为网格线
FILLS = [(1, 99.0, 'buy'), (2, 98.0, 'buy'), (3, 99.0, 'sell'), (4, 100.0, 'sell')]


def _truth_net_value(fills, entry, final_price):
    """真值：把同一组成交 + 完整逐 bar 路径喂 cal_equity_curve，取末行 net_value。"""
    rows = []
    last = entry
    for ts, p, side in fills:
        rows.append({'candle_begin_time': pd.to_datetime(ts * 60_000, unit='ms'),
                     'last_touch': float(last), 'touch': float(p),
                     'order_dir': 1.0 if side == 'buy' else -1.0, 'order_num': 0.5})
        last = p
    trade_df = pd.DataFrame(rows)
    # 注意：本真值用固定 order_num=0.5，测试里 record_fill 也用 size=0.5
    n = fills[-1][0] + 2
    tbars = pd.date_range(pd.to_datetime(0, unit='ms'), periods=n, freq='1min')
    closes = []
    fmap = {ts: p for ts, p, _ in fills}
    cur = entry
    for i in range(n):
        cur = fmap.get(i, cur)
        closes.append(cur)
    closes[-1] = final_price
    candle = pd.DataFrame({'candle_begin_time': tbars, 'open': closes, 'high': closes,
                           'low': closes, 'close': closes, 'symbol': 'X'})
    eq = cal_equity_curve(candle, trade_df.copy(), FEE, CAP, CRATE, funding_df=None)
    return float(eq['net_value'].iloc[-1])


def test_empty_snapshot_is_unit():
    snap = _le().snapshot(100.0)
    assert snap['net_value'] == 1.0 and snap['pnl_ratio'] == 0.0
    assert snap['net_position'] == 0.0 and snap['realized_pnl'] == 0.0


def test_snapshot_matches_full_path_engine():
    le = _le(entry=100.0)
    for ts, p, side in FILLS:
        le.record_fill(p, side, 0.5, ts * 60_000)
    final_price = 100.5
    snap = le.snapshot(final_price)
    truth = _truth_net_value(FILLS, 100.0, final_price)
    assert abs(snap['net_value'] - truth) < 1e-9, f"{snap['net_value']} vs {truth}"
    # 全平后净持仓应为 0，已实现 = 两个格子收益 = 2 × gap(1.0) × 0.5 = 1.0
    assert abs(snap['net_position']) < 1e-9
    assert abs(snap['realized_pnl'] - 1.0) < 1e-9


def test_open_position_marks_to_mark_price():
    le = _le(entry=100.0)
    le.record_fill(99.0, 'buy', 0.5, 60_000)   # 持多 0.5 @ 99
    snap = le.snapshot(101.0)                   # mark 101
    assert abs(snap['net_position'] - 0.5) < 1e-9
    assert abs(snap['avg_price'] - 99.0) < 1e-9
    truth = _truth_net_value([(1, 99.0, 'buy')], 100.0, 101.0)
    assert abs(snap['net_value'] - truth) < 1e-9


def test_bad_side_raises():
    import pytest
    with pytest.raises(ValueError):
        _le().record_fill(100.0, 'long', 0.5, 60_000)


def test_add_funding_reduces_net_value():
    le = _le(entry=100.0)
    le.record_fill(99.0, 'buy', 0.5, 60_000)
    before = le.snapshot(101.0)['net_value']
    le.add_funding(5.0)                       # 支付 5 USDT 资金费
    after = le.snapshot(101.0)
    assert abs((before - after['net_value']) - 5.0 / CAP) < 1e-12
    assert after['funding_paid'] == 5.0


def test_replay_matches_incremental():
    fills = [(99.0, 'buy', 0.5, 60_000), (98.0, 'buy', 0.5, 120_000),
             (99.0, 'sell', 0.5, 180_000)]
    inc = _le(entry=100.0)
    for price, side, size, ts in fills:
        inc.record_fill(price, side, size, ts)
    rep = _le(entry=100.0).replay(fills)
    a, b = inc.snapshot(100.0), rep.snapshot(100.0)
    assert abs(a['net_value'] - b['net_value']) < 1e-12
    assert abs(a['net_position'] - b['net_position']) < 1e-12
    assert abs(a['realized_pnl'] - b['realized_pnl']) < 1e-12


def test_snapshot_flip_long_to_short_matches_full_path():
    # net_dir crosses zero (long -> flat -> short); the net_dir-keyed avg-price path
    # is the highest-risk reconstruction case. Must still match the full-path engine.
    le = _le(entry=100.0)
    le.record_fill(100.0, 'buy', 0.5, 60_000)    # net +0.5
    le.record_fill(101.0, 'sell', 0.5, 120_000)  # net 0
    le.record_fill(102.0, 'sell', 0.5, 180_000)  # net -0.5 (flipped short)
    snap = le.snapshot(103.0)
    assert abs(snap['net_position'] - (-0.5)) < 1e-9
    fills = [(1, 100.0, 'buy'), (2, 101.0, 'sell'), (3, 102.0, 'sell')]
    truth = _truth_net_value(fills, 100.0, 103.0)
    assert abs(snap['net_value'] - truth) < 1e-9


def test_snapshot_net_position_with_variable_fill_sizes():
    # 实盘部分成交 → 逐笔 size 非均匀。hold_num 必须是累计带符号量
    # Σ(order_dir×order_num)，而非 net_dir(净手数) × 最后一笔 size。
    # 后者是回测「均匀 lot」假设，实盘出现一笔非均匀成交即失效
    # （testnet TIA/gt011 实证：buy 1.6 + buy 36 + sell 36 被算成 hold=36 而非 1.6）。
    le = _le(entry=100.0)
    le.record_fill(100.0, 'buy', 2.0, 60_000)     # +2.0
    le.record_fill(99.0, 'buy', 36.0, 120_000)    # +36.0 → 累计 38.0
    le.record_fill(100.0, 'sell', 36.0, 180_000)  # -36.0 → 残留 = 首笔 buy 2.0
    snap = le.snapshot(100.0)
    assert abs(snap['net_position'] - 2.0) < 1e-9, snap['net_position']


def test_snapshot_fee_paid_is_real_sum():
    le = _le(entry=100.0)
    le.record_fill(99.0, 'buy', 0.5, 60_000, fee=0.7)
    le.record_fill(99.0, 'sell', 0.5, 120_000, fee=0.9)
    snap = le.snapshot(100.0)
    assert abs(snap['fee_paid'] - 1.6) < 1e-12      # 0.7 + 0.9


def test_net_value_corrected_to_real_fee():
    fills_geom = [(99.0, 'buy', 0.5, 60_000), (98.0, 'buy', 0.5, 120_000)]
    est = _le(entry=100.0)
    for p, s, sz, ts in fills_geom:
        est.record_fill(p, s, sz, ts)               # fee=None → 估算费率
    est_snap = est.snapshot(100.0)

    real = _le(entry=100.0)
    for p, s, sz, ts in fills_geom:
        real.record_fill(p, s, sz, ts, fee=3.0)     # 每笔真实费 3.0，共 6.0
    real_snap = real.snapshot(100.0)

    assert real_snap['fee_paid'] == 6.0
    # net_value 用真实费替换估算费：real = est + (est_fee - real_fee)/cap
    expected = est_snap['net_value'] + (est_snap['fee_paid'] - 6.0) / CAP
    assert abs(real_snap['net_value'] - expected) < 1e-12
    assert abs(real_snap['pnl_ratio'] - (real_snap['net_value'] - 1.0)) < 1e-12


def test_replay_accepts_fee_tuples():
    fills = [(99.0, 'buy', 0.5, 60_000, 0.4), (98.0, 'buy', 0.5, 120_000, 0.6)]
    rep = _le(entry=100.0).replay(fills)
    assert abs(rep.snapshot(100.0)['fee_paid'] - 1.0) < 1e-12


def test_nonuniform_partial_reduce_avg_not_zero():
    # mainnet ADA 2026-07-08 实证序列：买469 + 卖60(非均匀 size,方向计数归零但净仓 409)。
    # 旧引擎 avg 分级键用计数 → 丢档填 0 → 幻影浮盈 +13.5%(409×mark/cap)。
    # 修后:快照 avg=精确加权成本 0.17192;pnl_ratio 回到理智带(|·|<1%)。
    le = LiveEquity(521.0, entry_price=0.1719)
    le.record_fill(0.171920, 'buy', 469.0, 1_000_000, fee=0.04)
    le.record_fill(0.175640, 'sell', 60.0, 2_000_000, fee=0.005)
    snap = le.snapshot(0.171365)
    assert abs(snap['net_position'] - 409.0) < 1e-9
    assert abs(snap['avg_price'] - 0.171920) < 1e-9      # 真实成本,绝不为 0
    assert abs(snap['pnl_ratio']) < 0.01                  # 旧 bug 此处 +0.135
    assert abs(snap['realized_pnl'] - 0.2232) < 1e-3      # 60×(0.17564−0.17192)


def test_avg_cost_weighted_add_and_flip_reset():
    le = LiveEquity(1000.0, entry_price=100.0)
    le.record_fill(100.0, 'buy', 1.0, 60_000)
    le.record_fill(98.0, 'buy', 1.0, 120_000)
    assert abs(le._avg_cost() - 99.0) < 1e-9              # 同向加权
    le.record_fill(101.0, 'sell', 0.5, 180_000)
    assert abs(le._avg_cost() - 99.0) < 1e-9              # 部分减仓成本不变
    le.record_fill(103.0, 'sell', 2.5, 240_000)           # 过零翻空 1.0
    assert abs(le._avg_cost() - 103.0) < 1e-9             # 翻向价重置


def test_net_position_property_matches_snapshot():
    """net_position property = Σ(order_dir×order_num),与 snapshot['net_position'] 同源同值
    (ledger claims 的真相源,勿走整条引擎重放)。"""
    le = LiveEquity(1000.0)
    assert le.net_position == 0.0
    le.record_fill(100.0, 'buy', 5.0, 1000)
    assert abs(le.net_position - 5.0) < 1e-12
    le.record_fill(101.0, 'sell', 2.0, 2000)
    assert abs(le.net_position - 3.0) < 1e-12
    assert abs(le.net_position - le.snapshot(102.0)['net_position']) < 1e-9


# ── pnl_exact:逐笔精确直算(spec 2026-07-12-honest-record-pnl 组件一) ──


def test_pnl_exact_long_reduce_and_unreal():
    le = LiveEquity(1000.0, entry_price=100.0)
    le.record_fill(100.0, 'buy', 2.0, 1000, fee=0.0)
    le.record_fill(98.0, 'buy', 2.0, 2000, fee=0.0)       # avg 99, net 4
    le.record_fill(101.0, 'sell', 1.0, 3000, fee=0.0)     # realize (101-99)*1 = +2
    r = le.pnl_exact(100.0)
    assert abs(r['realized'] - 2.0) < 1e-12
    assert abs(r['unreal'] - (100.0 - 99.0) * 3.0) < 1e-12
    assert abs(r['pnl'] - 5.0) < 1e-12
    assert abs(r['pnl_ratio'] - 0.005) < 1e-15


def test_pnl_exact_short_side_symmetric():
    le = LiveEquity(1000.0, entry_price=100.0)
    le.record_fill(100.0, 'sell', 2.0, 1000, fee=0.0)     # 开空 avg 100
    le.record_fill(97.0, 'buy', 1.0, 2000, fee=0.0)       # realize (100-97)*1 = +3
    r = le.pnl_exact(99.0)
    assert abs(r['realized'] - 3.0) < 1e-12
    assert abs(r['unreal'] - (100.0 - 99.0) * 1.0) < 1e-12   # 空 1 手,mark 99
    assert abs(r['pnl'] - 4.0) < 1e-12


def test_pnl_exact_cross_zero_realizes_then_flips():
    le = LiveEquity(1000.0, entry_price=100.0)
    le.record_fill(100.0, 'buy', 1.0, 1000, fee=0.0)
    le.record_fill(103.0, 'sell', 2.5, 2000, fee=0.0)     # 平 1 (+3),翻空 1.5 @103
    r = le.pnl_exact(102.0)
    assert abs(r['realized'] - 3.0) < 1e-12
    assert abs(r['unreal'] - (103.0 - 102.0) * 1.5) < 1e-12
    assert abs(r['pnl'] - 4.5) < 1e-12


def test_pnl_exact_deducts_fee_and_funding():
    le = LiveEquity(1000.0, entry_price=100.0)
    le.record_fill(100.0, 'buy', 1.0, 1000, fee=0.3)
    le.add_funding(0.2)
    r = le.pnl_exact(101.0)
    assert abs(r['pnl'] - (1.0 - 0.3 - 0.2)) < 1e-12


def test_pnl_exact_flat_exact_zero():
    le = LiveEquity(1000.0, entry_price=100.0)
    le.record_fill(100.0, 'buy', 1.0, 1000, fee=0.0)
    le.record_fill(102.0, 'sell', 1.0, 2000, fee=0.0)     # 恰好平净
    r = le.pnl_exact(50.0)                                 # mark 任意,不应影响
    assert abs(r['realized'] - 2.0) < 1e-12
    assert abs(r['unreal']) < 1e-12
    assert abs(r['pnl'] - 2.0) < 1e-12


# ── 事故形状回归(2026-07-11 VVV manual 记录失真根治验证) ──


def test_vvv_gt00_shape_manual_close_must_be_negative():
    """mainnet 2026-07-11 gt00 实测形状:三批买入 41.8@11.10/10.72/10.47,manual 关格
    合成 reduce 卖 125.4@10.434。旧引擎重放记录 +$15(+1.00%);真实 ≈ −$41.3。
    直算后必须为负且等于手算值。"""
    le = LiveEquity(1493.0, entry_price=11.10)
    le.record_fill(11.10, 'buy', 41.8, 1_000, fee=0.02)
    le.record_fill(10.72, 'buy', 41.8, 2_000, fee=0.02)
    le.record_fill(10.47, 'buy', 41.8, 3_000, fee=0.03)
    le.record_fill(10.434, 'sell', 125.4, 4_000, fee=0.0)   # ledger:reduce 合成行,零费
    snap = le.snapshot(10.434)
    avg = (11.10 + 10.72 + 10.47) / 3.0
    expect = (10.434 - avg) * 125.4 - 0.07                   # ≈ −41.37
    assert snap['pnl_ratio'] < -0.02                          # 绝不允许为正
    assert abs(snap['pnl_ratio'] * 1493.0 - expect) < 1e-6
    assert abs(snap['net_position']) < 1e-9


def test_zro_partial_fill_shape_no_distortion():
    """ZRO 2026-07-10 实测:一张限价单同毫秒被 3 个对手方拆成 16.9+66.7+428.5。
    非均匀拆单不得引入任何失真:随后整量平仓 realized = Δpx × 总量,精确。"""
    le = LiveEquity(1493.0, entry_price=0.9412)
    for sz in (16.9, 66.7, 428.5):
        le.record_fill(0.9400, 'buy', sz, 1_000, fee=0.0)
    le.record_fill(0.9450, 'sell', 512.1, 2_000, fee=0.0)
    snap = le.snapshot(0.9450)
    assert abs(snap['realized_pnl'] - 0.005 * 512.1) < 1e-9
    assert abs(snap['net_position']) < 1e-9


def test_transfer_pair_conserves_pnl_at_any_mark():
    """转仓守恒:A(成本100)按 mark 105 零费转 1 手给 B;任意后续 mark m 下,
    A+B 的 pnl_exact 之和 == 无转仓时 A 独自持有的 (m−100)。"""
    a = LiveEquity(1000.0, entry_price=100.0)
    a.record_fill(100.0, 'buy', 1.0, 1_000, fee=0.0)
    a.record_fill(105.0, 'sell', 1.0, 2_000, fee=0.0)       # 转出(合成,零费)
    b = LiveEquity(1000.0, entry_price=105.0)
    b.record_fill(105.0, 'buy', 1.0, 2_000, fee=0.0)        # 转入(合成,零费)
    for m in (90.0, 100.0, 105.0, 111.5):
        total = a.pnl_exact(m)['pnl'] + b.pnl_exact(m)['pnl']
        assert abs(total - (m - 100.0)) < 1e-9, (m, total)
