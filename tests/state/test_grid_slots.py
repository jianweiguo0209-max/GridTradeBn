# tests/state/test_grid_slots.py
"""active_symbol 槽位方案（cap=2 全套改造）：UNIQUE(exchange, active_symbol) 原样保留，
active_symbol 存 'SYM#slot'——同币最多 cap 个槽、抢槽仍是 DB 原子操作（TOCTOU 防线不丢）。"""
import pytest
from sqlalchemy import select

from gridtrade.state.grids import GridRepository
from gridtrade.state.models import ACTIVE, CLOSED, CLOSING, ConcurrencyError, Grid, grids

SYM = 'BTC/USDT:USDT'


def _grid(**kw):
    base = dict(id='', exchange='okx', symbol=SYM, status=ACTIVE)
    base.update(kw)
    return Grid(**base)


def _raw_active_symbol(store, gid):
    with store.engine.connect() as c:
        return c.execute(select(grids.c.active_symbol)
                         .where(grids.c.id == gid)).scalar()


def test_create_claims_slot0_then_slot1(store):
    repo = GridRepository(store)
    g0 = repo.create(_grid(), max_slots=2)
    g1 = repo.create(_grid(), max_slots=2)
    assert _raw_active_symbol(store, g0.id) == SYM + '#0'
    assert _raw_active_symbol(store, g1.id) == SYM + '#1'
    assert repo.count_active_by_symbol('okx', SYM) == 2


def test_create_third_slot_exhausted_raises(store):
    repo = GridRepository(store)
    repo.create(_grid(), max_slots=2)
    repo.create(_grid(), max_slots=2)
    with pytest.raises(ConcurrencyError):
        repo.create(_grid(), max_slots=2)          # DB 层兜底：门链竞态漏网也开不出第 3 格


def test_create_max_slots_1_preserves_old_semantics(store):
    repo = GridRepository(store)
    repo.create(_grid(), max_slots=1)
    with pytest.raises(ConcurrencyError):
        repo.create(_grid(), max_slots=1)


def test_close_frees_slot_and_it_is_reclaimable(store):
    repo = GridRepository(store)
    g0 = repo.create(_grid(), max_slots=2)
    g1 = repo.create(_grid(), max_slots=2)
    repo.transition_status(g0.id, CLOSING, expected_version=g0.version)
    g0b = repo.get(g0.id)
    assert _raw_active_symbol(store, g0.id) == SYM + '#0'   # CLOSING 仍占槽（保留后缀）
    repo.transition_status(g0.id, CLOSED, expected_version=g0b.version)
    assert _raw_active_symbol(store, g0.id) is None         # 终态释放
    assert repo.count_active_by_symbol('okx', SYM) == 1
    g2 = repo.create(_grid(), max_slots=2)                  # 释放的 #0 可复用
    assert _raw_active_symbol(store, g2.id) == SYM + '#0'
    assert _raw_active_symbol(store, g1.id) == SYM + '#1'   # 在位格不受影响


def test_get_active_by_symbol_prefix_match(store):
    repo = GridRepository(store)
    g0 = repo.create(_grid(), max_slots=2)
    assert repo.get_active_by_symbol('okx', SYM).id == g0.id
    assert repo.get_active_by_symbol('okx', 'ETH/USDT:USDT') is None
    assert repo.count_active_by_symbol('okx', 'ETH/USDT:USDT') == 0
