import os

import pandas as pd

from tests.backtest.test_selection_replay import _seed_cache, _bars, STRAT, FACTORS


def test_compute_key_deterministic_and_sensitive(tmp_path):
    from gridtrade.backtest import select_cache as SC
    syms = ['AAA/USDT:USDT', 'BBB/USDT:USDT']
    cache = _seed_cache(tmp_path, syms)
    ws, we = pd.Timestamp('2024-01-10'), pd.Timestamp('2024-01-11')
    k1, _ = SC.compute_key(cache, syms, ws, we, '1h', 0.0, (), STRAT, FACTORS)
    k2, _ = SC.compute_key(cache, syms, ws, we, '1h', 0.0, (), STRAT, FACTORS)
    assert k1 == k2                                              # 确定性
    k3, _ = SC.compute_key(cache, syms, ws, we, '1h', 1e6, (), STRAT, FACTORS)
    assert k3 != k1                                             # min_quote_volume 改 → 换 key
    k4, _ = SC.compute_key(cache, syms, ws, we, '1h', 0.0, (),
                           dict(STRAT, choose_symbols=2), FACTORS)
    assert k4 != k1                                             # choose_symbols 改 → 换 key


def test_save_load_roundtrip(tmp_path):
    from gridtrade.backtest import select_cache as SC
    syms = ['AAA/USDT:USDT']
    cache = _seed_cache(tmp_path, syms)
    ws, we = pd.Timestamp('2024-01-10'), pd.Timestamp('2024-01-11')
    key, params = SC.compute_key(cache, syms, ws, we, '1h', 0.0, (), STRAT, FACTORS)
    assert SC.load(cache, key, params) is None                 # 未写 → MISS
    grids = [(pd.Timestamp('2024-01-10'), 0, pd.Series({'symbol': 'AAA/USDT:USDT', 'close': 1.0}))]
    SC.save(cache, key, params, grids)
    got = SC.load(cache, key, params)
    assert got is not None and len(got) == 1
    assert got[0][2]['symbol'] == 'AAA/USDT:USDT' and got[0][1] == 0


def test_load_rejects_param_mismatch(tmp_path):
    from gridtrade.backtest import select_cache as SC
    syms = ['AAA/USDT:USDT']
    cache = _seed_cache(tmp_path, syms)
    ws, we = pd.Timestamp('2024-01-10'), pd.Timestamp('2024-01-11')
    key, params = SC.compute_key(cache, syms, ws, we, '1h', 0.0, (), STRAT, FACTORS)
    SC.save(cache, key, params, [(ws, 0, pd.Series({'symbol': 'X', 'close': 1.0}))])
    tampered = dict(params, choose_symbols=999)                # params 不一致 → 拒（防碰撞）
    assert SC.load(cache, key, tampered) is None


def test_fingerprint_changes_with_new_day(tmp_path):
    from gridtrade.backtest import select_cache as SC
    syms = ['AAA/USDT:USDT']
    cache = _seed_cache(tmp_path, syms)
    ws, we = pd.Timestamp('2024-01-10'), pd.Timestamp('2024-01-11')
    k1, _ = SC.compute_key(cache, syms, ws, we, '1h', 0.0, (), STRAT, FACTORS)
    cache.write('1h', 'AAA/USDT:USDT', '2024-02-01',
                _bars('AAA/USDT:USDT', n=5, start='2024-02-01'))   # 新增一天 → 指纹变
    k2, _ = SC.compute_key(cache, syms, ws, we, '1h', 0.0, (), STRAT, FACTORS)
    assert k2 != k1


def test_enabled_env(monkeypatch):
    from gridtrade.backtest import select_cache as SC
    monkeypatch.delenv('BT_SELECT_CACHE', raising=False)
    assert SC.enabled() is True
    monkeypatch.setenv('BT_SELECT_CACHE', 'off')
    assert SC.enabled() is False
