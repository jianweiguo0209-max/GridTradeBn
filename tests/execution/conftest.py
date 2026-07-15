"""同币多格机制测试与部署默认 tier2_cap 解耦（2026-07-15 用户定 cap 2→1 后）。

tests/execution/ 下的账本/兄弟平仓/簿对齐/净额等测试直接在同一币上建 ≥2 格,测的是
「给定同币多格,机制是否正确」——这机制在 cap=1 实盘只于关旧格→开新格的同步瞬间不产生
（close_by_tag 同步关到 CLOSED 释放槽位后才 open,见 grid_executor._finalize_record）,
但仍须防御性正确、须测。故本目录测试把 tier2_cap 恢复到 2（gx.open/grids.create 均读
gridtrade.config.DEFAULT_TIER_POLICY），与线上默认(1)解耦。策略默认值由 tests/test_config.py
钉住,不受此影响。
"""
from dataclasses import replace

import pytest

import gridtrade.config as _cfg


@pytest.fixture(autouse=True)
def _multigrid_slots(monkeypatch):
    monkeypatch.setattr(_cfg, 'DEFAULT_TIER_POLICY',
                        replace(_cfg.DEFAULT_TIER_POLICY, tier2_cap=2))
