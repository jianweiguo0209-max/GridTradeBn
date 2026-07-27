"""本目录的机制测试统一跑 **rank 选币线**(历史口径)。

**为什么要显式钉**:2026-07-27 起回测默认选币器跟随实盘 = `eff1`(见 backtest_run
`BT_SELECTION_RANKER`),而 eff1 要 1m 归档算 p12 标签,本目录的合成 cache 只种了 1h
⇒ 不钉就会一个币都选不出来,而这些测试(缓存命中/断点续跑/并行一致/三档分配/symbol_lock)
断言的是**机制**,会安静地变成"真空通过"——测了个寂寞还全绿。

**优先级**:显式实参 > 这里的 env > 代码默认。所以想测 eff1 的用例**照常传
`ranker='eff1'` 实参即可覆盖本 fixture**(见 test_select_eff1.py)。
"""
import pytest


@pytest.fixture(autouse=True)
def _pin_legacy_rank_selector(monkeypatch):
    monkeypatch.setenv('BT_SELECTION_RANKER', 'rank')
    monkeypatch.setenv('BT_MIN_TICKS', '0')
