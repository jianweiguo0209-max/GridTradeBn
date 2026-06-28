"""transition_status 事务内重校验（消除 TOCTOU）的串行契约守卫。

注意：真正的 TOCTOU 差异（并发交错下源态已变 -> 新码 StateError vs 旧码
ConcurrencyError）无法用单连接 StaticPool 串行测试复现——需要真实并发 mutator。
该真并发红->绿测试延后到多监控机（leader 选举/分片）阶段补齐。本文件只串行守卫
重构后必须保持的契约：非法转换 -> StateError；缺失行 -> ConcurrencyError；
合法转换但版本陈旧 -> ConcurrencyError；正常路径成功。
"""
import pytest

from gridtrade.state.models import (Grid, ACTIVE, OPENING, FAILED, CLOSING,
                                    PENDING, ConcurrencyError, StateError)


def _repo():
    from gridtrade.state.store import StateStore
    from gridtrade.state.grids import GridRepository
    s = StateStore.in_memory()
    s.create_all()
    return GridRepository(s)


def _grid(**kw):
    base = dict(id='', exchange='okx', symbol='BTC/USDT:USDT', status=PENDING)
    base.update(kw)
    return Grid(**base)


def test_illegal_transition_from_terminal_raises_state_error():
    """从终态（FAILED）转出非法 -> StateError（事务内重校验后仍守此契约）。"""
    repo = _repo()
    g = repo.create(_grid(status=OPENING))
    failed = repo.transition_status(g.id, FAILED, expected_version=g.version)
    with pytest.raises(StateError):
        repo.transition_status(failed.id, ACTIVE, expected_version=failed.version)


def test_legal_transition_stale_version_raises_concurrency_error():
    """转换合法但版本陈旧 -> ConcurrencyError（乐观锁守卫不破）。"""
    repo = _repo()
    g = repo.create(_grid(status=OPENING))
    # OPENING->ACTIVE 合法，但 expected_version 错误 -> 版本守卫 rowcount==0
    with pytest.raises(ConcurrencyError):
        repo.transition_status(g.id, ACTIVE, expected_version=999)


def test_missing_grid_raises_concurrency_error():
    repo = _repo()
    with pytest.raises(ConcurrencyError):
        repo.transition_status('nope', ACTIVE, expected_version=1)


def test_normal_transition_succeeds_and_bumps_version():
    """正常路径：事务内重校验不改变成功语义。"""
    repo = _repo()
    g = repo.create(_grid(status=OPENING))
    active = repo.transition_status(g.id, ACTIVE, expected_version=g.version)
    assert active.status == ACTIVE
    assert active.version == g.version + 1
    closing = repo.transition_status(active.id, CLOSING,
                                     expected_version=active.version)
    assert closing.status == CLOSING and closing.version == active.version + 1
