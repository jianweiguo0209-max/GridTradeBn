"""
OKX 公共历史数据取数适配器（requests 直连，免鉴权、线程安全）。

只覆盖「可程序化」的 REST 端点：
- 1H K线（选币 + 布网用）           market/history-candles
- 资金费率（永续PnL，预留）          public/funding-rate-history
- 标记价K线（强平/资金费基准，预留）  market/history-mark-price-candles
- 合约规格（tickSz/上市时间，冻结）   public/instruments

逐笔 tick / L2 盘口来自官方下载页（非本模块；由 prewarm 产出下载 manifest）。

每个取数内部分页拉取一个 [start, end] 区间，再由 prewarm 按天切片落 per-day 缓存。
公共端点无需 API key；如本地需要代理，传 proxies={'https': '...'}。
"""
import time

import pandas as pd
import requests

OKX_BASE = 'https://www.okx.com'

# 与实盘 api/kline.py 落地的列名保持一致，保证选币 parity
CANDLE_COLS = ['symbol', 'candle_begin_time', 'open', 'high', 'low', 'close', 'vol', 'volCcy', 'quote_volume']
_FLOAT_COLS = ['open', 'high', 'low', 'close', 'vol', 'volCcy', 'quote_volume']


def _get(path, params, proxies=None, retries=5, sleep_seconds=1.0, timeout=15):
    """带退避重试的 GET；OKX 返回 code=='0' 视为成功。"""
    url = OKX_BASE + path
    last_err = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, timeout=timeout, proxies=proxies)
            j = r.json()
            if j.get('code') == '0':
                return j.get('data', [])
            # 限频(50011)/系统忙 等：退避重试
            last_err = 'code=%s msg=%s' % (j.get('code'), j.get('msg'))
        except Exception as e:  # noqa
            last_err = str(e)
        time.sleep(sleep_seconds * (i + 1))
    raise RuntimeError('OKX GET 失败 %s params=%s err=%s' % (path, params, last_err))


def _candles_to_df(data, symbol):
    """OKX K线返回 [ts,o,h,l,c,vol,volCcy,volCcyQuote,confirm]（新→旧）。"""
    if not data:
        return pd.DataFrame(columns=['ts'] + CANDLE_COLS)
    df = pd.DataFrame([row[:8] for row in data],
                      columns=['ts', 'open', 'high', 'low', 'close', 'vol', 'volCcy', 'quote_volume'])
    df['ts'] = df['ts'].astype('int64')
    for c in _FLOAT_COLS:
        df[c] = df[c].astype(float)
    df['candle_begin_time'] = pd.to_datetime(df['ts'], unit='ms')  # UTC naive
    df['symbol'] = symbol
    df = df[['ts'] + CANDLE_COLS]
    df.sort_values('ts', inplace=True)
    df.drop_duplicates(subset=['ts'], keep='last', inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


def fetch_candles_range(symbol, start_ms, end_ms, bar='1H', proxies=None, page_limit=100):
    """
    分页拉取 [start_ms, end_ms] 区间的 K线，返回带 'ts' 列、按时间升序的 DataFrame。
    history-candles 每页最多 100 根，新→旧；用 after 向更早翻页。
    """
    all_rows = []
    after = end_ms + 1  # after: 返回早于该 ts 的记录
    guard = 0
    while True:
        guard += 1
        if guard > 100000:  # 死循环兜底
            break
        data = _get('/api/v5/market/history-candles',
                    {'instId': symbol, 'bar': bar, 'after': str(after), 'limit': str(page_limit)},
                    proxies=proxies)
        if not data:
            break
        all_rows.extend(data)
        oldest_ts = int(data[-1][0])  # 新→旧，最后一条最旧
        if oldest_ts <= start_ms:
            break
        if oldest_ts >= after:  # 没有继续向更早推进，停止
            break
        after = oldest_ts
    df = _candles_to_df(all_rows, symbol)
    if df.empty:
        return df
    return df[(df['ts'] >= start_ms) & (df['ts'] <= end_ms)].reset_index(drop=True)


def fetch_instruments(inst_type='SWAP', proxies=None):
    """合约规格列表，返回 DataFrame（含 instId / tickSz / lotSz / listTime / state ...）。"""
    data = _get('/api/v5/public/instruments', {'instType': inst_type}, proxies=proxies)
    if not data:
        return pd.DataFrame()
    return pd.DataFrame(data)


# ---- 以下为「下一步」预留：资金费 / 标记价（S2-API），prewarm v1 默认不调用 ----

def fetch_funding_rate_range(symbol, start_ms, end_ms, proxies=None, page_limit=100):
    """资金费率历史 [start, end]，升序返回。public/funding-rate-history 每页最多 100。"""
    all_rows = []
    after = end_ms + 1
    guard = 0
    while True:
        guard += 1
        if guard > 100000:
            break
        data = _get('/api/v5/public/funding-rate-history',
                    {'instId': symbol, 'after': str(after), 'limit': str(page_limit)},
                    proxies=proxies)
        if not data:
            break
        all_rows.extend(data)
        oldest_ts = int(data[-1]['fundingTime'])
        if oldest_ts <= start_ms or oldest_ts >= after:
            break
        after = oldest_ts
    if not all_rows:
        return pd.DataFrame(columns=['ts', 'symbol', 'fundingRate', 'realizedRate'])
    df = pd.DataFrame(all_rows)
    df['ts'] = df['fundingTime'].astype('int64')
    df['symbol'] = symbol
    df['fundingRate'] = df['fundingRate'].astype(float)
    if 'realizedRate' in df.columns:
        df['realizedRate'] = pd.to_numeric(df['realizedRate'], errors='coerce')
    df = df[(df['ts'] >= start_ms) & (df['ts'] <= end_ms)]
    return df.sort_values('ts').reset_index(drop=True)


def fetch_mark_candles_range(symbol, start_ms, end_ms, bar='1H', proxies=None, page_limit=100):
    """标记价 K线 [start, end]，升序返回。"""
    all_rows = []
    after = end_ms + 1
    guard = 0
    while True:
        guard += 1
        if guard > 100000:
            break
        data = _get('/api/v5/market/history-mark-price-candles',
                    {'instId': symbol, 'bar': bar, 'after': str(after), 'limit': str(page_limit)},
                    proxies=proxies)
        if not data:
            break
        all_rows.extend(data)
        oldest_ts = int(data[-1][0])
        if oldest_ts <= start_ms or oldest_ts >= after:
            break
        after = oldest_ts
    if not all_rows:
        return pd.DataFrame(columns=['ts', 'symbol', 'open', 'high', 'low', 'close'])
    df = pd.DataFrame([row[:5] for row in all_rows], columns=['ts', 'open', 'high', 'low', 'close'])
    df['ts'] = df['ts'].astype('int64')
    for c in ['open', 'high', 'low', 'close']:
        df[c] = df[c].astype(float)
    df['symbol'] = symbol
    df = df[(df['ts'] >= start_ms) & (df['ts'] <= end_ms)]
    return df.sort_values('ts').drop_duplicates(subset=['ts']).reset_index(drop=True)
