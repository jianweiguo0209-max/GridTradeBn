"""reconcile_fuses 三态对账测试 + reconcile_open_orders 不误撤保险丝。"""
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.execution.grid_executor import GridExecutor
from gridtrade.execution.reconciler import Reconciler
from gridtrade.state.models import CLOSED

SYM = 'BTC/USDC:USDC'
PARAMS = dict(low_price=90.0, high_price=110.0, grid_count=10,
              stop_low_price=80.0, stop_high_price=120.0)


def _open(store):
    fake = FakeExchange()
    fake.set_price(SYM, 100.0)
    ex = GridExecutor(fake, store, cap=1000.0, leverage=5.0,
                      stop_orders_enabled=True)
    gid = ex.open('hl', SYM, dict(PARAMS))
    return ex, fake, gid


def test_fuses_in_book_no_action(store):
    ex, fake, gid = _open(store)
    rec = Reconciler(ex)
    out = rec.reconcile_fuses(gid, SYM)
    assert out == {'replaced': 0, 'fired': False, 'futile': False}


def test_fired_fuse_tears_down_grid(store):
    ex, fake, gid = _open(store)
    rec = Reconciler(ex)
    fake.set_price(SYM, 79.0)               # 穿破 stop_low -> sell 保险丝触发、平多
    out = rec.reconcile_fuses(gid, SYM)
    assert out['fired'] is True
    assert ex.grids.get(gid).status == CLOSED
    assert not fake.fetch_open_orders(SYM)  # 撑网全拆，网格限价单全撤


def test_dropped_fuse_replaced_not_closed(store):
    ex, fake, gid = _open(store)
    g = ex.grids.get(gid)
    fake._stops[SYM] = [s for s in fake._stops[SYM]
                        if s.id != g.fuse_low_oid]   # 模拟 low 保险丝被交易所丢、无成交
    rec = Reconciler(ex)
    out = rec.reconcile_fuses(gid, SYM)
    assert out['fired'] is False
    assert out['replaced'] == 1
    assert ex.grids.get(gid).status != CLOSED
    new_low = ex.grids.get(gid).fuse_low_oid
    assert new_low != g.fuse_low_oid                 # 回写了新 oid
    assert any(s.id == new_low for s in fake._stops[SYM])


def test_disabled_short_circuits(store):
    fake = FakeExchange(); fake.set_price(SYM, 100.0)
    ex = GridExecutor(fake, store, cap=1000.0, leverage=5.0,
                      stop_orders_enabled=False)
    gid = ex.open('hl', SYM, dict(PARAMS))
    out = Reconciler(ex).reconcile_fuses(gid, SYM)
    assert out == {'replaced': 0, 'fired': False, 'futile': False}


# ── CRITICAL ADDITION: reconcile_open_orders must NOT cancel fuse stop orders ──

def test_reconcile_open_orders_does_not_cancel_fuses(store):
    """reconcile_open_orders 应跳过保险丝 stop orders，不误撤。

    HL 默认 fetch_open_orders 走 frontendOpenOrders，含 trigger/stop orders；
    FakeExchange.fetch_open_orders 忠实模拟该行为（返回 _stops）。
    保险丝不在 grid_orders（expected）中，如无排除逻辑将被当 unexpected 撤掉。
    """
    ex, fake, gid = _open(store)
    rec = Reconciler(ex)
    g = ex.grids.get(gid)
    low_oid = g.fuse_low_oid
    high_oid = g.fuse_high_oid

    # reconcile_open_orders 后保险丝应仍在交易所
    rec.reconcile_open_orders(gid, SYM)

    stops_on_exchange = {s.id for s in fake._stops.get(SYM, [])}
    assert low_oid in stops_on_exchange,  'fuse_low_oid was wrongly cancelled'
    assert high_oid in stops_on_exchange, 'fuse_high_oid was wrongly cancelled'


def test_fired_fuse_ingests_fill_ledger_matches_exchange(store):
    """端到端(spec 2026-07-16 §5,闭合 KITE grid 33b02230 背离):有仓状态下丝触发 →
    reconcile_fuses 摄入丝成交 + 关格 → 干预熔断的输入 check_position_drift 返 ok(不再背离)。

    KITE bug 机制:BinanceAdapter 未覆写 order_status(返 'unknown')→ reconcile_fuses 认不出丝
    已触发 → **不关格**,格持续活跃 → 其净仓持续进 drift model、而交易所已被 reduce-only 丝平至
    flat → model≠exchange 连续 2 轮 → 外部干预熔断。Task 1 覆写 order_status 后丝被判 'filled' →
    ingest_fuse_fills + close。本测试用 FakeExchange(order_status 已正确三态)锁住下游
    reconcile→ingest→close→drift 全链在丝触发后收敛到"不熔断"(Task 1 单测另验覆写本身)。

    两账本细节(为何断言 live 而非持久 accounting):`ingest_fuse_fills` 把丝的 reduce-only 平仓记入
    **live 内存账本**(→ 净仓归 0);持久 `accounting.net_position` 只在 sync 保存、丝触发后未再
    sync 故留旧值——但无害,因 `check_position_drift` 的 model 只对 `list_active()` 求和,关格后
    该格被排除、其陈旧持久净仓不再进 model。故 live 归 0 验 ingest 生效、drift.ok 验不熔断。"""
    ex, fake, gid = _open(store)
    fake.set_price(SYM, 85.0); ex.sync(gid, SYM)          # 穿破所有买线(85<low 90、>stop 80)→ 满多仓,fills 入账
    rec = Reconciler(ex)
    base = rec.check_position_drift(gid, SYM)
    assert base['ok'] and base['model'] > 0               # 基线:活跃格账本多仓 == 交易所,无背离
    fake.set_price(SYM, 79.0)                              # 穿 stop_low 80 → sell 丝触发(reduce-only 平多;买线已全成交,无新成交)
    out = rec.reconcile_fuses(gid, SYM)
    assert out['fired'] is True                           # 丝被识别为已触发(非误判丢失→重挂)
    assert abs(fake.fetch_positions(SYM).net_size) < 1e-6 # 交易所已平(丝 reduce-only 平满多)
    assert abs(ex.live.get(gid).net_position) < 1e-6      # live 账本归 0:ingest_fuse_fills 记入了丝平仓
    assert gid not in {g.id for g in ex.grids.list_active()}   # 关格:移出活跃集(KITE bug 此处仍活跃 → 熔断)
    drift = rec.check_position_drift(gid, SYM)             # 干预熔断的输入
    assert drift['ok'] and abs(drift['drift']) < 1e-6     # 不熔断:model 已不含该格幽灵仓 → drift≈0 ≤ tol
