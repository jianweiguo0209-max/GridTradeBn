"""一条命令运行六窗 s030 基线闸门并生成 CSV/HTML 报告。

用法：
  TZ=Asia/Shanghai BT_WORKERS=3 .venv/bin/python -u -m scripts.six_window_backtest my-test
"""
import argparse
import contextlib
import copy
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime

import pandas as pd

from gridtrade.backtest import sweep as SW
from gridtrade.backtest import vision as V
from gridtrade.backtest._resource_guard import safe_workers
from gridtrade.backtest.cache import ParquetCache
from gridtrade.backtest.four_window_report import (BASELINE, BASELINE_VERSION, GATE,
                                                   WINDOW_ORDER, evaluate_window,
                                                   write_report)
from gridtrade.config import DEFAULT_STRATEGY_CONFIG, DEFAULT_STOP_CFG, DEFAULT_TIER_POLICY
from gridtrade.core.tier_policy import effective_blacklist


def _slug(value):
    s = re.sub(r'[^A-Za-z0-9._-]+', '-', str(value).strip()).strip('-._')
    if not s or s in ('.', '..'):
        raise ValueError('名称不能为空，且只能包含可转换为安全目录名的字符')
    return s[:120]


def factor_slug(strategy):
    parts = []
    weights = list(strategy['weight_list'])
    for i, (name, ascending) in enumerate(strategy['factors'].items()):
        weight = weights[i] if i < len(weights) else 1
        parts.append('%s-%s-w%s' % (name, 'asc' if ascending else 'desc', ('%g' % weight)))
    return _slug('__'.join(parts))


class Tee:
    def __init__(self, *streams): self.streams = streams
    def write(self, value):
        for stream in self.streams:
            stream.write(value); stream.flush()
    def flush(self):
        for stream in self.streams: stream.flush()


def heartbeat(label, fn, every=30):
    done = threading.Event()
    def beat():
        elapsed = 0
        while not done.wait(every):
            elapsed += every
            print('[心跳] %s 仍在运行，已用时约 %ds' % (label, elapsed), flush=True)
    thread = threading.Thread(target=beat, daemon=True); thread.start()
    try:
        return fn()
    finally:
        done.set(); thread.join(timeout=1)


def _git_meta():
    def cmd(*args):
        try:
            return subprocess.check_output(args, stderr=subprocess.DEVNULL).decode().strip()
        except Exception:
            return 'unknown'
    return {'commit': cmd('git', 'rev-parse', 'HEAD'),
            'branch': cmd('git', 'rev-parse', '--abbrev-ref', 'HEAD'),
            'dirty': bool(cmd('git', 'status', '--porcelain') not in ('', 'unknown'))}


def _parameters(workers, mode):
    return {'strategy': copy.deepcopy(DEFAULT_STRATEGY_CONFIG),
            'stop_loss': copy.deepcopy(DEFAULT_STOP_CFG),
            'tier_policy': {'tier0': list(DEFAULT_TIER_POLICY.tier0),
                            'tier1': list(DEFAULT_TIER_POLICY.tier1),
                            'tier2_cap': DEFAULT_TIER_POLICY.tier2_cap},
            'sweep_baseline': SW.live_baseline(), 'workers': workers,
            'timezone': os.environ.get('TZ'), 'universe_mode': 'cached_archive_wide',
            'baseline_version': BASELINE_VERSION, 'gate': GATE,
            'run_mode': mode}


def run(name, output_root='data/result', workers=1, heartbeat_sec=30, mode='traditional'):
    if os.environ.get('TZ') != 'Asia/Shanghai':
        raise RuntimeError('必须设置 TZ=Asia/Shanghai，避免选币 offset 时区漂移')
    if mode not in ('traditional', 'full'):
        raise ValueError('mode 只支持 traditional/full，当前=%r' % mode)
    workers = safe_workers(workers)
    combo = factor_slug(DEFAULT_STRATEGY_CONFIG)
    run_dir = os.path.abspath(os.path.join(output_root, combo, _slug(name)))
    if os.path.exists(run_dir) and os.listdir(run_dir):
        raise RuntimeError('结果目录已存在且非空，请换一个 --name：%s' % run_dir)
    os.makedirs(run_dir, exist_ok=True)
    command = ' '.join(sys.argv)
    with open(os.path.join(run_dir, 'command.txt'), 'w', encoding='utf-8') as f:
        f.write(command + '\n')
    log_file = open(os.path.join(run_dir, 'run.log'), 'a', encoding='utf-8')
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = Tee(old_out, log_file), Tee(old_err, log_file)
    rows, grids, failed_windows = [], {}, []
    meta = {'name': name, 'factor_combo': combo, 'started_at': datetime.now().isoformat(),
            'status': 'RUNNING', 'stopped_reason': None, 'git': _git_meta(),
            'mode': mode, 'parameters': _parameters(workers, mode), 'command': command}
    try:
        print('[开始] 六窗回测 name=%s' % name, flush=True)
        print('[目录] %s' % run_dir, flush=True)
        print('[参数] factors=%s weights=%s baseline=%s workers=%d mode=%s' %
              (DEFAULT_STRATEGY_CONFIG['factors'], DEFAULT_STRATEGY_CONFIG['weight_list'],
               BASELINE_VERSION, workers, mode), flush=True)
        print('[模式] %s' % ('传统模式：前序窗失败立即停止'
                           if mode == 'traditional' else '全测模式：失败仍继续完成六窗'), flush=True)
        cache = ParquetCache(V.default_cache_root())
        blacklist = set(effective_blacklist((), DEFAULT_TIER_POLICY))
        universe = sorted(set(cache.list_symbols('1h')) - blacklist)
        if not universe:
            raise RuntimeError('本地 1h 缓存票池为空；请先按回测文档预热数据')
        print('[票池] 本地归档缓存 %d 币（与冻结 s030 宽池口径一致）' % len(universe), flush=True)
        pv_cache = {}
        for index, window in enumerate(WINDOW_ORDER, 1):
            b = BASELINE[window]
            print('\n[窗口 %d/6] %s %s ~ %s：选币/装配开始' %
                  (index, window, b['start'], b['end']), flush=True)
            wd = heartbeat('%s 选币与数据装配' % window,
                           lambda w=window, x=b: SW.preload_window(
                               cache, universe, w, x['start'], x['end'], workers=workers),
                           heartbeat_sec)
            print('[窗口 %d/6] %s：仿真开始 grids=%d' % (index, window, len(wd.raw)), flush=True)
            df = heartbeat('%s 网格仿真' % window,
                           lambda d=wd: SW.run_arm(d, SW.Arm('manual', name, {}),
                                                   pv_cache, workers=workers),
                           heartbeat_sec)
            grids[window] = df
            window_dir = os.path.join(run_dir, window); os.makedirs(window_dir, exist_ok=True)
            df.to_csv(os.path.join(window_dir, 'grids.csv'), index=False)
            m = SW.metrics(df, wd.days)
            passed, reasons = evaluate_window(window, m)
            row = {'window': window, 'start': b['start'], 'end': b['end'],
                   'status': 'PASSED' if passed else 'FAILED', 'passed': passed,
                   'failure_reasons': '；'.join(reasons), **m,
                   'baseline_ret': b['ret'], 'baseline_mdd': b['mdd'],
                   'baseline_calmar': b['calmar'],
                   'delta_ret_pp': (m['ret'] - b['ret']) * 100,
                   'delta_mdd_pp': (m['mdd'] - b['mdd']) * 100,
                   'delta_calmar': m['calmar'] - b['calmar']}
            rows.append(row)
            print('[窗口 %d/6] %s：ret=%+.2f%% mdd=%.2f%% calmar=%.2f '
                  '破网=%d 爆仓=%d → %s' %
                  (index, window, m['ret'] * 100, m['mdd'] * 100, m['calmar'],
                   m['n_broke'], m['n_blown'], '通过' if passed else '失败'), flush=True)
            if not passed:
                failed_windows.append((window, reasons))
                if mode == 'traditional':
                    meta['status'] = 'FAILED'
                    meta['stopped_reason'] = '%s 未通过：%s；后续窗口未执行' % (window, '；'.join(reasons))
                    print('[中断] %s' % meta['stopped_reason'], flush=True)
                    break
                print('[继续] %s 未通过，但当前为全测模式，继续下一个窗口' % window, flush=True)
        else:
            if failed_windows:
                meta['status'] = 'FAILED'
                meta['stopped_reason'] = ('全测模式已完成六窗；未通过窗口：' + '；'.join(
                    '%s（%s）' % (w, '；'.join(rs)) for w, rs in failed_windows))
            else:
                meta['status'] = 'PASSED'; meta['stopped_reason'] = '六个窗口全部通过'
        meta['finished_at'] = datetime.now().isoformat()
        write_report(run_dir, meta, rows, grids)
        print('\n[完成] status=%s' % meta['status'], flush=True)
        print('[报告] %s' % os.path.join(run_dir, 'report.html'), flush=True)
        return 0 if meta['status'] == 'PASSED' else 2
    except BaseException as exc:
        meta['status'] = 'ERROR'; meta['stopped_reason'] = repr(exc)
        meta['finished_at'] = datetime.now().isoformat()
        write_report(run_dir, meta, rows, grids)
        print('[错误] %r' % exc, flush=True)
        print('[报告] %s' % os.path.join(run_dir, 'report.html'), flush=True)
        if isinstance(exc, KeyboardInterrupt):
            return 130
        raise
    finally:
        sys.stdout, sys.stderr = old_out, old_err
        log_file.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description='六窗 s030 基线闸门 + CSV/HTML 报告')
    ap.add_argument('name', help='本次实验自定义名称（输出目录最后一级）')
    ap.add_argument('--output-root', default='data/result')
    ap.add_argument('--workers', type=int, default=int(os.environ.get('BT_WORKERS', '1')))
    ap.add_argument('--heartbeat-sec', type=int, default=30)
    ap.add_argument('--mode', choices=('traditional', 'full'), default='traditional',
                    help='traditional=前序窗失败立即停止（默认）；full=不论失败均跑完六窗')
    args = ap.parse_args(argv)
    return run(args.name, args.output_root, args.workers, args.heartbeat_sec, args.mode)


if __name__ == '__main__':
    from gridtrade.backtest.envfile import load_env_file
    load_env_file()
    sys.exit(main())
