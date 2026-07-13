import pandas as pd


def test_vision_sync_main_wires_args(monkeypatch, tmp_path):
    from gridtrade.backtest import vision_sync as VS
    calls = {}
    monkeypatch.setenv('BT_DATA_DIR', str(tmp_path))
    monkeypatch.setattr('gridtrade.backtest.vision.list_archive_symbols',
                        lambda quote='USDT', **kw: ['BTC/USDT:USDT', 'ETH/USDT:USDT'])
    def fake_warm(cache, universe, start_ms, end_ms, *, timeframes, quote, workers,
                  session=None, log=print):
        calls.update(universe=universe, start_ms=start_ms, end_ms=end_ms,
                     timeframes=timeframes, workers=workers)
        return {'1h': {'rows': 1, 'files': 1}, 'skipped_cached': 0,
                'retry_later': 0, 'empty_days': 0}
    monkeypatch.setattr('gridtrade.backtest.vision.warm_vision', fake_warm)
    VS.main(['2020-01-01', '2020-01-31', '--tf', '1h', '--workers', '2'])
    assert calls['universe'] == ['BTC/USDT:USDT', 'ETH/USDT:USDT']
    assert calls['timeframes'] == ('1h',)
    assert calls['workers'] == 2
    assert calls['start_ms'] == int(pd.Timestamp('2020-01-01').value // 1_000_000)
    # end 含当天：end_ms = 2020-02-01 00:00 - 1ms
    assert calls['end_ms'] == int(pd.Timestamp('2020-02-01').value // 1_000_000) - 1


def test_vision_sync_symbols_override(monkeypatch, tmp_path):
    from gridtrade.backtest import vision_sync as VS
    monkeypatch.setenv('BT_DATA_DIR', str(tmp_path))
    seen = {}
    monkeypatch.setattr('gridtrade.backtest.vision.warm_vision',
                        lambda cache, universe, s, e, **kw: seen.update(u=universe) or
                        {'1m': {'rows': 0, 'files': 0}, 'skipped_cached': 0,
                         'retry_later': 0, 'empty_days': 0})
    VS.main(['2020-01-01', '2020-01-02', '--symbols', 'DOGE/USDT:USDT'])
    assert seen['u'] == ['DOGE/USDT:USDT']
