"""Reservoir(Hydromancer) S3 归档 → ParquetCache 的 1m 装载层。

背景：Hyperliquid 公共 candle 端点只留最近 ~5000 根 1m（~3.5 天），无法回测三个月 1m。
Reservoir 提供全历史 **1 秒 OHLCV**（parquet，requester-pays，无订阅费）：
  s3://hydromancer-reservoir/by_dex/hyperliquid/candles/1s/date=YYYY-MM-DD/candles.parquet
  region ap-northeast-1；每日一个文件含当天全部 HL 币种。
本模块把 1s 重采样成 1m，映射到本项目 CANDLE_COLS，写入 cache 的 '1m' 命名空间——
之后 run_backtest(..., sim_timeframe='1m') 完全不用改即可跑真三个月 1m。

需要调用方已配置 AWS 凭证（requester-pays 必须已鉴权；egress 由调用方账户承担）。
"""
import os
import subprocess
import tempfile

import pandas as pd

from gridtrade.exchanges.base import CANDLE_COLS

S3_BUCKET = 's3://hydromancer-reservoir'
MAIN_DEX = 'hyperliquid'
S3_KEY_FMT = 'by_dex/%s/candles/1s/date=%s/candles.parquet'   # % (dex, day)
NAMESPACE = '1m'
_RULES = {'1m': '1min', '1h': '1H'}   # cache 命名空间 → pandas resample 规则

# Reservoir candles 列（见 docs.hydromancer.xyz schema）：
#   coin, dex, asset_class, base_symbol, quote_symbol, timestamp(ms,UTC),
#   open, high, low, close, volume, volume_quote, trade_count


def _days(start_ms, end_ms):
    s = pd.to_datetime(start_ms, unit='ms').normalize()
    e = pd.to_datetime(end_ms, unit='ms').normalize()
    return [d.strftime('%Y-%m-%d') for d in pd.date_range(s, e, freq='D')]


def candles_1s_resample(df, symbol_map, rule):
    """纯函数：Reservoir 1s candles(df) → {symbol: rule 周期 CANDLE_COLS df}。
    rule: pandas resample 规则（'1min'/'1H'）。symbol_map: {reservoir_coin: canonical_symbol}。
    只处理 symbol_map 里的币；bar-begin 口径（label/closed=left）。"""
    out = {}
    if df is None or df.empty:
        return out
    ts = pd.to_datetime(df['timestamp'], utc=True).dt.tz_localize(None)  # tz-naive UTC，与 cache 同口径
    df = df.assign(candle_begin_time=ts)
    # Reservoir 的 OHLCV 列是 decimal(20,10)→pandas object(Decimal)；重采样前先转 float，
    # 否则 resample 的 max/min/sum 在 object 列上不可靠/极慢。
    for c in ('open', 'high', 'low', 'close', 'volume', 'volume_quote'):
        df[c] = df[c].astype(float)
    for coin, sym in symbol_map.items():
        sub = df[df['coin'] == coin]
        if sub.empty:
            continue
        g = (sub.set_index('candle_begin_time').sort_index()
             .resample(rule, label='left', closed='left')
             .agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last',
                   'volume': 'sum', 'volume_quote': 'sum'}))
        g = g.dropna(subset=['open']).reset_index()
        if g.empty:
            continue
        g['symbol'] = sym
        g['vol'] = g['volume'].astype(float)
        g['volCcy'] = g['volume_quote'].astype(float)
        g['quote_volume'] = g['volume_quote'].astype(float)
        for c in ('open', 'high', 'low', 'close'):
            g[c] = g[c].astype(float)
        out[sym] = g[CANDLE_COLS].reset_index(drop=True)
    return out


def candles_1s_to_1m(df, symbol_map):
    """向后兼容薄包装：1s→1m。"""
    return candles_1s_resample(df, symbol_map, '1min')


def validate_1m_cell(m_df, h_df, *, range_tol=0.05):
    """判定 (币,天) 的缓存 1m 是否可信（spec 2026-07-07-1m-cache-integrity）。
    返回 (ok, reason)，reason ∈ {'ok','no_1h_ref','range_mismatch','hour_gap'}。

    1h 缺/空 → 无基准，视合法（真·不成交或无参照）；
    振幅：|1m 日高低幅 − 1h 日高低幅|/入场价 > range_tol → range_mismatch（TRUMP 型）；
    完整性：1h 有 bar 的每个整点小时，1m 该小时窗零 bar → hour_gap（GMX 残缺型）。
    合法稀疏（分钟级缺但每小时都有 bar）判 ok。"""
    if h_df is None or len(h_df) == 0:
        return True, 'no_1h_ref'
    entry = float(h_df['close'].iloc[0])
    if entry <= 0:
        return True, 'no_1h_ref'
    if m_df is None or len(m_df) == 0:
        return False, 'hour_gap'          # 1h 有数据但 1m 空 = 该交易的天缺 1m
    h_hi = float(h_df['high'].max()); h_lo = float(h_df['low'].min())
    m_hi = float(m_df['high'].max()); m_lo = float(m_df['low'].min())
    if abs((m_hi - m_lo) - (h_hi - h_lo)) / entry > range_tol:
        return False, 'range_mismatch'
    m_hours = set(pd.to_datetime(m_df['candle_begin_time']).dt.floor('H'))
    for ht in pd.to_datetime(h_df['candle_begin_time']).dt.floor('H'):
        if ht not in m_hours:
            return False, 'hour_gap'
    return True, 'ok'


def reservoir_coin(symbol, dex=None):
    """canonical → Reservoir coin 名。主 dex 'BTC/USDC:USDC'→'BTC'；
    builder（如 dex='xyz'）'XYZ-TSLA/USDC:USDC'→'xyz:TSLA'（档案实测命名，
    2026-07-12 探针：by_dex/xyz 文件 coin 列 = 'xyz:XYZ100' 等）。"""
    base = symbol.split('/')[0]
    if not dex or dex == MAIN_DEX:
        return base
    pre = dex.upper() + '-'
    return '%s:%s' % (dex, base[len(pre):] if base.startswith(pre) else base)


def _dex_groups(universe, dex_map):
    """{dex: {reservoir_coin: canonical}}。dex_map: {canonical: dex}（builder 币才需要，
    缺省=主 dex）。哨兵语义按组独立：某 dex 日文件失败只影响该组（根治跨 dex 假哨兵）。"""
    groups = {}
    for s in universe:
        d = (dex_map or {}).get(s) or MAIN_DEX
        groups.setdefault(d, {})[reservoir_coin(s, None if d == MAIN_DEX else d)] = s
    return groups


def _s3_cp(day, dest, *, dex=MAIN_DEX, log=print):
    """aws s3 cp（requester-pays）。返回 True=成功；文件不存在/失败=False。"""
    src = '%s/%s' % (S3_BUCKET, S3_KEY_FMT % (dex, day))
    r = subprocess.run(['aws', 's3', 'cp', src, dest, '--request-payer', 'requester'],
                       capture_output=True, text=True)
    if r.returncode != 0:
        log('[reservoir] %s(%s) 跳过：%s' % (day, dex, (r.stderr or '').strip().splitlines()[-1:]))
        return False
    return True


def _day_1m_all_valid(cache, universe, day):
    """该天所有币的缓存 1m 是否都过完整性校验（配合 warm 跳过判定）。
    任一坏 → False（该天不跳过、重下修复）。"""
    for s in universe:
        ok, _ = validate_1m_cell(cache.read('1m', s, day), cache.read('1h', s, day))
        if not ok:
            return False
    return True


def warm_reservoir_ohlcv(cache, universe, start_ms, end_ms, *, timeframes=('1h', '1m'),
                         workdir=None, log=print, dex_map=None):
    """把 [start,end] 每个 UTC 天的 1s 拉下→按 timeframes 重采样→写各命名空间（一次下载多周期同写）。

    幂等：**所有** timeframe 的整天全命中才跳过；只差其一也重下 day 文件补齐（覆盖写同值无害）。
    只缓存**完整**的天：当天(UTC)未过完、或该天在 S3 尚未发布/拉取报错 → 不写任何文件（含空哨兵），
    计入 retry_later，下次重取。只有「日文件已成功下载、但某币当天确无成交」才落该币空哨兵（真空）。

    dex_map（可选，spec 2026-07-12-builder-dex）：{canonical_symbol: dex 名}——builder 币按
    by_dex/{dex}/ 独立日文件下载（coin 命名 'xyz:TSLA'），哨兵/失败按组隔离；None=全主 dex，
    路径与行为逐字节不变。"""
    groups = _dex_groups(universe, dex_map)             # {dex: {coin: canonical}}
    now_ms = int(pd.Timestamp.utcnow().value // 1_000_000)
    days = _days(start_ms, end_ms)
    tmpdir = workdir or tempfile.mkdtemp(prefix='reservoir_')
    os.makedirs(tmpdir, exist_ok=True)
    stat = {tf: {'days': 0, 'rows': 0} for tf in timeframes}
    stat['skipped_cached'] = 0
    stat['retry_later'] = 0
    for day in days:
        # 当天(UTC)未过完 → 无完整日文件；不缓存、不落哨兵，下次重取
        day_end_ms = int((pd.Timestamp(day) + pd.Timedelta(days=1)).value // 1_000_000)
        if day_end_ms > now_ms:
            stat['retry_later'] += 1
            continue
        # 组粒度幂等：某 dex 组全命中即跳过该组（哨兵/失败互不串组——根治跨 dex 假哨兵）
        pending = {}
        for dex, cmap in groups.items():
            syms = list(cmap.values())
            if (all(cache.exists(tf, s, day) for tf in timeframes for s in syms)
                    and _day_1m_all_valid(cache, syms, day)):   # 自愈：坏 1m 不跳过、重下
                continue
            pending[dex] = cmap
        if not pending:
            stat['skipped_cached'] += 1
            continue
        wrote_any = False
        for dex in sorted(pending):
            cmap = pending[dex]
            dest = os.path.join(tmpdir, '%s_%s.parquet' % (dex, day))
            if not _s3_cp(day, dest, dex=dex, log=log):
                # 该 dex 拉取失败(404 未发布 / dex 上线前) → 该组不写任何文件，下次重取
                stat['retry_later'] += 1
                continue
            raw = pd.read_parquet(dest)
            os.remove(dest)
            for tf in timeframes:
                per_sym = candles_1s_resample(raw, cmap, _RULES[tf])
                for s in cmap.values():
                    df = per_sym.get(s)
                    if df is None or df.empty:
                        cache.write_empty(tf, s, day, CANDLE_COLS)  # 该组文件已下、该币当天确无成交
                    else:
                        cache.write(tf, s, day, df)
                        stat[tf]['rows'] += int(len(df))
            wrote_any = True
        if wrote_any:
            for tf in timeframes:
                stat[tf]['days'] += 1
        done = stat[timeframes[0]]['days']
        if done and done % 10 == 0:
            log('[reservoir] %d days done (%s)' % (
                done, ', '.join('%s rows=%d' % (tf, stat[tf]['rows']) for tf in timeframes)))
    return stat


def warm_reservoir_1m(cache, universe, start_ms, end_ms, *, workdir=None, log=print, dex_map=None):
    """向后兼容薄包装：只做 1m，返回旧格式 {'days','rows','skipped_cached','retry_later'}。"""
    st = warm_reservoir_ohlcv(cache, universe, start_ms, end_ms,
                              timeframes=('1m',), workdir=workdir, log=log, dex_map=dex_map)
    return {'days': st['1m']['days'], 'rows': st['1m']['rows'],
            'skipped_cached': st['skipped_cached'], 'retry_later': st['retry_later']}
