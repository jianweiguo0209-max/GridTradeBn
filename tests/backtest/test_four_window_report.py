import json
from types import SimpleNamespace

import pandas as pd

from gridtrade.backtest.four_window_report import (BASELINE, build_equity_curve,
                                                   evaluate_window, write_report)


def _metrics(name, **overrides):
    b = BASELINE[name]
    out = {'n_grids': b['n_grids'], 'ret': b['ret'], 'mdd': b['mdd'], 'calmar': b['calmar'],
           'n_broke': 0, 'n_blown': 0}
    out.update(overrides)
    return out


def test_negative_window_ignores_calmar_but_checks_ret_and_mdd():
    ok, reasons = evaluate_window('W1', _metrics('W1', calmar=-999))
    assert ok and reasons == []
    ok, reasons = evaluate_window('W1', _metrics('W1', ret=BASELINE['W1']['ret'] - 0.0031))
    assert not ok and any('收益' in x for x in reasons)


def test_positive_window_checks_calmar_and_safety_vetoes():
    ok, reasons = evaluate_window('OOS', _metrics('OOS', calmar=BASELINE['OOS']['calmar'] - .31))
    assert not ok and any('Calmar' in x for x in reasons)
    ok, reasons = evaluate_window('OOS', _metrics('OOS', n_broke=1))
    assert not ok and any('一票否决' in x for x in reasons)


def test_zero_grid_holdout_cannot_false_pass():
    ok, reasons = evaluate_window('HOLD-A', _metrics('HOLD-A', n_grids=0,
                                                     ret=0.0, mdd=0.0, calmar=0.0))
    assert not ok and any('数据/票池不完整' in x for x in reasons)


def test_equity_curve_and_report_include_skipped_windows(tmp_path):
    df = pd.DataFrame({'run_time': pd.to_datetime(['2025-08-15', '2025-08-15 01:00']),
                       'offset': [0, 1], 'symbol': ['A', 'B'], 'pnl_ratio': [.01, -.02],
                       'exit_reason': ['窗口结束', '固定止损'], 'n_fills': [2, 3]})
    curve = build_equity_curve(df)
    assert 'portfolio_equity' in curve and len(curve) == 2
    rows = [{'window': 'W1', 'passed': False, 'ret': -.04, 'mdd': .05,
             'calmar': -4., 'n_broke': 0, 'n_blown': 0,
             'failure_reasons': '收益未通过'}]
    meta = {'name': 'x', 'status': 'FAILED', 'stopped_reason': 'W1 未通过',
            'parameters': {'weight_list': [1, 1, 1]}}
    write_report(str(tmp_path), meta, rows, {'W1': df})
    text = (tmp_path / 'report.html').read_text(encoding='utf-8')
    assert 'W1 未通过' in text and '前序窗口未通过' in text
    assert 'W1 爆雷' in text and 'HOLD-A 腰斩' in text and 'HOLD-B 牛市' in text
    assert 'min-width:76px' in text and 'white-space:nowrap' in text
    assert '组合权益曲线（按窗口分别展示）' in text
    assert '已完成窗口拼接展示' not in text
    assert 'class="worse">-4.00%' in text       # ret 候选及差值逊于基线 → 红色
    assert 'class="worse">5.00%' in text        # MDD 更高 → 红色
    assert '<h3>W1 爆雷</h3>' in text
    assert (tmp_path / 'window_summary.csv').exists()
    assert json.loads((tmp_path / 'parameters.json').read_text())['status'] == 'FAILED'


def test_baseline_table_order_colors_and_equity_are_split_by_window(tmp_path):
    def frame(day, pnl):
        return pd.DataFrame({
            'run_time': pd.to_datetime([day, pd.Timestamp(day) + pd.Timedelta(hours=1)]),
            'offset': [0, 1], 'symbol': ['A', 'B'], 'pnl_ratio': [pnl, pnl / 2],
            'exit_reason': ['窗口结束', '窗口结束'], 'n_fills': [2, 1]})

    rows = []
    for name in ('W1', 'W2'):
        b = BASELINE[name]
        rows.append({'window': name, 'passed': True,
                     'ret': b['ret'] + .01, 'mdd': b['mdd'] - .005,
                     'calmar': b['calmar'] + 1, 'n_broke': 0, 'n_blown': 0,
                     'failure_reasons': ''})
    meta = {'name': 'split-curves', 'status': 'PASSED', 'stopped_reason': '',
            'parameters': {}, 'mode': 'full'}
    write_report(str(tmp_path), meta, rows,
                 {'W1': frame('2025-08-15', .01), 'W2': frame('2025-10-15', .02)})
    text = (tmp_path / 'report.html').read_text(encoding='utf-8')

    header = ('<th>窗口</th><th>判定</th><th>基线ret</th><th>候选ret</th><th>Δret</th>'
              '<th>基线MDD</th><th>候选MDD</th><th>ΔMDD</th>'
              '<th>基线Calmar</th><th>候选Calmar</th><th>破网/爆仓</th><th>说明</th>')
    assert header in text
    assert text.count('class="better"') == 10  # 2窗 × (候选+Δ ret/MDD + 候选Calmar)
    equity_panel = text.split('组合权益曲线（按窗口分别展示）', 1)[1].split(
        '<div class="panel"><h2>分 offset 统计', 1)[0]
    assert equity_panel.count('<svg ') == 2
    assert '<h3>W1 爆雷</h3>' in equity_panel and '<h3>W2 震荡</h3>' in equity_panel


def test_color_comparison_uses_displayed_two_decimal_values(tmp_path):
    """底层有微差、但百分比两位小数相同：候选和Δ均黑色，且不出现-0.00pp。"""
    b = BASELINE['W1']
    rows = [{'window': 'W1', 'passed': True,
             'ret': b['ret'] - 0.000001,       # -0.0001pp，显示仍为 -2.83%
             'mdd': b['mdd'] + 0.000001,       # +0.0001pp，显示仍为 4.29%
             'calmar': b['calmar'] + 0.001,    # 两位小数仍与基线相同
             'n_broke': 0, 'n_blown': 0, 'failure_reasons': ''}]
    df = pd.DataFrame({'run_time': pd.to_datetime(['2025-08-15', '2025-08-15 01:00']),
                       'offset': [0, 1], 'symbol': ['A', 'B'], 'pnl_ratio': [0., 0.],
                       'exit_reason': ['未触网', '未触网'], 'n_fills': [0, 0]})
    meta = {'name': 'rounded-neutral', 'status': 'PASSED', 'stopped_reason': '',
            'parameters': {}}
    write_report(str(tmp_path), meta, rows, {'W1': df})
    text = (tmp_path / 'report.html').read_text(encoding='utf-8')
    baseline_panel = text.split('六窗与基线', 1)[1].split(
        '组合权益曲线（按窗口分别展示）', 1)[0]
    assert 'class="better"' not in baseline_panel
    assert 'class="worse"' not in baseline_panel
    assert '-0.00pp' not in baseline_panel
    assert baseline_panel.count('+0.00pp') >= 2


def test_cli_runner_stops_after_first_failed_window_and_still_reports(tmp_path, monkeypatch):
    from scripts import four_window_backtest as cli
    cache_root = tmp_path / 'cache'
    (cache_root / '1h' / 'AAA' / 'USDT:USDT').mkdir(parents=True)
    monkeypatch.setenv('TZ', 'Asia/Shanghai')
    monkeypatch.setattr(cli.V, 'default_cache_root', lambda: str(cache_root))
    called = []
    monkeypatch.setattr(cli.SW, 'preload_window',
                        lambda *a, **k: (called.append(a[2]) or
                                        SimpleNamespace(raw=[1], days=61)))
    frame = pd.DataFrame({'run_time': pd.to_datetime(['2025-08-15']), 'offset': [0],
                          'symbol': ['AAA/USDT:USDT'], 'pnl_ratio': [-.05],
                          'exit_reason': ['窗口结束'], 'n_fills': [1]})
    monkeypatch.setattr(cli.SW, 'run_arm', lambda *a, **k: frame)
    monkeypatch.setattr(cli.SW, 'metrics', lambda *a, **k: {
        'n_grids': 1, 'ret': -.05, 'ann': -.2, 'mdd': .06, 'calmar': -3.,
        'win_rate': 0., 'n_fills': 1., 'n_broke': 0, 'n_blown': 0,
        'n_fixstop': 0, 'n_pvstop': 0, 'worst_grid': -.05})
    out = tmp_path / 'result'
    assert cli.run('fail-fast', str(out), workers=1, heartbeat_sec=1) == 2
    assert called == ['W1']
    report = next(out.rglob('report.html'))
    assert '后续窗口未执行' in report.read_text(encoding='utf-8')


def test_full_mode_runs_all_six_even_when_each_window_fails(tmp_path, monkeypatch):
    from scripts import four_window_backtest as cli
    cache_root = tmp_path / 'cache'
    (cache_root / '1h' / 'AAA' / 'USDT:USDT').mkdir(parents=True)
    monkeypatch.setenv('TZ', 'Asia/Shanghai')
    monkeypatch.setattr(cli.V, 'default_cache_root', lambda: str(cache_root))
    called = []
    monkeypatch.setattr(cli.SW, 'preload_window',
                        lambda *a, **k: (called.append(a[2]) or
                                        SimpleNamespace(raw=[1], days=61)))
    frame = pd.DataFrame({'run_time': pd.to_datetime(['2025-08-15']), 'offset': [0],
                          'symbol': ['AAA/USDT:USDT'], 'pnl_ratio': [-.05],
                          'exit_reason': ['窗口结束'], 'n_fills': [1]})
    monkeypatch.setattr(cli.SW, 'run_arm', lambda *a, **k: frame)
    monkeypatch.setattr(cli.SW, 'metrics', lambda *a, **k: {
        'n_grids': 1, 'ret': -.05, 'ann': -.2, 'mdd': .06, 'calmar': -3.,
        'win_rate': 0., 'n_fills': 1., 'n_broke': 0, 'n_blown': 0,
        'n_fixstop': 0, 'n_pvstop': 0, 'worst_grid': -.05})
    out = tmp_path / 'result'
    assert cli.run('full-run', str(out), workers=1, heartbeat_sec=1, mode='full') == 2
    assert called == ['W1', 'W2', 'OOS', 'IS', 'HOLD-A', 'HOLD-B']
    report = next(out.rglob('report.html')).read_text(encoding='utf-8')
    assert '全测模式（失败仍继续完成六窗）' in report
    assert '前序窗口未通过，未继续执行' not in report
