"""
回测数据预热程序（对应 支柱五 Cache + Prewarm）。

阶段（阶段间有依赖序，阶段内并发）：
  S3 元数据   : 拉合约规格(instruments)，冻结票池 + tickSz（与窗口无关，先做以确定票池）
  S0 共享日线 : 对票池每个币拉 [start-warmup, end] 的 1H K线，按天落 parquet 缓存
  S1 候选发现 : 按小时回放实盘选币，产出 candidates.csv；并派生 tick 下载清单 tick_manifest.csv
  S1m 持仓1m : 对选中币持仓周期(±1天)预取 1m K线(条件取数)，供 backtest_run --sim-bar 1m 纯读缓存
  S2 条件取数: 选中币持仓周期的资金费 + 标记价

三条工程约束：
  - 有界并发：S0 用 ThreadPoolExecutor(max_workers) + as_completed 流式回收
  - 幂等短路：每天先 cache.exists() 跳过；S1 跳过已回放的 run_time（可断点续跑）
  - 原子写 + 空哨兵：见 cache.py

用法（务必与实盘服务器同时区；本部署经 orderInfo.pkl 确认为 UTC+8，须 TZ=Asia/Shanghai）：
  TZ=Asia/Shanghai python prewarm.py --stage all
  TZ=Asia/Shanghai python prewarm.py --stage s0 --start "2024-01-01" --end "2024-02-01"
  TZ=Asia/Shanghai python prewarm.py --stage s1
"""
import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import bt_config as C
from cache import ParquetCache
import okx_history as H


def _to_ms(ts):
    """tz-naive pd.Timestamp 按 UTC 解释，返回毫秒。"""
    return int(pd.Timestamp(ts).value // 1_000_000)


# ===================== S3: 合约规格 / 票池 =====================
def stage_instruments(cache, proxies, refresh=False, log=print):
    """拉 SWAP 合约规格并冻结。返回 (universe[list], tick_size[dict])。"""
    cached = None if refresh else cache.read('instruments', 'SWAP', 'frozen')
    if cached is None:
        log('[S3] 拉取 OKX SWAP 合约规格...')
        inst = H.fetch_instruments('SWAP', proxies=proxies)
        if inst.empty:
            raise RuntimeError('[S3] 获取合约规格失败')
        cache.write('instruments', 'SWAP', 'frozen', inst)
        cached = inst
        log('[S3] 已冻结 %d 条合约规格' % len(cached))
    else:
        log('[S3] 复用已冻结的合约规格 %d 条' % len(cached))

    # 过滤口径与实盘 ccxt_fetch_ok_exchangeinfo 对齐：live + USDT 永续
    # 注意：instruments 只含"当前存活"的币 → 存在 survivorship bias（已退市币缺失），v1 已接受妥协。
    df = cached[(cached['state'] == 'live') & (cached['instId'].str.endswith('-USDT-SWAP'))]
    universe = sorted(df['instId'].tolist())
    tick_size = dict(zip(df['instId'], df['tickSz']))
    log('[S3] 票池(USDT永续, live): %d 个' % len(universe))
    return universe, tick_size


# ===================== S0: 共享 1H 日线 =====================
def _fetch_symbol_candles(cache, symbol, start_dt, end_dt, bar, proxies):
    """幂等地把某 symbol [start,end] 的 1H K线按天落缓存。返回 (symbol, warmed_days, status)。"""
    days = [d.strftime('%Y-%m-%d') for d in pd.date_range(start_dt.normalize(), end_dt.normalize(), freq='D')]
    missing = [d for d in days if not cache.exists(bar, symbol, d)]
    if not missing:
        return symbol, 0, 'skip'

    lo = pd.Timestamp(min(missing) + ' 00:00:00')
    hi = pd.Timestamp(max(missing) + ' 23:59:59')
    df = H.fetch_candles_range(symbol, _to_ms(lo), _to_ms(hi), bar=bar, proxies=proxies)

    warmed = 0
    if df is None or df.empty:
        # 整段无数据：对所有缺失天落空哨兵，避免反复重取
        for d in missing:
            cache.write_empty(bar, symbol, d, H.CANDLE_COLS)
        return symbol, len(missing), 'empty'

    df = df.copy()
    df['day'] = df['candle_begin_time'].dt.strftime('%Y-%m-%d')
    by_day = {d: g[H.CANDLE_COLS] for d, g in df.groupby('day')}
    for d in missing:
        if d in by_day and not by_day[d].empty:
            cache.write(bar, symbol, d, by_day[d].reset_index(drop=True))
            warmed += 1
        else:
            cache.write_empty(bar, symbol, d, H.CANDLE_COLS)  # 这天确实没 bar
    return symbol, warmed, 'fetched'


def stage_candles(cache, universe, start_dt, end_dt, bar, workers, proxies, log=print):
    log('[S0] 共享日线: %d 个币, 窗口 %s ~ %s, 并发 %d' %
        (len(universe), start_dt, end_dt, workers))
    t0 = time.time()
    warmed = skipped = empty = failed = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_fetch_symbol_candles, cache, s, start_dt, end_dt, bar, proxies): s
                for s in universe}
        done = 0
        for fut in as_completed(futs):
            s = futs[fut]
            done += 1
            try:
                _, w, status = fut.result()
                if status == 'skip':
                    skipped += 1
                elif status == 'empty':
                    empty += 1
                else:
                    warmed += 1
            except Exception as e:  # noqa  单币失败只告警+计数，绝不中断整次预热
                failed += 1
                log('[S0][WARN] %s 取数失败: %s' % (s, str(e)))
            if done % 50 == 0:
                log('[S0] 进度 %d/%d' % (done, len(universe)))
    log('[S0] 完成: warmed=%d skipped=%d empty=%d failed=%d 耗时=%.1fs'
        % (warmed, skipped, empty, failed, time.time() - t0))


# ===================== S1: 候选发现 + tick 清单 =====================
def _done_run_times(candidates_csv):
    if not os.path.exists(candidates_csv):
        return set()
    try:
        df = pd.read_csv(candidates_csv)
        return set(df['run_time'].astype(str).unique())
    except Exception:
        return set()


def stage_select(cache, universe, start_dt, end_dt, strategy_config, factors,
                 utc_offset, manifest_dir, log=print):
    import selection_replay as SR  # 延迟导入：触发 account_0 选币管线 import

    os.makedirs(manifest_dir, exist_ok=True)
    candidates_csv = os.path.join(manifest_dir, 'candidates.csv')

    # 枚举小时游标（实盘每小时触发一个 offset）
    all_run_times = [pd.Timestamp(t) for t in pd.date_range(start_dt, end_dt, freq='1H')]
    done = _done_run_times(candidates_csv)
    run_times = [t for t in all_run_times if str(t) not in done]
    log('[S1] run_time 总数 %d, 已完成 %d, 待回放 %d' % (len(all_run_times), len(done), len(run_times)))
    if not run_times:
        log('[S1] 无待回放 run_time（已是最新）')
    else:
        new_file = not os.path.exists(candidates_csv)
        fh = open(candidates_csv, 'a', encoding='utf-8-sig')
        if new_file:
            fh.write('run_time,offset,symbol,rank\n')

        # 即便某 run_time 没选中任何币，也要标记为已完成 → 写一个空标记行避免重复回放
        seen_rt = set()

        def on_select(run_time, offset, row):
            seen_rt.add(str(run_time))
            fh.write('%s,%s,%s,%s\n' % (run_time, offset, row['symbol'], row.get('rank')))
            fh.flush()

        try:
            SR.replay_selection(cache, universe, run_times, strategy_config, factors,
                                utc_offset, on_select, log=log)
        finally:
            # 标记未选中任何币的 run_time（写 symbol 为空），保证幂等续跑不重复
            for t in run_times:
                if str(t) not in seen_rt:
                    fh.write('%s,%s,,\n' % (t, SR.compute_offset(pd.Timestamp(t), strategy_config['period'], utc_offset)))
            fh.flush()
            fh.close()

    # 派生 tick 下载清单：选中币的持仓周期 [run_time, run_time+period] 覆盖到的每一天
    _build_tick_manifest(candidates_csv, strategy_config['period'], manifest_dir, log=log)


def _build_tick_manifest(candidates_csv, period, manifest_dir, log=print):
    if not os.path.exists(candidates_csv):
        log('[S1] 无 candidates.csv，跳过 tick 清单')
        return
    df = pd.read_csv(candidates_csv)
    df = df[df['symbol'].notna() & (df['symbol'].astype(str) != '')]
    if df.empty:
        log('[S1] 候选为空，tick 清单为空')
        return
    period_td = pd.to_timedelta(period)
    rows = set()
    for _, r in df.iterrows():
        rt = pd.Timestamp(r['run_time'])
        sym = r['symbol']
        for d in pd.date_range(rt.normalize(), (rt + period_td).normalize(), freq='D'):
            rows.add((sym, d.strftime('%Y-%m-%d')))
    out = pd.DataFrame(sorted(rows), columns=['symbol', 'day'])
    path = os.path.join(manifest_dir, 'tick_manifest.csv')
    out.to_csv(path, index=False)
    log('[S1] tick 下载清单: %d 个 (symbol, day) -> %s' % (len(out), path))
    log('[S1]   候选 (symbol,run_time) 条数=%d, 去重币数=%d' % (len(df), df['symbol'].nunique()))


# ===================== S2: 条件取数（资金费 + 标记价）=====================
# 只对 S1 选中币的持仓周期取数（设计文档「Stage 2 条件取数」），per-day 缓存、幂等。
MARK_COLS = ['ts', 'symbol', 'open', 'high', 'low', 'close']
FUNDING_COLS = ['ts', 'symbol', 'fundingRate', 'realizedRate']


def _fetch_symbol_range(cache, namespace, symbol, start_dt, end_dt, fetch_fn, columns, proxies):
    """通用：把某 symbol [start,end] 的数据按天落 namespace 缓存（幂等 + 空哨兵）。"""
    days = [d.strftime('%Y-%m-%d') for d in pd.date_range(start_dt.normalize(), end_dt.normalize(), freq='D')]
    missing = [d for d in days if not cache.exists(namespace, symbol, d)]
    if not missing:
        return symbol, 0, 'skip'
    lo = pd.Timestamp(min(missing) + ' 00:00:00')
    hi = pd.Timestamp(max(missing) + ' 23:59:59')
    df = fetch_fn(symbol, _to_ms(lo), _to_ms(hi), proxies)
    if df is None or df.empty:
        for d in missing:
            cache.write_empty(namespace, symbol, d, columns)
        return symbol, len(missing), 'empty'
    df = df.copy()
    df['day'] = pd.to_datetime(df['ts'], unit='ms').dt.strftime('%Y-%m-%d')
    keep = [c for c in columns if c in df.columns]
    by_day = {d: g[keep] for d, g in df.groupby('day')}
    warmed = 0
    for d in missing:
        if d in by_day and not by_day[d].empty:
            cache.write(namespace, symbol, d, by_day[d].reset_index(drop=True))
            warmed += 1
        else:
            cache.write_empty(namespace, symbol, d, columns)
    return symbol, warmed, 'fetched'


def _symbol_holding_ranges(candidates_csv, period, buffer_days=1):
    """从候选派生每个币需要取数的 [min_start, max_end]（含 ±buffer 天，覆盖时区偏移）。"""
    if not os.path.exists(candidates_csv):
        return {}
    df = pd.read_csv(candidates_csv)
    df = df[df['symbol'].notna() & (df['symbol'].astype(str) != '')]
    if df.empty:
        return {}
    period_td = pd.to_timedelta(period)
    buf = pd.Timedelta(days=buffer_days)
    ranges = {}
    for _, r in df.iterrows():
        rt = pd.Timestamp(r['run_time'])
        s = r['symbol']
        start = rt - buf
        end = rt + period_td + buf
        if s not in ranges:
            ranges[s] = [start, end]
        else:
            ranges[s][0] = min(ranges[s][0], start)
            ranges[s][1] = max(ranges[s][1], end)
    return ranges


def stage_funding_mark(cache, manifest_dir, period, bar, workers, proxies, log=print):
    candidates_csv = os.path.join(manifest_dir, 'candidates.csv')
    ranges = _symbol_holding_ranges(candidates_csv, period)
    if not ranges:
        log('[S2] 无候选，跳过资金费/标记价取数'); return
    log('[S2] 条件取数: %d 个选中币的持仓周期（资金费 + 标记价 %s），并发 %d' % (len(ranges), bar, workers))
    t0 = time.time()

    mark_fn = lambda sym, s, e, px: H.fetch_mark_candles_range(sym, s, e, bar=bar, proxies=px)
    fund_fn = lambda sym, s, e, px: H.fetch_funding_rate_range(sym, s, e, proxies=px)

    jobs = []  # (namespace, symbol, start, end, fetch_fn, cols)
    for s, (st, en) in ranges.items():
        jobs.append(('mark', s, st, en, mark_fn, MARK_COLS))
        jobs.append(('funding', s, st, en, fund_fn, FUNDING_COLS))

    counts = {'fetched': 0, 'skip': 0, 'empty': 0, 'failed': 0}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_fetch_symbol_range, cache, ns, s, st, en, fn, cols, proxies): (ns, s)
                for (ns, s, st, en, fn, cols) in jobs}
        done = 0
        for fut in as_completed(futs):
            ns, s = futs[fut]
            done += 1
            try:
                _, _w, status = fut.result()
                counts[status] += 1
            except Exception as e:  # noqa
                counts['failed'] += 1
                log('[S2][WARN] %s %s 取数失败: %s' % (ns, s, str(e)))
            if done % 100 == 0:
                log('[S2] 进度 %d/%d' % (done, len(jobs)))
    log('[S2] 完成: fetched=%d skip=%d empty=%d failed=%d 耗时=%.1fs'
        % (counts['fetched'], counts['skip'], counts['empty'], counts['failed'], time.time() - t0))


# ===================== S1m: 选中币持仓期 1m 预取（条件取数）=====================
def stage_candles_1m(cache, manifest_dir, period, workers, proxies, log=print):
    """对 S1 选中币的持仓周期(含±1天缓冲)并发预取 1m K线，落 namespace='1m' 缓存。
    与 backtest_run.load_1m_holding 的窗口一致，预热后回测纯读缓存、不打网络。"""
    candidates_csv = os.path.join(manifest_dir, 'candidates.csv')
    ranges = _symbol_holding_ranges(candidates_csv, period)  # {sym: [min_start, max_end]}
    if not ranges:
        log('[S1m] 无候选，跳过 1m 预取'); return
    log('[S1m] 持仓期 1m 预取: %d 个选中币, 并发 %d' % (len(ranges), workers))
    t0 = time.time()
    counts = {'fetched': 0, 'skip': 0, 'empty': 0, 'failed': 0}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_fetch_symbol_candles, cache, s, st, en, '1m', proxies): s
                for s, (st, en) in ranges.items()}
        done = 0
        for fut in as_completed(futs):
            s = futs[fut]
            done += 1
            try:
                _, _w, status = fut.result()
                counts[status] += 1
            except Exception as e:  # noqa
                counts['failed'] += 1
                log('[S1m][WARN] %s 取数失败: %s' % (s, str(e)))
            if done % 50 == 0:
                log('[S1m] 进度 %d/%d' % (done, len(ranges)))
    log('[S1m] 完成: fetched=%d skip=%d empty=%d failed=%d 耗时=%.1fs'
        % (counts['fetched'], counts['skip'], counts['empty'], counts['failed'], time.time() - t0))


# ===================== main =====================
def _load_strategy_config():
    """从 account_0/config.py 取 strategy_config（period/weight_list/choose_symbols/max_candle_num）。"""
    acc = os.path.join(os.path.dirname(_HERE), 'account_0')
    for p in (acc, os.path.join(acc, 'utils'), os.path.join(acc, 'api')):
        if p not in sys.path:
            sys.path.insert(0, p)
    from config import strategy_config
    return strategy_config


def main():
    ap = argparse.ArgumentParser(description='OKX 网格回测数据预热 (S0/S1/S2/S3)')
    ap.add_argument('--stage', choices=['all', 's0', 's1', 's1m', 's2', 's3'], default='all')
    ap.add_argument('--start', default=C.WINDOW_START)
    ap.add_argument('--end', default=C.WINDOW_END)
    ap.add_argument('--bar', default=C.BAR)
    ap.add_argument('--cache-dir', default=C.CACHE_DIR)
    ap.add_argument('--manifest-dir', default=C.MANIFEST_DIR)
    ap.add_argument('--workers', type=int, default=C.S0_WORKERS)
    ap.add_argument('--refresh-instruments', action='store_true')
    args = ap.parse_args()

    cache = ParquetCache(args.cache_dir)
    proxies = C.PROXIES
    strategy_config = _load_strategy_config()
    period = strategy_config['period']

    window_start = pd.Timestamp(args.start)
    window_end = pd.Timestamp(args.end)
    # S0 需要往前暖机
    s0_start = window_start - pd.Timedelta(days=C.WARMUP_DAYS)

    print('=' * 60)
    print('预热配置: stage=%s window=[%s, %s] bar=%s cache=%s'
          % (args.stage, window_start, window_end, args.bar, args.cache_dir))
    print('TZ=%s （务必与实盘服务器一致，否则选币 parity 漂移）' % os.environ.get('TZ', '<未设置>'))
    print('=' * 60)

    # S3 先做：确定票池
    universe, _tick = stage_instruments(cache, proxies, refresh=args.refresh_instruments)

    if args.stage in ('all', 's3'):
        if args.stage == 's3':
            return

    if args.stage in ('all', 's0'):
        stage_candles(cache, universe, s0_start, window_end, args.bar, args.workers, proxies)

    if args.stage in ('all', 's1'):
        stage_select(cache, universe, window_start, window_end, strategy_config,
                     C.FACTORS, C.UTC_OFFSET, args.manifest_dir)

    if args.stage in ('all', 's1m'):
        # 依赖 S1 候选；持仓期 1m 预取，供 backtest_run --sim-bar 1m 纯读缓存
        stage_candles_1m(cache, args.manifest_dir, period, args.workers, proxies)

    if args.stage in ('all', 's2'):
        # S2 依赖 S1 的候选；单独跑 s2 时复用已有 candidates.csv
        stage_funding_mark(cache, args.manifest_dir, period, args.bar, args.workers, proxies)

    print('预热结束。')


if __name__ == '__main__':
    main()
