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
S3_KEY_FMT = 'by_dex/hyperliquid/candles/1s/date=%s/candles.parquet'
NAMESPACE = '1m'

# Reservoir candles 列（见 docs.hydromancer.xyz schema）：
#   coin, dex, asset_class, base_symbol, quote_symbol, timestamp(ms,UTC),
#   open, high, low, close, volume, volume_quote, trade_count


def _days(start_ms, end_ms):
    s = pd.to_datetime(start_ms, unit='ms').normalize()
    e = pd.to_datetime(end_ms, unit='ms').normalize()
    return [d.strftime('%Y-%m-%d') for d in pd.date_range(s, e, freq='D')]


def candles_1s_to_1m(df, symbol_map):
    """纯函数：Reservoir 1s candles(df) → {symbol: 1m CANDLE_COLS df}。
    symbol_map: {reservoir_coin: canonical_symbol}，如 {'BTC': 'BTC/USDC:USDC'}。
    只处理 symbol_map 里的币；1s→1m 用 bar-begin 口径（label/closed=left）。"""
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
             .resample('1min', label='left', closed='left')
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


def _s3_cp(day, dest, *, log=print):
    """aws s3 cp（requester-pays）。返回 True=成功；文件不存在/失败=False。"""
    src = '%s/%s' % (S3_BUCKET, S3_KEY_FMT % day)
    r = subprocess.run(['aws', 's3', 'cp', src, dest, '--request-payer', 'requester'],
                       capture_output=True, text=True)
    if r.returncode != 0:
        log('[reservoir] %s 跳过：%s' % (day, (r.stderr or '').strip().splitlines()[-1:]))
        return False
    return True


def warm_reservoir_1m(cache, universe, start_ms, end_ms, *, workdir=None, log=print):
    """把 [start,end] 每个 UTC 天的 1s 拉下→1m→写 cache '1m' 命名空间。幂等：整天全命中即跳过。

    只缓存**完整**的天：当天(UTC)未过完、或该天在 S3 尚未发布/拉取报错 → 不写任何文件（含空哨兵），
    计入 retry_later，下次重取。只有「日文件已成功下载、但某币当天确无成交」才落该币空哨兵（真空）。"""
    symbol_map = {s.split('/')[0]: s for s in universe}
    now_ms = int(pd.Timestamp.utcnow().value // 1_000_000)
    days = _days(start_ms, end_ms)
    tmpdir = workdir or tempfile.mkdtemp(prefix='reservoir_')
    os.makedirs(tmpdir, exist_ok=True)
    stat = {'days': 0, 'rows': 0, 'skipped_cached': 0, 'retry_later': 0}
    for day in days:
        # 当天(UTC)未过完 → 无完整日文件；不缓存、不落哨兵，下次重取
        day_end_ms = int((pd.Timestamp(day) + pd.Timedelta(days=1)).value // 1_000_000)
        if day_end_ms > now_ms:
            stat['retry_later'] += 1
            continue
        if all(cache.exists(NAMESPACE, s, day) for s in universe):
            stat['skipped_cached'] += 1
            continue
        dest = os.path.join(tmpdir, '%s.parquet' % day)
        if not _s3_cp(day, dest, log=log):
            # 拉取失败(404 未发布 / 接口报错) → 不写任何文件，跳过，下次重取
            stat['retry_later'] += 1
            continue
        raw = pd.read_parquet(dest)
        os.remove(dest)
        per_sym = candles_1s_to_1m(raw, symbol_map)
        for s in universe:
            df = per_sym.get(s)
            if df is None or df.empty:
                cache.write_empty(NAMESPACE, s, day, CANDLE_COLS)  # 日文件已下、该币确无成交 → 真空哨兵
            else:
                cache.write(NAMESPACE, s, day, df)
                stat['rows'] += int(len(df))
        stat['days'] += 1
        if stat['days'] % 10 == 0:
            log('[reservoir] %d days done, rows=%d' % (stat['days'], stat['rows']))
    return stat
