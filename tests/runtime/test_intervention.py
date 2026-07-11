"""外部干预熔断 + 丝重挂即消守卫(spec 2026-07-12-honest-record-pnl 组件三)。

事故原型(2026-07-11 mainnet):用户 HL 前端手动全平 → HL 自动撤无仓位 reduce-only 丝
→ monitor 每轮重挂 → 无限 churn;同时账本与交易所净仓背离,系统继续按幻影库存管理。
两道闸:①同币 drift 连续 ≥2 轮 → 熔断(只读+告警,resolve=按钮/指令);
②丝"挂了下轮又不在"连续 ≥2 轮 → 停止重挂+告警。
"""
from gridtrade.exchanges.base import Instrument
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.execution.grid_executor import GridExecutor
from gridtrade.execution.gates import GateChain, GridProposal
from gridtrade.execution.manager import GridManager
from gridtrade.execution.reconciler import Reconciler
from gridtrade.runtime.commands import INTERVENTION_PREFIX, execute_command
from gridtrade.runtime.cycles import braked_symbols, run_monitor_cycle
from gridtrade.state.control import ControlFlagRepository

BTC = 'BTC/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}
STOP_CFG = {'stop_loss': 0.034, 'trailing_k': 0.3, 'trailing_floor': 0.00618}


def _setup(store, price=100.0):
    ex = FakeExchange(instruments=[Instrument(BTC, 0.1, 0.001, 0.001, 'live', 0)],
                      price=price)
    ex.set_price(BTC, price)
    gx = GridExecutor(ex, store, cap=1000.0, leverage=5.0)
    mgr = GridManager(gx, GateChain([]), stop_cfg=STOP_CFG)
    flags = ControlFlagRepository(store)
    return ex, gx, mgr, flags


def _open_with_position(store, size=12.0):   # > 1.5×order_num(≈5.67) 容差,构成真背离
    """开格并注入已入账的持仓(模型净仓 size),交易所同步同仓。"""
    ex, gx, mgr, flags = _setup(store)
    gid = mgr.open_proposals([GridProposal(exchange='fake', symbol=BTC,
                                           grid_params=dict(GP), offset=0,
                                           tag='t0', source='test')])[0]
    from gridtrade.state.models import Fill
    gx.fills.add_if_new(Fill(trade_id='seed', grid_id=gid, line_index=3, side='buy',
                             price=100.0, size=size, fee=0.0, ts=1000))
    gx.live[gid].record_fill(100.0, 'buy', size, 1000, 0.0)
    gx._book_ids.setdefault(gid, set()).add('seed')
    gx.sync(gid, BTC)                       # accounting 落库(净仓=size)
    ex._pos[BTC] = type(ex.fetch_positions(BTC))(BTC, size, 100.0)
    return ex, gx, mgr, flags, gid


def _flatten_externally(ex):
    """模拟用户在交易所前端手动平仓:交易所净仓归零,系统无感知。"""
    ex._pos[BTC] = type(ex.fetch_positions(BTC))(BTC, 0.0, 100.0)


def test_drift_two_rounds_trips_brake(store):
    ex, gx, mgr, flags, gid = _open_with_position(store)
    rec = Reconciler(gx)
    _flatten_externally(ex)
    run_monitor_cycle(rec, mgr, log=lambda *a: None, flags=flags)   # 第1轮:容忍瞬差
    assert not flags.get(INTERVENTION_PREFIX + BTC)
    run_monitor_cycle(rec, mgr, log=lambda *a: None, flags=flags)   # 第2轮:熔断
    assert flags.get(INTERVENTION_PREFIX + BTC)
    assert braked_symbols(flags) == {BTC}


def test_braked_unit_is_read_only(store):
    ex, gx, mgr, flags, gid = _open_with_position(store)
    rec = Reconciler(gx)
    flags.set(INTERVENTION_PREFIX + BTC, True, actor='test')
    calls = {'created': 0}
    orig_limit, orig_stop = ex.create_limit_order, ex.create_stop_order
    ex.create_limit_order = lambda *a, **k: calls.__setitem__('created', calls['created'] + 1) or orig_limit(*a, **k)
    ex.create_stop_order = lambda *a, **k: calls.__setitem__('created', calls['created'] + 1) or orig_stop(*a, **k)
    _flatten_externally(ex)
    ex.set_price(BTC, 90.0)                 # 深跌:未熔断时必触固定止损→关格(交易所写入)
    out = run_monitor_cycle(rec, mgr, log=lambda *a: None, flags=flags)
    assert calls['created'] == 0            # 零新单:不补线单/不重挂丝
    assert not out['monitored'][0].get('closed')
    assert gx.grids.get(gid).status == 'ACTIVE'   # 不关格(留待 resolve)


def test_transient_single_round_drift_does_not_trip(store):
    ex, gx, mgr, flags, gid = _open_with_position(store)
    rec = Reconciler(gx)
    _flatten_externally(ex)
    run_monitor_cycle(rec, mgr, log=lambda *a: None, flags=flags)   # 第1轮 drift
    ex._pos[BTC] = type(ex.fetch_positions(BTC))(BTC, 12.0, 100.0)  # 瞬差自愈
    run_monitor_cycle(rec, mgr, log=lambda *a: None, flags=flags)   # 第2轮干净
    assert not flags.get(INTERVENTION_PREFIX + BTC)
    _flatten_externally(ex)
    run_monitor_cycle(rec, mgr, log=lambda *a: None, flags=flags)   # 重新计数第1轮
    assert not flags.get(INTERVENTION_PREFIX + BTC)                 # streak 已重置过


def test_resolve_command_clears_brake(store):
    ex, gx, mgr, flags, gid = _open_with_position(store)
    flags.set(INTERVENTION_PREFIX + BTC, True, actor='monitor')

    class Cmd:
        type = 'RESOLVE_INTERVENTION'
        payload = '{"symbol": "%s"}' % BTC
        created_by = 'user'
    assert 'resolved' in execute_command(Cmd(), mgr, flags, exchange='fake')
    assert not flags.get(INTERVENTION_PREFIX + BTC)


def test_close_and_open_commands_refused_when_braked(store):
    import pytest
    ex, gx, mgr, flags, gid = _open_with_position(store)
    flags.set(INTERVENTION_PREFIX + BTC, True, actor='monitor')

    class Close:
        type = 'CLOSE_GRID'
        payload = '{"grid_id": "%s", "symbol": "%s"}' % (gid, BTC)
        created_by = 'user'
    with pytest.raises(RuntimeError, match='braked'):
        execute_command(Close(), mgr, flags, exchange='fake')
    assert gx.grids.get(gid).status == 'ACTIVE'


def test_scheduler_rotation_skips_braked_symbol(store):
    from gridtrade.runtime.cycles import run_scheduler_cycle
    from gridtrade.execution.triggers import TriggerEngine, TriggerContext
    ex, gx, mgr, flags, gid = _open_with_position(store)
    rec = Reconciler(gx)
    out = run_scheduler_cycle(mgr, TriggerEngine([]), rec,
                              TriggerContext('fake', None, {}),
                              close_tag='t0', braked_symbols=frozenset({BTC}),
                              log=lambda *a: None)
    assert out['closed'] == []
    assert gx.grids.get(gid).status == 'ACTIVE'


def test_fuse_futile_guard_stops_after_two_rounds(store):
    """交易所系统性拒收丝(挂上即被撤,churn 原型)→ 第1/2轮各重挂,第3轮起停手。"""
    ex, gx, mgr, flags, gid = _open_with_position(store)
    gx.stop_orders_enabled = True
    rec = Reconciler(gx)
    placed = {'n': 0}
    orig = ex.create_stop_order

    def create_then_vanish(*a, **k):
        placed['n'] += 1
        o = orig(*a, **k)
        # 模拟 HL 自动撤销无仓位 reduce-only 丝:挂上即消失(churn 原型)
        ex._stops[BTC] = [s for s in ex._stops.get(BTC, []) if s.id != o.id]
        return o
    ex.create_stop_order = create_then_vanish
    ex._stops.pop(BTC, None)     # 开格时挂的丝也被"交易所"撤掉 → 触发"被丢"分支
    r1 = rec.reconcile_fuses(gid, BTC)
    r2 = rec.reconcile_fuses(gid, BTC)
    assert r1['replaced'] == 2 and r2['replaced'] == 2
    n_before = placed['n']
    r3 = rec.reconcile_fuses(gid, BTC)
    r4 = rec.reconcile_fuses(gid, BTC)
    assert r3['futile'] and r4['futile']
    assert r3['replaced'] == 0 and r4['replaced'] == 0
    assert placed['n'] == n_before          # 停手后零新挂
