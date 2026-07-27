"""aggTrades 真实路径 vs 4-tick 近似 —— 唯一剩余未验证假设的对撞工具(2026-07-26)。

**背景**:成交额口径标定证明引擎成交模型在 间距 1.204~5.258% / 流动性 $1.78M~$1197M /
参与率需求到 14.7% 三维上 fill_rate≈1.004、无依赖。但那批样本的**穿越/bar 密度**中位仅
0.0042(max 0.0655)——密度 ≪1 时一根 bar 内至多穿一条线,**任何**单调路径都数得一样,
所以 1.004 **恰恰没有验证** `trans_candle_to_tick` 的 4-tick 路径近似。而 eff1×b2_c26 跑在
0.210/bar(极端格 2.30),那里穿越数**完全由该近似决定**。

**三数对撞**:
  X_engine     : 1m bar → 4-tick 近似的穿越数(= 回测现在算的 n_fills)
  X_true       : aggTrades 真实逐笔路径的穿越数(同一套穿越判据)
  X_realizable : 在真实路径上模拟 executor 实际机制(逐线静置挂单 + 成交后对侧补单、
                 补单有 MONITOR_INTERVAL_SEC=5s 延迟)⇒ **实盘真能吃到几笔**

两个方向的偏差会打架,故必须三个一起量:
  · 真实分钟内路径总变差 > 三段折线 ⇒ X_true > X_engine(引擎**少**数)
  · 5s 补单延迟使秒级来回震荡吃不到 ⇒ X_realizable < X_true(实盘**更少**)
净效应 X_realizable / X_engine 才是回测该乘的修正系数。

**方法学**:先用实盘 131 格验证 X_realizable ≈ 实盘真值(仪器校准,已知点),
再在**同一批真实路径**上把间距人工收紧,沿密度轴外推 —— 仪器已校准,外推才可信。

Vision 数据面:futures/um 有 daily/aggTrades(alt 约 0.1~0.4MB/天),**没有 1s klines**
(实测 404;spot 有 1s 但那是现货、价格路径不同,不可用)。

本模块只读归档、不改引擎。
"""
import io
import os
import zipfile

import numpy as np
import pandas as pd
import requests

BASE = 'https://data.binance.vision'
AGG_CACHE = os.path.expanduser('~/.cache/gridtrade_aggtrades')
LATENCY_SEC = 5.0        # prod MONITOR_INTERVAL_SEC(实测 flyctl printenv = 5)


def agg_url(native, day):
    return '%s/data/futures/um/daily/aggTrades/%s/%s-aggTrades-%s.zip' % (
        BASE, native, native, day)


def fetch_agg(native, day, session=None):
    """下载并解析一天的 aggTrades → DataFrame[ts_ms, price, qty]。缺档返回 None。
    落本地缓存(zip 原文),重跑不重下。"""
    os.makedirs(AGG_CACHE, exist_ok=True)
    fp = os.path.join(AGG_CACHE, '%s-%s.zip' % (native, day))
    if os.path.exists(fp):
        with open(fp, 'rb') as fh:
            data = fh.read()
    else:
        r = (session or requests).get(agg_url(native, day), timeout=120)
        if r.status_code != 200:
            return None
        data = r.content
        with open(fp, 'wb') as fh:
            fh.write(data)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        name = z.namelist()[0]
        head = z.open(name).readline().decode('utf8', 'ignore')
        has_hdr = 'price' in head.lower()
        df = pd.read_csv(z.open(name), header=0 if has_hdr else None,
                         usecols=[1, 2, 5], names=None if has_hdr else
                         ['agg_id', 'price', 'qty', 'f', 'l', 'ts', 'maker'])
    df.columns = ['price', 'qty', 'ts']
    df['ts'] = pd.to_numeric(df['ts'], errors='coerce')
    # 2025 起部分档案时间戳是微秒;按量级归一到毫秒
    if len(df) and df['ts'].iloc[0] > 1e14:
        df['ts'] = df['ts'] // 1000
    return df.dropna().reset_index(drop=True)


def crossings(prices, lines):
    """价格路径 → 穿越到的网格线序列(与 grid_touch_info + get_trade_info 同判据)。

    判据: 线 p 在 prev→cur 段被穿越 iff (prev < p <= cur) or (prev > p >= cur)。
    段内多线按运动方向排序;最后**去掉连续重复**(get_trade_info 的
    `con = touch == touch.shift()`)。返回 (线索引数组, 每笔对应的路径位置数组)。
    """
    p = np.asarray(prices, dtype=float)
    if len(p) < 2:
        return np.empty(0, dtype=int), np.empty(0, dtype=int)
    ln = np.asarray(lines, dtype=float)
    sr = np.searchsorted(ln, p, side='right')     # 线数 <= p
    sl = np.searchsorted(ln, p, side='left')      # 线数 <  p
    prev_sr, cur_sr = sr[:-1], sr[1:]
    prev_sl, cur_sl = sl[:-1], sl[1:]
    up = cur_sr > prev_sr                          # 上穿: 索引 [prev_sr, cur_sr-1] 升序
    dn = cur_sl < prev_sl                          # 下穿: 索引 [cur_sl, prev_sl-1] 降序
    out_idx, out_pos = [], []
    for k in np.nonzero(up | dn)[0]:
        if up[k]:
            seg = np.arange(prev_sr[k], cur_sr[k])
        else:
            seg = np.arange(prev_sl[k] - 1, cur_sl[k] - 1, -1)
        out_idx.append(seg)
        out_pos.append(np.full(len(seg), k + 1))
    if not out_idx:
        return np.empty(0, dtype=int), np.empty(0, dtype=int)
    idx = np.concatenate(out_idx)
    pos = np.concatenate(out_pos)
    keep = np.empty(len(idx), dtype=bool)          # 去连续重复
    keep[0] = True
    keep[1:] = idx[1:] != idx[:-1]
    return idx[keep], pos[keep]


def tick_path_from_bars(bars):
    """1m bar → 4-tick 近似路径(与 trans_candle_to_tick 同序,方向感知)。不含破网截断。"""
    o = bars['open'].to_numpy(float)
    h = bars['high'].to_numpy(float)
    lo = bars['low'].to_numpy(float)
    c = bars['close'].to_numpy(float)
    up = c >= o
    p2 = np.where(up, lo, h)
    p3 = np.where(up, h, lo)
    return np.column_stack([o, p2, p3, c]).ravel()


def simulate_executor(line_idx, ts_ms, lines, entry, latency_sec=LATENCY_SEC):
    """在真实穿越序列上模拟 grid_executor:逐线静置挂单 + 成交后对侧补单(有延迟)。

    照抄实盘(gridtrade/execution/grid_executor.py):
      · 初始挂单: p > entry → sell, p < entry → buy, p == entry → **跳过**(continue)
      · 成交后 _replenish_opposite: sell@i → buy@i-1;buy@i → sell@i+1
      · 补单不是瞬时的,跟 monitor 轮询(MONITOR_INTERVAL_SEC=5)⇒ 延迟 latency_sec
      · 方向匹配才成交:下穿吃 buy、上穿吃 sell(限价单物理约束)
    返回 (成交笔数, 因缺挂单而错过的穿越数)。
    """
    n = len(lines)
    resting = [None] * n
    for i, p in enumerate(lines):
        if p > entry:
            resting[i] = 'sell'
        elif p < entry:
            resting[i] = 'buy'
    pending = []                                   # (ready_ms, line, side)
    fills = missed = 0
    prev_line = None
    for k, t in zip(line_idx, ts_ms):
        while pending and pending[0][0] <= t:      # 到期补单
            _rt, li, sd = pending.pop(0)
            if resting[li] is None:
                resting[li] = sd
        side_needed = 'buy' if (prev_line is not None and k < prev_line) else 'sell'
        if prev_line is None:                      # 首笔:按相对 entry 的位置定方向
            side_needed = 'buy' if lines[k] < entry else 'sell'
        prev_line = k
        if resting[k] == side_needed:
            fills += 1
            resting[k] = None
            opp = k - 1 if side_needed == 'sell' else k + 1
            opp_side = 'buy' if side_needed == 'sell' else 'sell'
            if 0 <= opp < n and resting[opp] is None:
                pending.append((t + latency_sec * 1000.0, opp, opp_side))
                pending.sort(key=lambda x: x[0])
        else:
            missed += 1
    return fills, missed
