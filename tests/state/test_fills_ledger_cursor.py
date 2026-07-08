"""游标防护(spec 2026-07-08-position-ledger):合成成交(trade_id 前缀 ledger:)
不得推进 max_ts(它是 fetch_my_trades 的 since 游标源,推过会漏摄入真实成交);
list_by_grid(restore 重放)必须包含合成行——重放正是 claims 恢复机制。"""
from gridtrade.state.fills import FillRepository
from gridtrade.state.models import Fill


def _fill(trade_id, ts, **kw):
    base = dict(trade_id=trade_id, grid_id='g1', line_index=5, side='sell',
                price=100.5, size=0.5, ts=ts)
    base.update(kw)
    return Fill(**base)


def test_max_ts_excludes_ledger_rows(store):
    repo = FillRepository(store)
    repo.add_if_new(_fill('t1', ts=100))
    repo.add_if_new(_fill('ledger:closeshare:g1:200', ts=200, line_index=-1))
    assert repo.max_ts('g1') == 100


def test_max_ts_all_ledger_rows_is_zero(store):
    repo = FillRepository(store)
    repo.add_if_new(_fill('ledger:reduce:g1:50', ts=50, line_index=-1))
    assert repo.max_ts('g1') == 0


def test_list_by_grid_includes_ledger_rows(store):
    repo = FillRepository(store)
    repo.add_if_new(_fill('t1', ts=100))
    repo.add_if_new(_fill('ledger:closeshare:g1:200', ts=200, line_index=-1))
    assert len(repo.list_by_grid('g1')) == 2
