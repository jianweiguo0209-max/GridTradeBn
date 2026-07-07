"""gearing 换元等价(spec 2026-07-07-account-leverage-gearing):
新 (gearing, max_rate=1.0) 与旧 (leverage, max_rate) 参数化逐位一致;executor 向后兼容旧签名。"""
from gridtrade.core.grid_engine import grid_order_info


def test_order_num_equal_within_fp():
    # 5×0.68=3.4000000000000004(浮点),与 3.4 差 4e-16——远低于交易所 lot 精度,
    # 用 1e-12 相对容差断言等价;价格序列与杠杆无关,逐位相等。
    old = grid_order_info(302.0, 5.0, 10.0, 12.0, 20, 9.0, 13.0, max_rate=0.68)
    new = grid_order_info(302.0, 3.4, 10.0, 12.0, 20, 9.0, 13.0, max_rate=1.0)
    assert abs(old['每笔数量'] - new['每笔数量']) < 1e-12 * old['每笔数量']
    assert list(old['价格序列']) == list(new['价格序列'])


def test_executor_backcompat_and_gearing(tmp_path):
    from gridtrade.execution.grid_executor import GridExecutor
    from gridtrade.state.store import StateStore
    store = StateStore.in_memory()
    class _A:  # 最小假 adapter(本测试不触网)
        pass
    ex_old = GridExecutor(_A(), store, cap=100.0, leverage=5.0)      # 旧签名(存量测试形态)
    ex_new = GridExecutor(_A(), store, cap=100.0, gearing=3.4)       # 新签名
    ex_def = GridExecutor(_A(), store, cap=100.0)                    # 全默认
    assert abs(ex_old.gearing - 3.4) < 1e-12
    assert abs(ex_new.gearing - 3.4) < 1e-12
    assert abs(ex_def.gearing - 3.4) < 1e-12
