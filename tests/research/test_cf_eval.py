# tests/research/test_cf_eval.py
"""cf_eval 单元锚(spec §4):已知格逐位复现 s030_calib / geo 产物。
依赖本机研究资产(缺失即 skip);读 1m 缓存,运行 ~1min。"""
import importlib.util
import os

import pandas as pd
import pytest

RD = 'data/score_research_2026-07-21'

pytestmark = pytest.mark.skipif(
    not os.path.exists(os.path.join(RD, 'ablation', 's030_calib_W1.parquet')),
    reason='research assets not on this machine')


@pytest.fixture(scope='module')
def ctx():
    spec = importlib.util.spec_from_file_location(
        'cf_eval', os.path.join(RD, 'cf_eval.py'))
    cf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cf)
    from gridtrade.backtest import vision as V
    from gridtrade.backtest.cache import ParquetCache
    return cf, ParquetCache(V.default_cache_root())


def test_s030_caliber_reproduces_anchor(ctx):
    cf, cache = ctx
    s030 = pd.read_parquet(os.path.join(RD, 'ablation', 's030_calib_W1.parquet')).head(3)
    geo = pd.read_parquet(os.path.join(RD, 'ablation', 'geo_W1.parquet'))[
        ['rt', 'symbol', 'Atr_5']]
    s030 = s030.merge(geo, on=['rt', 'symbol'], how='left')
    for _, r in s030.iterrows():
        m1 = cache.read_all_days('1m', r['symbol'])
        fd = cache.read_all_days('funding', r['symbol'])
        out = cf.eval_grid(m1, fd, pd.Timestamp(r['rt']), r['Atr_5'], geometry='v2')
        assert out is not None
        assert out['pnl_s030'] == r['pnl']          # 逐位相等,禁 atol


def test_e0_geo_caliber_reproduces_anchor(ctx):
    cf, cache = ctx
    geo = pd.read_parquet(os.path.join(RD, 'ablation', 'geo_W1.parquet')).head(3)
    for _, r in geo.iterrows():
        m1 = cache.read_all_days('1m', r['symbol'])
        fd = cache.read_all_days('funding', r['symbol'])
        out = cf.eval_grid(m1, fd, pd.Timestamp(r['rt']), r['Atr_5'], geometry='geo')
        assert out is not None
        assert out['pnl_e0'] == r['pnl_m30_c16']


def test_insufficient_data_returns_none(ctx):
    cf, _ = ctx
    assert cf.eval_grid(None, None, pd.Timestamp('2026-01-01'), 0.02) is None
    assert cf.eval_grid(pd.DataFrame(), None, pd.Timestamp('2026-01-01'), 0.02) is None
    assert cf.eval_grid(None, None, pd.Timestamp('2026-01-01'), float('nan')) is None
