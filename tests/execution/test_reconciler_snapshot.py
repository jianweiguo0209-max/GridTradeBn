# tests/execution/test_reconciler_snapshot.py
"""reconcile 三方法快照供给等价：对账/漂移/保险丝三态。"""
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.exchanges.base import Instrument
from gridtrade.execution.grid_executor import GridExecutor
from gridtrade.execution.reconciler import Reconciler
from gridtrade.execution.snapshot import build_account_snapshot

BTC = 'BTC/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}


def _setup(store, stop_orders=False):
    ex = FakeExchange(instruments=[Instrument(BTC, 0.1, 0.001, 0.001, 'live', 0)],
                      price=100.0)
    ex.set_price(BTC, 100.0)
    gx = GridExecutor(ex, store, cap=1000.0, leverage=5.0, stop_orders_enabled=stop_orders)
    gid = gx.open('fake', BTC, dict(GP), tag='t0')
    return ex, gx, gid


def test_reconcile_open_orders_snapshot_clean(store):
    ex, gx, gid = _setup(store)
    rec = Reconciler(gx)
    snap = build_account_snapshot(ex, [BTC])
    assert rec.reconcile_open_orders(gid, BTC, snapshot=snap) == {'canceled': 0, 'replaced': 0}


def test_reconcile_snapshot_grace_then_replace(store):
    # 挂单从快照消失（且成交不可见）→ 宽限 2 轮后重挂，与逐格路径同语义
    ex, gx, gid = _setup(store)
    rec = Reconciler(gx)                       # replace_grace=2
    sell = [o for o in ex.fetch_open_orders(BTC) if o.side == 'sell'][0]
    ex._open.pop(sell.id, None)                # 从交易所丢单（成交不可见）
    snap = build_account_snapshot(ex, [BTC])
    assert rec.reconcile_open_orders(gid, BTC, snapshot=snap)['replaced'] == 0   # 第 1 轮宽限
    snap = build_account_snapshot(ex, [BTC])
    assert rec.reconcile_open_orders(gid, BTC, snapshot=snap)['replaced'] == 1   # 第 2 轮重挂


def test_position_drift_via_snapshot(store):
    ex, gx, gid = _setup(store)
    gx.sync(gid, BTC)
    rec = Reconciler(gx)
    ex.create_market_order(BTC, 'sell', 3 * gx._geom[gid]['order_num'],
                           client_oid='external:0')     # 外部动仓
    snap = build_account_snapshot(ex, [BTC])
    d = rec.check_position_drift(gid, BTC, snapshot=snap)
    assert d is not None and d['ok'] is False


def test_position_drift_snapshot_missing_position_means_zero(store):
    ex, gx, gid = _setup(store)
    rec = Reconciler(gx)
    snap = build_account_snapshot(ex, [BTC])
    d = rec.check_position_drift(gid, BTC, snapshot=snap)
    assert d is not None and d['exchange'] == 0.0       # 无仓位行 → 0（开网即 flat）


def test_fuse_replaced_and_fired_via_snapshot(store):
    ex, gx, gid = _setup(store, stop_orders=True)
    rec = Reconciler(gx)
    g = gx.grids.get(gid)
    # 保险丝在挂 → 无动作
    snap = build_account_snapshot(ex, [BTC])
    assert rec.reconcile_fuses(gid, BTC, snapshot=snap) == {'replaced': 0, 'fired': False}
    # 丢一根（低侧）且未成交 → 重挂（FakeExchange 触发单存于 _stops，非 _open）
    ex._stops[BTC] = [s for s in ex._stops[BTC] if s.id != g.fuse_low_oid]
    snap = build_account_snapshot(ex, [BTC])
    out = rec.reconcile_fuses(gid, BTC, snapshot=snap)
    assert out == {'replaced': 1, 'fired': False}


def test_fuse_fired_outside_snapshot_window_still_detected(store):
    # XYZ-MSTR 2026-07-05 事故复现：快照 trades 窗口起点=全格最小游标，活跃格把窗口
    # 推过安静格的保险丝成交时刻 → 旧实现漏判"已触发"→ 误重挂覆写 oid → 永久盲区。
    # 修复：快照查不到时重挂前逐格全量直查（fetch_my_trades since=None）。
    from gridtrade.exchanges.base import Trade
    ex, gx, gid = _setup(store, stop_orders=True)
    g = gx.grids.get(gid)
    # 模拟保险丝已触发：从 book 消失 + 成交带 fuse 的 order_id（ts=1，逻辑时钟很早）
    ex._stops[BTC] = [s for s in ex._stops[BTC] if s.id != g.fuse_low_oid]
    ex._trades.append(Trade(id='fusefill', client_oid='f', symbol=BTC, side='buy',
                            price=97.0, size=4.0, fee=0.0, ts=1,
                            order_id=g.fuse_low_oid))
    rec = Reconciler(gx)
    # 快照窗口起点推到 fuse 成交之后（模拟其他活跃格推进了最小游标）
    snap = build_account_snapshot(ex, [BTC], trade_since_ms=1_000_000)
    assert snap.trades_for(BTC) == []                    # 窗口确实错过了 fuse 成交
    out = rec.reconcile_fuses(gid, BTC, snapshot=snap)
    assert out['fired'] is True                          # 直查回退 → 正确判定已触发
    assert gx.grids.get(gid).status == 'CLOSED'          # 撑网全拆


def test_fuse_truly_lost_still_replaced_after_direct_scan(store):
    # 真"被丢"（无成交）：直查确认未触发后照常重挂，不误判 fired。
    ex, gx, gid = _setup(store, stop_orders=True)
    g = gx.grids.get(gid)
    ex._stops[BTC] = [s for s in ex._stops[BTC] if s.id != g.fuse_low_oid]
    rec = Reconciler(gx)
    snap = build_account_snapshot(ex, [BTC], trade_since_ms=1_000_000)
    out = rec.reconcile_fuses(gid, BTC, snapshot=snap)
    assert out == {'replaced': 1, 'fired': False}
    assert gx.grids.get(gid).status == 'ACTIVE'
