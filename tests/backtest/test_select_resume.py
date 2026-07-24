"""select_cache 断点续跑:按天 checkpoint,杀了从已完成的轮接着跑,续跑结果与整跑 diff==0。"""
import pandas as pd

from tests.backtest.test_selection_replay import _seed_cache, STRAT, FACTORS


def _run_times(ws, we):
    return [pd.Timestamp(t) for t in pd.date_range(ws, we, freq='1H')]


def test_checkpoint_roundtrip(tmp_path):
    from gridtrade.backtest import select_cache as SC
    cache = _seed_cache(tmp_path, ['AAA/USDT:USDT'])
    params = {'version': SC.CACHE_VERSION, 'x': 1}
    done = ['2024-01-10 00:00:00', '2024-01-10 01:00:00']
    grids = [('2024-01-10 00:00:00', 0, 'AAA/USDT:USDT')]
    SC.save_checkpoint(cache, 'k1', params, done, grids)
    got = SC.load_checkpoint(cache, 'k1', params)
    assert got is not None
    assert set(got['done']) == set(done) and got['grids'] == grids
    assert SC.load_checkpoint(cache, 'k1', {'version': SC.CACHE_VERSION, 'x': 2}) is None  # 参数变→不复用
    SC.clear_checkpoint(cache, 'k1')
    assert SC.load_checkpoint(cache, 'k1', params) is None


def test_select_grids_resume_matches_full(tmp_path, monkeypatch):
    from gridtrade.backtest import select_cache as SC
    from gridtrade.backtest.backtest_run import select_grids
    syms = ['AAA/USDT:USDT', 'BBB/USDT:USDT', 'CCC/USDT:USDT', 'DDD/USDT:USDT']
    cache = _seed_cache(tmp_path, syms)
    ws, we = '2024-01-09', '2024-01-11'

    full = select_grids(cache, syms, ws, we, STRAT, FACTORS, top_volume_pct=0.0)

    # 模拟"跑到一半被杀":删掉整窗成品,手写一个覆盖前半轮的 checkpoint
    key, params = SC.compute_key(cache, syms, ws, we, '1h', 0.0, (), STRAT, FACTORS,
                                 top_volume_pct=0.0)
    SC.clear_final(cache, key)                           # 抹掉整窗缓存 → 下次是 miss
    rts = _run_times(ws, we)
    split = rts[len(rts) // 2]
    done_half = [str(rt) for rt in rts if rt <= split]
    grids_half = [g for g in full if g[0] <= split]
    SC.save_checkpoint(cache, key, params, done_half, grids_half)

    # 记录续跑实际喂给 replay 的轮数(应只有后半)
    import gridtrade.backtest.backtest_run as BR
    seen = {}
    real = BR.SR.replay_selection

    def spy(cache_, universe, run_times, *a, **k):
        seen['n'] = len(list(run_times)); return real(cache_, universe, run_times, *a, **k)

    monkeypatch.setattr(BR.SR, 'replay_selection', spy)
    resumed = select_grids(cache, syms, ws, we, STRAT, FACTORS, top_volume_pct=0.0)

    assert seen['n'] == len(rts) - len(done_half), '续跑该只算未完成的轮'
    key_of = lambda g: (str(g[0]), g[1], g[2]['symbol'], round(float(g[2]['close']), 8))
    assert sorted(map(key_of, resumed)) == sorted(map(key_of, full))   # 续跑==整跑 diff==0


def test_crash_midrun_then_resume_completes(tmp_path, monkeypatch):
    """真续跑:跑到一半崩(flush 过 checkpoint)→ 重跑接上 → 结果与整跑 diff==0。"""
    from gridtrade.backtest import select_cache as SC
    from gridtrade.backtest import backtest_run as BR
    from gridtrade.backtest.backtest_run import select_grids
    syms = ['AAA/USDT:USDT', 'BBB/USDT:USDT', 'CCC/USDT:USDT']
    cache = _seed_cache(tmp_path, syms)
    ws, we = '2024-01-09', '2024-01-11'
    full = select_grids(cache, syms, ws, we, STRAT, FACTORS, top_volume_pct=0.0)
    key, params = SC.compute_key(cache, syms, ws, we, '1h', 0.0, (), STRAT, FACTORS,
                                 top_volume_pct=0.0)
    SC.clear_final(cache, key)

    monkeypatch.setattr(SC, 'CKPT_EVERY', 2)            # 每 2 轮刷一次
    real_bpc = BR.SR.build_pit_candidates
    calls = {'n': 0}

    def faulty(*a, **k):
        calls['n'] += 1
        if calls['n'] == 6:                            # 第 6 轮开始时崩(前 5 轮已完成,刷到第 4)
            raise RuntimeError('模拟崩溃')
        return real_bpc(*a, **k)

    monkeypatch.setattr(BR.SR, 'build_pit_candidates', faulty)
    try:
        select_grids(cache, syms, ws, we, STRAT, FACTORS, top_volume_pct=0.0)
        assert False, '本应崩溃'
    except RuntimeError:
        pass
    ck = SC.load_checkpoint(cache, key, params)
    assert ck is not None and len(ck['done']) == 4     # flush 落在 rt2/rt4

    monkeypatch.setattr(BR.SR, 'build_pit_candidates', real_bpc)   # 修复后重跑
    resumed = select_grids(cache, syms, ws, we, STRAT, FACTORS, top_volume_pct=0.0)
    key_of = lambda g: (str(g[0]), g[1], g[2]['symbol'], round(float(g[2]['close']), 8))
    assert sorted(map(key_of, resumed)) == sorted(map(key_of, full))
    assert SC.load_checkpoint(cache, key, params) is None          # 跑完清掉


def test_select_grids_clears_checkpoint_on_complete(tmp_path):
    from gridtrade.backtest import select_cache as SC
    from gridtrade.backtest.backtest_run import select_grids
    syms = ['AAA/USDT:USDT', 'BBB/USDT:USDT', 'CCC/USDT:USDT']
    cache = _seed_cache(tmp_path, syms)
    ws, we = '2024-01-09', '2024-01-10'
    select_grids(cache, syms, ws, we, STRAT, FACTORS, top_volume_pct=0.0)
    key, params = SC.compute_key(cache, syms, ws, we, '1h', 0.0, (), STRAT, FACTORS,
                                 top_volume_pct=0.0)
    assert SC.load_checkpoint(cache, key, params) is None    # 跑完清 checkpoint
    assert SC.load(cache, key, params) is not None           # 成品落盘
