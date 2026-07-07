# tests/execution/test_gearing_db_impact.py
"""DB 影响验证矩阵(spec 2026-07-07-account-leverage-gearing,用户点名):
grids.leverage 列在 gearing 重构下行为惰性——旧行(5.0 时代)/新行(3.4)/NULL 共存安全,
restore/补单尺寸/图表逐位连续,证明无需数据迁移。"""
from types import SimpleNamespace

from gridtrade.core.grid_engine import grid_order_info
from gridtrade.exchanges.base import Instrument
from gridtrade.exchanges.fake import FakeExchange
from gridtrade.execution.grid_executor import GridExecutor
from gridtrade.execution.reconciler import Reconciler
from gridtrade.state.models import ACTIVE, Grid

SYM = 'BTC/USDT:USDT'
GP = {'low_price': 98.0, 'high_price': 102.0, 'grid_count': 8,
      'stop_low_price': 97.0, 'stop_high_price': 103.0}
OLD_CAP = 302.0


def _new_executor(store, price=100.0):
    fx = FakeExchange(instruments=[Instrument(SYM, 0.1, 0.001, 0.001, 'live', 0)], price=price)
    fx.set_price(SYM, price)
    return fx, GridExecutor(fx, store, cap=1000.0, gearing=3.4)     # 新代码形态


def _old_row(gx, *, order_num):
    """直接落一条旧部署产物行:leverage=5.0(旧语义),order_num 可传持久化真值或 None。"""
    return gx.grids.create(Grid(
        id='', exchange='fake', symbol=SYM, status=ACTIVE, offset=0, tag='old',
        entry_price=100.0, low_price=GP['low_price'], high_price=GP['high_price'],
        stop_low_price=GP['stop_low_price'], stop_high_price=GP['stop_high_price'],
        grid_count=GP['grid_count'], order_num=order_num, leverage=5.0, cap=OLD_CAP))


def _old_code_order_num():
    """旧代码(leverage=5, max_rate=0.68)对同几何的重算值。"""
    gi = grid_order_info(OLD_CAP, 5.0, GP['low_price'], GP['high_price'],
                         GP['grid_count'], GP['stop_low_price'], GP['stop_high_price'],
                         max_rate=0.68)
    return float(gi['每笔数量'])


def test_restore_old_row_uses_persisted_order_num(store):
    """用例1:旧行带持久化 order_num → restore 逐位用真值,不经任何重算(金钱路径连续)。"""
    _, gx = _new_executor(store)
    persisted = _old_code_order_num()
    g = _old_row(gx, order_num=persisted)
    Reconciler(gx).restore(g.id)
    assert gx._geom[g.id]['order_num'] == persisted                 # 逐位


def test_restore_old_row_fallback_recompute_equal(store):
    """用例2:旧行缺 order_num(回退重算路径)→ 新代码重算与旧代码重算 1e-12 等价。"""
    _, gx = _new_executor(store)
    g = _old_row(gx, order_num=None)
    Reconciler(gx).restore(g.id)
    old_val = _old_code_order_num()
    assert abs(gx._geom[g.id]['order_num'] - old_val) < 1e-12 * old_val


def test_new_row_roundtrip(store):
    """用例3:新行 open 落库 leverage==gearing(3.4),restore 后几何逐位连续。"""
    _, gx = _new_executor(store)
    gid = gx.open('fake', SYM, GP)
    g = gx.grids.get(gid)
    assert abs(g.leverage - 3.4) < 1e-12
    before = {'order_num': gx._geom[gid]['order_num'],
              'price_array': list(gx._geom[gid]['price_array'])}
    gx._geom.pop(gid)
    Reconciler(gx).restore(gid)
    assert gx._geom[gid]['order_num'] == before['order_num']
    assert list(gx._geom[gid]['price_array']) == before['price_array']


def _chart_row(leverage):
    return SimpleNamespace(cap=OLD_CAP, leverage=leverage,
                           low_price=GP['low_price'], high_price=GP['high_price'],
                           grid_count=GP['grid_count'],
                           stop_low_price=GP['stop_low_price'],
                           stop_high_price=GP['stop_high_price'])


def test_chart_lines_leverage_invariant():
    """用例4:图表价格档与杠杆无关——旧行(5.0)/新行(3.4)渲染逐位一致。"""
    from gridtrade.dashboard.gridchart import _grid_lines
    old_lines = _grid_lines(_chart_row(5.0))
    new_lines = _grid_lines(_chart_row(3.4))
    assert old_lines == new_lines and old_lines != []


def test_chart_null_leverage_safe():
    """用例5:史前行 leverage=NULL → 图表不崩、返回 [](现行为保持)。"""
    from gridtrade.dashboard.gridchart import _grid_lines
    assert _grid_lines(_chart_row(None)) == []
