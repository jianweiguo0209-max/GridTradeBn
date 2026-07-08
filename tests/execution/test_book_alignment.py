# tests/execution/test_book_alignment.py
"""账本↔DB fills 对齐(spec 2026-07-09-book-db-alignment):
grid_fills 是第三方可写的真相源(scheduler 转仓/手工修复),内存账本必须每 sync 轮收敛到
DB 集合——mainnet GRAM 转仓首样本实证:scheduler 写的合成行 monitor 账本看不见,
acc 停旧值直到重启。顺序行追加、乱序行整本重建(LiveEquity 平均成本路径依赖)。"""
from gridtrade.exchanges.base import Instrument
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.execution.grid_executor import GridExecutor
from gridtrade.execution.reconciler import Reconciler
from gridtrade.state.models import Fill

BTC = 'BTC/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}


def _two_grids(store):
    ex = FakeExchange(instruments=[Instrument(BTC, 0.1, 0.001, 0.001, 'live', 0)],
                      price=100.0)
    ex.set_price(BTC, 100.0)
    gx = GridExecutor(ex, store, cap=1000.0, leverage=5.0)
    ga = gx.open('fake', BTC, dict(GP), tag='tA')
    gb = gx.open('fake', BTC, dict(GP), tag='tB')
    return ex, gx, ga, gb


def test_cross_process_transfer_reaches_survivor_book(store):
    """进程A(scheduler)写转仓合成行 → 进程B(monitor)sync 后幸存格账本归一。"""
    ex, gxA, ga, gb = _two_grids(store)
    # 对冲态入库(真实成交行,双进程都可重放)
    for gid, side in ((ga, 'buy'), (gb, 'sell')):
        gxA.fills.add_if_new(Fill(trade_id='t-%s' % gid, grid_id=gid, line_index=3,
                                  side=side, price=100.0, size=5.0, fee=0.1, ts=1000))
        gxA.live[gid].record_fill(100.0, side, 5.0, 1000, 0.1)
    ex._pos[BTC] = type(ex.fetch_positions(BTC))(BTC, 0.0, 100.0)

    # 进程B:独立执行器(monitor),先加载幸存格
    gxB = GridExecutor(ex, store, cap=1000.0, leverage=5.0)
    Reconciler(gxB).restore(gb)
    assert abs(gxB.live[gb].net_position + 5.0) < 1e-9      # B 账本:空 5

    gxA.close(ga, BTC, '周期再平衡')                          # A 关格 → 转仓行落库
    assert abs(gxA.live[gb].net_position) < 1e-9             # A 进程账本已归一(旧行为)

    out = gxB.sync(gb, BTC)                                  # B 进程:sync 对齐步收敛
    assert abs(gxB.live[gb].net_position) < 1e-9             # ← 缺口修复点
    assert abs(gxB.accounting.get(gb).net_position) < 1e-9   # acc 同轮反映


def test_manual_backfill_old_ts_triggers_rebuild(store):
    """手工补历史 ts 成交(GRAM 修复同型)→ 乱序 → 整本重建,净仓/费用正确。"""
    ex, gx, ga, gb = _two_grids(store)
    gx.fills.add_if_new(Fill(trade_id='t-new', grid_id=ga, line_index=3, side='buy',
                             price=100.0, size=5.0, fee=0.1, ts=5000))
    gx.live[ga].record_fill(100.0, 'buy', 5.0, 5000, 0.1)
    gx.live[ga].add_funding(0.7)
    # 手工补一笔更早的成交(直接写 DB,模拟修复 session)
    gx.fills.add_if_new(Fill(trade_id='t-backfill', grid_id=ga, line_index=2, side='sell',
                             price=101.0, size=2.0, fee=0.05, ts=3000))
    gx.sync(ga, BTC)
    assert abs(gx.live[ga].net_position - 3.0) < 1e-9        # 5 - 2
    assert abs(gx.live[ga].real_fee_paid - 0.15) < 1e-9      # 重建带真实 fee
    assert abs(gx.live[ga].funding_paid - 0.7) < 1e-12       # funding 跨重建保留


def test_ordered_ledger_row_appends_without_rebuild(store):
    """顺序新行(转仓 ts=now)走追加:账本对象不换(引用不变=未重建)。"""
    from gridtrade.state.models import now_ms
    ex, gx, ga, gb = _two_grids(store)
    gx.fills.add_if_new(Fill(trade_id='t-a', grid_id=ga, line_index=3, side='buy',
                             price=100.0, size=5.0, fee=0.1, ts=1000))
    gx.live[ga].record_fill(100.0, 'buy', 5.0, 1000, 0.1)
    gx._book_ids.setdefault(ga, set()).add('t-a')   # 真实路径 record_fill 皆登记
    book_ref = gx.live[ga]
    gx.fills.add_if_new(Fill(trade_id='ledger:closeshare:%s:%d:0' % (ga, now_ms()),
                             grid_id=ga, line_index=-1, side='sell', price=100.0,
                             size=5.0, fee=0.0, ts=now_ms()))
    gx.sync(ga, BTC)
    assert gx.live[ga] is book_ref                            # 追加,未重建
    assert abs(gx.live[ga].net_position) < 1e-9


def test_no_catchup_on_normal_flow(store):
    """单进程常规成交流:自己写的行都在集合里,对齐步空转(账本引用不变)。"""
    ex, gx, ga, gb = _two_grids(store)
    ex.set_price(BTC, 98.5)
    gx.sync(ga, BTC)
    ref = gx.live[ga]
    gx.sync(ga, BTC)
    assert gx.live[ga] is ref
