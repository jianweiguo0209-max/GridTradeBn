"""六窗手工回测的冻结基线、闸门判定与离线报告（文件名保留旧入口兼容）。

本模块不访问网络、不运行仿真；把容易漂移的裁决口径集中在一处，供 CLI 和测试复用。
更新基线时必须同时修改 BASELINE_VERSION/BASELINE 并更新回测文档 §5.3。
"""
import html
import json
import math
import os
from datetime import datetime

import pandas as pd


BASELINE_VERSION = 's030-star-2026-07-24'
WINDOW_ORDER = ('W1', 'W2', 'OOS', 'IS', 'HOLD-A', 'HOLD-B')
WINDOW_LABELS = {
    'W1': 'W1 爆雷', 'W2': 'W2 震荡', 'OOS': 'OOS 逆风', 'IS': 'IS 磨涨',
    'HOLD-A': 'HOLD-A 腰斩', 'HOLD-B': 'HOLD-B 牛市',
}
BASELINE = {
    'W1': {'start': '2025-08-15', 'end': '2025-10-14', 'days': 61,
           'ret': -0.02830128, 'mdd': 0.04291595, 'calmar': -3.677905, 'n_grids': 1292},
    'W2': {'start': '2025-10-15', 'end': '2025-12-14', 'days': 61,
           'ret': 0.06313957, 'mdd': 0.02548333, 'calmar': 17.362982, 'n_grids': 1230},
    'OOS': {'start': '2026-01-01', 'end': '2026-02-28', 'days': 59,
            'ret': 0.01854559, 'mdd': 0.02587018, 'calmar': 4.653771, 'n_grids': 1248},
    'IS': {'start': '2026-03-01', 'end': '2026-06-30', 'days': 122,
           'ret': 0.13102271, 'mdd': 0.03969458, 'calmar': 11.219640, 'n_grids': 2752},
    'HOLD-A': {'start': '2025-02-01', 'end': '2025-03-31', 'days': 59,
               'ret': -0.0236, 'mdd': 0.0470, 'calmar': -2.9, 'n_grids': 1109},
    'HOLD-B': {'start': '2024-10-01', 'end': '2024-11-30', 'days': 61,
               'ret': 0.0158, 'mdd': 0.0239, 'calmar': 4.1, 'n_grids': 1169},
}
GATE = {'ret_tolerance_pp': 0.30, 'mdd_tolerance_pp': 0.30,
        'calmar_tolerance': 0.30, 'min_grid_ratio': 0.95}


def evaluate_window(name, metrics, baseline=None, gate=None):
    """返回 (passed, reasons)。负收益基线窗不比较符号扭曲的 Calmar。"""
    b = dict((baseline or BASELINE)[name])
    g = dict(GATE, **(gate or {}))
    reasons = []
    minimum_grids = int(math.floor(b['n_grids'] * g['min_grid_ratio']))
    if int(metrics.get('n_grids', 0)) < minimum_grids:
        reasons.append('网格数=%d，低于冻结基线%d的%.0f%%完整性下限%d；数据/票池不完整' %
                       (int(metrics.get('n_grids', 0)), b['n_grids'],
                        g['min_grid_ratio'] * 100, minimum_grids))
    if int(metrics.get('n_broke', 0)) > 0:
        reasons.append('破网=%d，安全一票否决' % int(metrics['n_broke']))
    if int(metrics.get('n_blown', 0)) > 0:
        reasons.append('爆仓=%d，安全一票否决' % int(metrics['n_blown']))
    ret_floor = b['ret'] - g['ret_tolerance_pp'] / 100.0
    if float(metrics['ret']) < ret_floor:
        reasons.append('收益 %.2f%% 低于基线 %.2f%% 的容忍下限 %.2f%%' %
                       (metrics['ret'] * 100, b['ret'] * 100, ret_floor * 100))
    mdd_ceiling = b['mdd'] + g['mdd_tolerance_pp'] / 100.0
    if float(metrics['mdd']) > mdd_ceiling:
        reasons.append('MDD %.2f%% 超过基线 %.2f%% 的容忍上限 %.2f%%' %
                       (metrics['mdd'] * 100, b['mdd'] * 100, mdd_ceiling * 100))
    if b['ret'] >= 0:
        calmar_floor = b['calmar'] - g['calmar_tolerance']
        if float(metrics['calmar']) < calmar_floor:
            reasons.append('Calmar %.2f 低于基线 %.2f 的容忍下限 %.2f' %
                           (metrics['calmar'], b['calmar'], calmar_floor))
    return not reasons, reasons


def build_equity_curve(df):
    """与 sweep.metrics 同口径：各 offset 按 12H 平仓事件复利，再等权。"""
    if df is None or df.empty:
        return pd.DataFrame(columns=['time', 'portfolio_equity'])
    d = df.copy()
    d['run_time'] = pd.to_datetime(d['run_time'])
    d['close_ts'] = d['run_time'] + pd.Timedelta('12H')
    events = pd.DatetimeIndex(sorted(d['close_ts'].unique()))
    out = pd.DataFrame(index=events)
    for off, group in d.sort_values('close_ts').groupby('offset'):
        curve = (1.0 + group.set_index('close_ts')['pnl_ratio']).cumprod()
        curve = curve[~curve.index.duplicated(keep='last')]
        out['offset_%s' % int(off)] = curve.reindex(events, method='ffill').fillna(1.0)
    out['portfolio_equity'] = out.mean(axis=1)
    out.index.name = 'time'
    return out.reset_index()


def build_offset_summary(df, days):
    columns = ['offset', 'n_grids', 'return', 'annual_return', 'mdd', 'calmar',
               'win_rate', 'mean_pnl', 'best_grid', 'worst_grid', 'n_fills']
    if df is None or df.empty:
        return pd.DataFrame(columns=columns)
    rows = []
    for off, g in df.sort_values('run_time').groupby('offset'):
        eq = (1.0 + g['pnl_ratio']).cumprod()
        ret = float(eq.iloc[-1] - 1.0)
        mdd = float((1.0 - eq / eq.cummax()).max())
        ann = (1.0 + ret) ** (365.0 / max(int(days), 1)) - 1.0
        traded = g[g['exit_reason'] != '未触网']
        rows.append({'offset': int(off), 'n_grids': int(len(g)), 'return': ret,
                     'annual_return': ann, 'mdd': mdd,
                     'calmar': ann / mdd if mdd > 1e-12 else math.inf,
                     'win_rate': float((traded['pnl_ratio'] > 0).mean()) if len(traded) else 0.0,
                     'mean_pnl': float(g['pnl_ratio'].mean()),
                     'best_grid': float(g['pnl_ratio'].max()),
                     'worst_grid': float(g['pnl_ratio'].min()),
                     'n_fills': float(g['n_fills'].mean())})
    return pd.DataFrame(rows, columns=columns)


def _pct(value):
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return '—'
    return '%+.2f%%' % (float(value) * 100)


def _svg_curve(equity):
    if equity is None or equity.empty or len(equity) < 2:
        return '<p class="muted">尚无权益曲线（窗口未运行或无网格）。</p>'
    y = equity['portfolio_equity'].astype(float).values
    lo, hi = float(y.min()), float(y.max())
    span = max(hi - lo, 1e-9)
    pts = []
    for i, value in enumerate(y):
        x = 20 + 960 * i / max(len(y) - 1, 1)
        py = 240 - 210 * (float(value) - lo) / span
        pts.append('%.1f,%.1f' % (x, py))
    return ('<svg viewBox="0 0 1000 270" role="img" aria-label="组合权益曲线">'
            '<line x1="20" y1="240" x2="980" y2="240" stroke="#d1d5db"/>'
            '<polyline fill="none" stroke="#2563eb" stroke-width="3" points="%s"/>'
            '<text x="20" y="20">max %.4f</text><text x="20" y="262">min %.4f</text></svg>'
            % (' '.join(pts), hi, lo))


def render_html(run_meta, summary, offsets, equity, exit_reasons):
    status = run_meta['status']
    badge = '通过' if status == 'PASSED' else '未通过' if status == 'FAILED' else '未完成'
    css_status = 'ok' if status == 'PASSED' else 'bad' if status == 'FAILED' else 'warn'
    mode = run_meta.get('mode', 'traditional')
    mode_text = ('传统模式（前序窗失败立即停止）' if mode == 'traditional'
                 else '全测模式（失败仍继续完成六窗）')
    rows = []
    for name in WINDOW_ORDER:
        row = (summary[summary['window'] == name]
               if summary is not None and 'window' in summary.columns else pd.DataFrame())
        b = BASELINE[name]
        if row.empty:
            rows.append('<tr><td>%s</td><td>跳过</td><td colspan="10">前序窗口未通过，未继续执行</td></tr>' % WINDOW_LABELS[name])
            continue
        r = row.iloc[0]
        reasons = html.escape(str(r.get('failure_reasons') or '—'))
        rows.append('<tr><td>%s</td><td><span class="pill %s">%s</span></td>'
                    '<td>%s</td><td>%s</td><td>%+.2fpp</td><td>%.2f</td><td>%.2f</td>'
                    '<td>%.2f%%</td><td>%.2f%%</td><td>%+.2fpp</td><td>%d/%d</td><td>%s</td></tr>' %
                    (WINDOW_LABELS[name], 'ok' if bool(r['passed']) else 'bad', '通过' if bool(r['passed']) else '失败',
                     _pct(b['ret']), _pct(r['ret']), (r['ret'] - b['ret']) * 100,
                     b['calmar'], r['calmar'], b['mdd'] * 100, r['mdd'] * 100,
                     (r['mdd'] - b['mdd']) * 100, int(r['n_broke']), int(r['n_blown']), reasons))
    offset_sections, exit_sections = [], []
    for name in WINDOW_ORDER:
        od = offsets[offsets['window'] == name] if 'window' in offsets.columns else pd.DataFrame()
        if not od.empty:
            off_rows = []
            for _, r in od.iterrows():
                off_rows.append('<tr><td>%d</td><td>%d</td><td>%s</td><td>%s</td><td>%.2f</td>'
                                '<td>%s</td><td>%.2f</td><td>%s</td></tr>' %
                                (int(r['offset']), int(r['n_grids']), _pct(r['return']),
                                 _pct(r['mdd']), r['calmar'], _pct(r['win_rate']),
                                 r['n_fills'], _pct(r['worst_grid'])))
            offset_sections.append('<h3>%s</h3><div class="scroll"><table><thead><tr>'
                                   '<th>offset</th><th>网格数</th><th>收益</th><th>MDD</th>'
                                   '<th>Calmar</th><th>胜率</th><th>均值成交</th><th>最差单格</th>'
                                   '</tr></thead><tbody>%s</tbody></table></div>' %
                                   (WINDOW_LABELS[name], ''.join(off_rows)))
        ed = (exit_reasons[exit_reasons['window'] == name]
              if 'window' in exit_reasons.columns else pd.DataFrame())
        if not ed.empty:
            exit_rows = ''.join('<tr><td>%s</td><td>%d</td></tr>' %
                                (html.escape(str(r['exit_reason'])), int(r['count']))
                                for _, r in ed.iterrows())
            exit_sections.append('<h3>%s</h3><div class="scroll"><table><thead><tr>'
                                 '<th>退出原因</th><th>数量</th></tr></thead><tbody>%s</tbody>'
                                 '</table></div>' % (WINDOW_LABELS[name], exit_rows))
    params = html.escape(json.dumps(run_meta['parameters'], ensure_ascii=False, indent=2, default=str))
    stopped = html.escape(run_meta.get('stopped_reason') or '—')
    return '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>六窗回测报告</title>
<style>body{font:14px system-ui;margin:0;background:#f4f6f8;color:#172033}.wrap{max-width:1400px;margin:auto;padding:24px}
h1,h2{margin:0 0 14px}.panel{background:white;border-radius:12px;padding:18px;margin:16px 0;box-shadow:0 2px 12px #0000000d}
.pill{display:inline-block;padding:4px 9px;border-radius:999px;white-space:nowrap}.ok{background:#dcfce7;color:#166534}.bad{background:#fee2e2;color:#991b1b}.warn{background:#fef3c7;color:#92400e}
table{border-collapse:collapse;width:100%%}th,td{padding:9px;border-bottom:1px solid #e5e7eb;text-align:right}th:first-child,td:first-child{text-align:left}
.panel> .scroll+ h3,.panel h3:not(:first-child){margin-top:28px}.scroll{overflow:auto}.baseline-table th:nth-child(2),.baseline-table td:nth-child(2){min-width:76px;white-space:nowrap;text-align:center}
.muted{color:#667085}svg{width:100%%;height:270px}pre{white-space:pre-wrap;background:#0f172a;color:#e2e8f0;padding:14px;border-radius:8px}</style></head>
<body><div class="wrap"><h1>币安网格六窗基线闸门报告</h1><p><span class="pill %s">%s</span>　实验：%s　基线：%s　模式：%s</p>
<div class="panel"><h2>闸门结论</h2><p>%s</p><p class="muted">规则：破网/爆仓一票否决；ret 与 MDD 容忍 0.30pp；正收益窗 Calmar 容忍 0.30；负收益窗不比较 Calmar。</p></div>
<div class="panel scroll"><h2>六窗与基线</h2><table class="baseline-table"><thead><tr><th>窗口</th><th>判定</th><th>基线ret</th><th>候选ret</th><th>Δret</th><th>基线Calmar</th><th>候选Calmar</th><th>基线MDD</th><th>候选MDD</th><th>ΔMDD</th><th>破网/爆仓</th><th>说明</th></tr></thead><tbody>%s</tbody></table></div>
<div class="panel"><h2>组合权益曲线（已完成窗口拼接展示）</h2>%s</div>
<div class="panel"><h2>分 offset 统计</h2>%s</div>
<div class="panel"><h2>退出原因</h2>%s</div>
<div class="panel"><h2>关键参数</h2><pre>%s</pre></div>
<div class="panel"><h2>产物</h2><p>window_summary.csv · all_grids.csv · offset_summary.csv · equity_curve.csv · exit_reasons.csv · parameters.json · command.txt · run.log</p></div>
</div></body></html>''' % (css_status, badge, html.escape(run_meta['name']), BASELINE_VERSION,
                              html.escape(mode_text),
                              stopped, ''.join(rows), _svg_curve(equity), ''.join(offset_sections),
                              ''.join(exit_sections), params)


def write_report(run_dir, run_meta, window_rows, grids_by_window):
    os.makedirs(run_dir, exist_ok=True)
    summary = pd.DataFrame(window_rows)
    summary.to_csv(os.path.join(run_dir, 'window_summary.csv'), index=False)
    all_grids, offsets, curves, exits = [], [], [], []
    for name, df in grids_by_window.items():
        d = df.copy(); d.insert(0, 'window', name); all_grids.append(d)
        od = build_offset_summary(df, BASELINE[name]['days']); od.insert(0, 'window', name); offsets.append(od)
        ec = build_equity_curve(df); ec.insert(0, 'window', name); curves.append(ec)
        er = df['exit_reason'].fillna('未知').value_counts().rename_axis('exit_reason').reset_index(name='count')
        er.insert(0, 'window', name); exits.append(er)
    all_df = pd.concat(all_grids, ignore_index=True) if all_grids else pd.DataFrame()
    off_df = pd.concat(offsets, ignore_index=True) if offsets else pd.DataFrame()
    curve_df = pd.concat(curves, ignore_index=True) if curves else pd.DataFrame()
    exit_df = pd.concat(exits, ignore_index=True) if exits else pd.DataFrame(columns=['window','exit_reason','count'])
    all_df.to_csv(os.path.join(run_dir, 'all_grids.csv'), index=False)
    off_df.to_csv(os.path.join(run_dir, 'offset_summary.csv'), index=False)
    curve_df.to_csv(os.path.join(run_dir, 'equity_curve.csv'), index=False)
    exit_df.to_csv(os.path.join(run_dir, 'exit_reasons.csv'), index=False)
    with open(os.path.join(run_dir, 'parameters.json'), 'w', encoding='utf-8') as f:
        json.dump(run_meta, f, ensure_ascii=False, indent=2, default=str)
    with open(os.path.join(run_dir, 'report.html'), 'w', encoding='utf-8') as f:
        f.write(render_html(run_meta, summary, off_df, curve_df, exit_df))
    return summary
