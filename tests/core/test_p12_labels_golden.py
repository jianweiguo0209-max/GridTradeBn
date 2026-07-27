"""黄金向量:hold_labels_JUL26.parquet 抽样 50 行,从本地 Vision 1m 重算,逐位一致。

标签表口径:rt = 窗**起点**,窗=[rt, rt+12h)(选币用时再 +12h 平移,与本测试无关)。
CI/无数据机器:文件缺失 → skip(研究资产 gitignore,先例 test_cf_patrol)。
"""
import os

import pandas as pd
import pytest

LAB = 'data/score_research_2026-07-21/ablation/hold_labels_JUL26.parquet'


@pytest.mark.skipif(not os.path.exists(LAB), reason='研究资产不在此机器(gitignore)')
def test_golden_jul26_sample_bitwise():
    from gridtrade.backtest import vision as V
    from gridtrade.backtest.cache import ParquetCache
    from gridtrade.core.p12_labels import window_label

    lab = pd.read_parquet(LAB)
    smp = lab.sample(50, random_state=7)
    cache = ParquetCache(V.default_cache_root())
    checked = 0
    for r in smp.itertuples(index=False):
        m1 = cache.read_all_days('1m', r.symbol)
        if m1 is None or m1.empty:
            continue
        w0, w1 = pd.Timestamp(r.rt), pd.Timestamp(r.rt) + pd.Timedelta(hours=12)
        seg = m1[(m1['candle_begin_time'] >= w0 - pd.Timedelta(hours=1))
                 & (m1['candle_begin_time'] < w1)].sort_values('candle_begin_time')
        # ⚠ 窗前必须给到与存档相同的 positional 前驱:取窗前 1h 足够(无 >1h 断档时等价)
        got = window_label(seg, w0, w1)
        if got is None:
            continue
        assert got[0] == pytest.approx(r.cross1, abs=1e-9), (r.symbol, r.rt)
        assert got[1] == pytest.approx(r.mae, abs=1e-12), (r.symbol, r.rt)
        checked += 1
    assert checked >= 30, '有效样本过少(%d),黄金测试没咬到' % checked
